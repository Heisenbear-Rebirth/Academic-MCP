import os
import asyncio
import functools
import sys
from mcp_logging import safe_stderr_print
from typing import List, Dict, Optional
from playwright.async_api import async_playwright
import bs4
import pymupdf4llm
import re
import hashlib

print = safe_stderr_print

class IEEEScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
    
    async def initialize(self, force_headful=False):
        import json
        self.playwright = None # Will not be used anymore
        
        from runtime_config import profile_path
        from scraper_utils import acquire_profile
        profile_dir = profile_path(".ieee_profile")
        # Refuse early (clean error, no blocking Firefox modal) if another live MCP
        # server already owns this profile.
        acquire_profile(profile_dir, "IEEE")
        self._profile_dir = profile_dir
        # parent.lock / .parentlock are Firefox-style locks (Camoufox is Firefox-based);
        # lockfile / SingletonLock are Chromium-style. A crashed previous Camoufox session
        # leaves parent.lock behind, and the next launch silently exits if we don't clean it.
        for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
            lfile = os.path.join(profile_dir, lock_name)
            if os.path.exists(lfile):
                try: os.remove(lfile)
                except: pass

        os.makedirs(os.path.join(profile_dir, "Default"), exist_ok=True)
        # Bypasses WAF completely; renders PDFs not internally, but drops straight to downloader
        prefs = {"plugins": {"always_open_pdf_externally": True}, "download": {"prompt_for_download": False}}
        with open(os.path.join(profile_dir, "Default", "Preferences"), "w") as f:
            json.dump(prefs, f)
            
        from camoufox.async_api import AsyncCamoufox
        # NOTE: do not enable block_images on IEEE — its Cloudflare profile flags the
        # missing-image-requests pattern. Speed gains come from sleep tightening only.
        self.camoufox_cm = AsyncCamoufox(
            headless=not force_headful,
            user_data_dir=profile_dir,
            persistent_context=True,
            os="windows",
            humanize=True,
            geoip=True,
        )
        self.context = await self.camoufox_cm.__aenter__()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def close(self):
        if hasattr(self, 'page') and self.page:
            try:
                await self.page.close()
                self.page = None
            except:
                pass
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            await self.camoufox_cm.__aexit__(None, None, None)
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None
            self.playwright = None
        if getattr(self, "_profile_dir", None):
            from scraper_utils import release_profile
            release_profile(self._profile_dir)
            self._profile_dir = None

    async def search_papers(self, query: str, search_field: str = "All", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        if not self.page:
            await self.initialize()

        import urllib.parse
        # IEEE Xplore advanced-query syntax: ("Publication Title":"...") forces results
        # to a single journal. Without this, the journal argument was silently ignored.
        journal_clean = (journal or "").strip()
        if journal_clean:
            quoted = journal_clean.replace('"', '\\"')
            search_query = f'("Publication Title":"{quoted}") AND ({query})'
        else:
            search_query = query
        encoded_query = urllib.parse.quote(search_query)

        sort_str = ""
        if sort_by == "citations":
            sort_str = "&sortType=paper-citations"
        elif sort_by == "date_desc":
            sort_str = "&sortType=newest"

        range_str = ""
        if start_year and end_year:
            range_str = f"&ranges={start_year}_{end_year}_Year"
        elif start_year:
            range_str = f"&ranges={start_year}_2026_Year"
        elif end_year:
            range_str = f"&ranges=1900_{end_year}_Year"

        base_url = f"https://ieeexplore.ieee.org/search/searchresult.jsp?newsearch=true&queryText={encoded_query}{sort_str}{range_str}"
        
        await self.page.goto(base_url, wait_until="networkidle")
        await asyncio.sleep(1)

        # Determine total results
        html = await self.page.content()
        soup = bs4.BeautifulSoup(html, 'html.parser')
        
        total_count = "未知"
        try:
            # IEEE typically has something like "Showing 1-25 of 1,234,567 results for"
            match = re.search(r'of\s+([\d,]+)\s+results?', soup.text, re.IGNORECASE)
            if match:
                total_count = match.group(1).replace(",", "")
            else:
                for pattern in [
                    r'"totalRecords"\s*:\s*"?([\d,]+)"?',
                    r'"totalResults"\s*:\s*"?([\d,]+)"?',
                    r'"recordsTotal"\s*:\s*"?([\d,]+)"?',
                    r'([\d,]+)\s+Results',
                ]:
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        total_count = match.group(1).replace(",", "")
                        break
            if total_count == "未知":
                # Try finding in specific classes
                count_elem = soup.select_one("span.strong, h1, span[class*='results-display']")
                if count_elem:
                    match = re.search(r'([\d,]+)\s+results', count_elem.text, re.IGNORECASE)
                    if match:
                        total_count = match.group(1).replace(",", "")
        except Exception as e:
            print("IEEE Extract count error:", e)

        # In IEEE, one page applies source_type by navigating Facets
        if source_type.lower() != "all" and source_type != "全部":
            try:
                # Find the label or specific text for the facet
                facet = self.page.locator(f"//label[contains(text(), '{source_type}')] | //div[contains(@class, 'facet-label') and contains(text(), '{source_type}')] | //input[@type='checkbox']/parent::*//*[contains(text(), '{source_type}')]").first
                if await facet.count() > 0:
                    await facet.scroll_into_view_if_needed()
                    await asyncio.sleep(0.5)
                    await facet.click(force=True)
                    
                    # Sometimes there is an Apply button, sometimes it auto-reloads.
                    apply_btn = self.page.locator("button:has-text('Apply')").first
                    if await apply_btn.count() > 0:
                        await apply_btn.click(force=True)
                    
                    # Wait for network and DOM update
                    await asyncio.sleep(4)
            except Exception as e:
                print(f"Error filtering IEEE source_type: {e}")

        # IEEE pagination works via passing pageNumber= (1-indexed based on page sizes of 25)
        # However, for simplicity and alignment with CNKI, we just simulate clicking Next or doing basic pagination.
        target_start_page = (start_index // 25) + 1
        offset_in_first_page = start_index % 25

        if target_start_page > 1:
            try:
                # Direct url manipulation may be safer for leaping pages in IEEE
                current_url = self.page.url
                if "pageNumber=" not in current_url:
                    current_url += f"&pageNumber={target_start_page}"
                else:
                    current_url = re.sub(r'pageNumber=\d+', f'pageNumber={target_start_page}', current_url)
                await self.page.goto(current_url, wait_until="networkidle")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"Pagination error jumping to page {target_start_page}: {e}")

        results = []
        collected = 0
        seen_links = set()
        
        while collected < limit:
            html = await self.page.content()
            soup = bs4.BeautifulSoup(html, "html.parser")
            
            # The items are usually inside div.List-results-items
            rows = soup.select("div.List-results-items, div.result-item")
            if not rows:
                break
                
            startIndexToProcess = offset_in_first_page if collected == 0 else 0
            
            for row in rows[startIndexToProcess:]:
                if collected >= limit:
                    break
                    
                title_elem = row.select_one("h2 a, h3 a")
                title = title_elem.text.strip() if title_elem else "N/A"
                detail_link = title_elem.get("href") if title_elem else ""
                if detail_link and detail_link.startswith("/"):
                    detail_link = "https://ieeexplore.ieee.org" + detail_link
                    
                if detail_link in seen_links:
                    continue
                seen_links.add(detail_link)
                    
                author_elem = row.select_one(".author, p.author")
                author = author_elem.text.strip() if author_elem else "N/A"
                author = author.replace("\n", "").replace("  ", "")

                # Venue link lives directly inside div.description (the first <a> before the
                # nested publisher-info-container). Journals point at /xpl/RecentIssue.jsp?punumber=
                # while conferences point at /xpl/conhome/<id>/proceeding.
                description = row.select_one("div.description")
                venue_name = ""
                if description:
                    venue_link = description.find(
                        "a",
                        href=lambda h: h and ("/xpl/RecentIssue" in h or "/xpl/conhome" in h or "punumber=" in h),
                    )
                    if venue_link and venue_link.text.strip():
                        venue_name = venue_link.text.strip()
                publisher_container = row.select_one("div.publisher-info-container")

                # Belt and suspenders: if the caller asked for a specific journal but IEEE
                # returned a paper from a different venue (happens when the query syntax falls
                # back to full-text matching), skip it client-side. venue_matches handles
                # punctuation / parenthetical noise like "Conference Paper" suffixes.
                from scraper_utils import venue_matches
                if journal_clean and venue_name and not venue_matches(journal_clean, venue_name):
                    continue

                source = publisher_container.text.strip() if publisher_container else "IEEE"

                # Year info
                year_elem = row.select_one("div.description, div.publisher-info-container")
                date = "N/A"
                if year_elem:
                    y_match = re.search(r'Year:\s*(\d{4})', year_elem.text)
                    if y_match:
                        date = y_match.group(1)

                db_type = "N/A"
                if year_elem:
                    t_match = re.search(r'\|\s*([^|]+)$', year_elem.text)
                    if t_match:
                        db_type = t_match.group(1).strip()

                uid = hashlib.md5(detail_link.encode()).hexdigest()[:8]

                results.append({
                    "id": uid,
                    "title": title,
                    "author": author,
                    "source": source,
                    "venue_name": venue_name,
                    "date": date,
                    "db_type": db_type,
                    "detail_link": detail_link,
                })
                collected += 1
                
            if collected < limit:
                try:
                    next_btn = self.page.locator("a.next-btn, a.next, button:has-text('Next')").first
                    if await next_btn.count() > 0:
                        await next_btn.click()
                        await asyncio.sleep(4)
                    else:
                        break
                except Exception:
                    break
                    
        return {
            "total_results": total_count,
            "papers": results
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        if not self.page:
            await self.initialize()

        from scraper_utils import goto_with_retry
        await goto_with_retry(self.page, detail_url, wait_until="networkidle")
        await asyncio.sleep(2)
        
        try:
            html = await self.page.content()
            abstract = "No abstract found."
            keywords = []
            doi = ""

            # IEEE detail page injects all metadata as JSON into a script tag
            # This is significantly more robust than parsing React DOM classes
            match = re.search(r'xplGlobal\.document\.metadata\s*=\s*(\{.*?\});', html, re.DOTALL)
            if match:
                try:
                    import json
                    meta = json.loads(match.group(1))
                    if "abstract" in meta:
                        abstract = bs4.BeautifulSoup(meta["abstract"], "html.parser").text.strip()
                    if "doi" in meta:
                        doi = meta["doi"]
                    if "keywords" in meta:
                        for k_obj in meta["keywords"]:
                            if "kwd" in k_obj:
                                keywords.extend(k_obj["kwd"])
                except Exception as e:
                    print("IEEE JSON Meta parse error:", e)

            # Fallback to visual DOM parsing if JSON fails
            if abstract == "No abstract found.":
                soup = bs4.BeautifulSoup(html, "html.parser")
                abstract_elem = soup.select_one("div.abstract-text, div.u-mb-1 div")
                if abstract_elem:
                    abstract = abstract_elem.text.strip()
                if abstract.startswith("Abstract:"):
                    abstract = abstract[9:].strip()
                doi_elem = soup.select_one("a[href^='https://doi.org/']")
                if doi_elem:
                    doi = doi_elem.text.strip()

            return {
                "url": detail_url,
                "abstract": abstract,
                "keywords": list(set(keywords)),
                "doi": doi
            }
        except Exception as e:
            return {"error": str(e)}

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        if not self.page:
            await self.initialize()
            
        os.makedirs(output_dir, exist_ok=True)
        
        # We find the arnumber directly from the URL e.g. /document/11007439/
        match = re.search(r'document/(\d+)', detail_url)
        if not match:
            return "Could not find arnumber in detail_url."
        arnumber = match.group(1)
        
        # Use the direct PDF stream URL
        pdf_url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}"
        
        await self.page.goto(detail_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        file_path = os.path.join(output_dir, f"ieee_{arnumber}.pdf")
        
        # Action: A-tag emulation triggered 418 inside IEEE WAF since it's an untrusted DOM Event.
        # Action Update: We natively transition page via evaluate JS. The Preferences JSON triggers immediate DL.
        try:
            file_path = os.path.join(output_dir, f"ieee_{arnumber}.pdf")
            
            # Setup a listener to catch any response that looks like the PDF stream
            pdf_body = []
            async def handle_response(response):
                if response.status == 200 and ("application/pdf" in response.headers.get("content-type", "") or response.url.endswith(".pdf") or "getPDF.jsp" in response.url):
                    try:
                        body = await response.body()
                        if b"%PDF" in body[:20]:
                            pdf_body.append(body)
                            print(f"Intercepted direct PDF stream from {response.url[:60]}...")
                    except Exception:
                        pass
                        
            self.page.on("response", handle_response)
            
            download_event_task = asyncio.create_task(self.page.wait_for_event("download"))
            js_code = f"window.location.href = '{pdf_url}';"
            await self.page.evaluate(js_code)
                
            is_downloaded = False
            for _ in range(60):
                if download_event_task.done():
                    try:
                        download = download_event_task.result()
                        await download.save_as(file_path)
                        is_downloaded = True
                    except Exception as e:
                        print("Native download failed:", e)
                    break
                    
                if len(pdf_body) > 0:
                    with open(file_path, "wb") as f:
                        f.write(pdf_body[0])
                    is_downloaded = True
                    download_event_task.cancel()
                    break
                    
                await asyncio.sleep(1)
                
            self.page.remove_listener("response", handle_response)
            if not download_event_task.done():
                download_event_task.cancel()
                
            if not is_downloaded:
                return "Error downloading IEEE PDF: Timeout 60s exceeded waiting for download event or stream."
                
            # Additional size verify
            if os.path.getsize(file_path) < 70000:
                return f"Error: Download generated successfully but size seems to be an HTML trap ({os.path.getsize(file_path)} bytes)."
                
            return file_path
        except Exception as e:
            return f"Error downloading IEEE PDF: {str(e)}"

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not pdf_path or not os.path.exists(pdf_path) or "Error" in pdf_path:
            return f"Failed to download PDF: {pdf_path}"
            
        if not pdf_path.lower().endswith(".pdf"):
            return f"Downloaded file is not a PDF, conversion not supported. Saved at: {pdf_path}"
            
        try:
            images_dir = os.path.join(output_dir, "images")
            os.makedirs(images_dir, exist_ok=True)
            
            md_text = pymupdf4llm.to_markdown(
                doc=pdf_path,
                write_images=True,
                image_path=images_dir
            )
            
            base_name = os.path.basename(pdf_path)
            md_filename = os.path.splitext(base_name)[0] + ".md"
            md_path = os.path.join(output_dir, md_filename)
            
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md_text)
                
            snippet = md_text[:1000] + "\n...(More Content Available)"
            res = (f"=== 转换成功 ===\n"
                   f"PDF已下载: {pdf_path}\n"
                   f"Markdown及高清图像已保存至目录: {output_dir}\n"
                   f"完整的MD文件路径: {md_path}\n"
                   f"--- 以下为前1000字预览 ---\n\n{snippet}")
            return res
        except Exception as e:
            return f"PDF downloaded to {pdf_path} but Markdown conversion failed: {str(e)}"

scraper_instance = IEEEScraper()
