import asyncio
import functools
import sys
from bs4 import BeautifulSoup
import os
import hashlib
import pymupdf4llm
from typing import Dict, List
import urllib.parse
import re
from mcp_logging import safe_stderr_print
from runtime_config import allow_headful_fallback_for, ensure_runtime_environment, project_path

print = safe_stderr_print
ensure_runtime_environment()

class ScienceDirectScraper:
    def __init__(self):
        self.context = None
        self.page = None
        self.is_headful = False
        self.camoufox_cm = None
        self.allow_headful_fallback = allow_headful_fallback_for("SD")

    async def _ensure_browser(self, force_headful=False):
        if not self.context:
            print(f"Initializing ScienceDirect Persistent Browser Context (Headless: {not force_headful})...")
            profile_dir = project_path(".sd_profile")

            for lock_name in ["lockfile", "SingletonLock"]:
                lfile = os.path.join(profile_dir, lock_name)
                if os.path.exists(lfile):
                    try: os.remove(lfile)
                    except: pass

            from camoufox.async_api import AsyncCamoufox

            # Pin os=windows so the fingerprint hash stays stable across MCP restarts -- Cloudflare's
            # cf_clearance is bound to that hash, so randomizing OS every launch invalidates it and
            # forces a fresh manual challenge each session.
            # Do NOT block_images: SD rides DataDome which flags the missing-image-requests pattern
            # (Camoufox warns about this explicitly).
            # firefox_user_prefs disables the built-in PDF viewer so downloads land as download events.
            self.is_headful = force_headful
            self.camoufox_cm = AsyncCamoufox(
                headless=not force_headful,
                user_data_dir=profile_dir,
                persistent_context=True,
                os="windows",
                humanize=True,
                geoip=True,
                firefox_user_prefs={"pdfjs.disabled": True},
            )
            self.context = await self.camoufox_cm.__aenter__()
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            
            print("Navigating to SD (Resolving protections)...")
            await self.page.goto("https://www.sciencedirect.com")
            
            # Smart wait for DataDome
            cf_blocked = True
            for _ in range(15):
                try:
                    title = await self.page.title()
                    html = await self.page.content()
                    if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "请稍候" not in title and "Cloudflare" not in title and "Just a moment" not in title and "DataDome" not in html:
                        cf_blocked = False
                        break
                except Exception:
                    pass # Navigation in progress
                await asyncio.sleep(1)
                
            if cf_blocked:
                if not force_headful:
                    if not self.allow_headful_fallback:
                        print("[Anti-Bot] SD CAPTCHA blocked in headless mode; headful fallback is disabled.")
                        return
                    print("[Anti-Bot] SD CAPTCHA blocked in headless! Relaunching headful for manual check...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    return
                else:
                    print(">>> PLEASE WAIT OR SOLVE SD CAPTCHA IN BROWSER WINDOW. Waiting up to 60s... <<<")
                    solved = False
                    for _ in range(60):
                        try:
                            title = await self.page.title()
                            html = await self.page.content()
                            if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "请稍候" not in title and "Cloudflare" not in title and "Just a moment" not in title and "DataDome" not in html:
                                print("[Anti-Bot] SD CAPTCHA passed! Proceeding...")
                                solved = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
            
            # Try to accept cookies banner if it exists
            try:
                btn = self.page.locator('button#onetrust-accept-btn-handler')
                if await btn.count() > 0:
                    await btn.first.click()
                    print("Accepted SD cookies.")
                    await asyncio.sleep(1)
            except Exception:
                pass
                
            print("ScienceDirect context initialized successfully.")

    async def initialize(self):
        await self._ensure_browser()

    async def close(self):
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            try:
                await self.camoufox_cm.__aexit__(None, None, None)
            except Exception as e:
                print(f"ScienceDirect context close failed: {e}")
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None

    async def search_papers(self, query: str, search_field: str = "qs", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        await self._ensure_browser()
        
        # SD handles pages by 'offset' (number of items to skip).
        # We can pass offset=start_index directly!
        offset = start_index 
        
        # SD expects `qs` for default keyword search, `authors` for author, `title` for Title
        field_map = {
            "全部": "qs",
            "主题": "qs",
            "篇名": "title",
            "摘要": "qs", # SD doesn't easily isolate abstract only via query params, defaults to qs
            "作者": "authors"
        }
        field = field_map.get(search_field, search_field if search_field in ["qs", "title", "authors"] else "qs")
        
        q_url = f"https://www.sciencedirect.com/search?{field}={urllib.parse.quote_plus(query)}&offset={offset}"
        
        if journal:
            q_url += f"&pub={urllib.parse.quote_plus(journal)}"
            
        if sort_by == "date_desc":
            q_url += "&sortBy=date"
            
        if start_year and end_year:
            # For SD we can pass a range or list of years. Range might not explicitly be date=2023-2026. Usually it's years=2023,2024. Let's try date.
            q_url += f"&date={start_year}-{end_year}"
        elif start_year:
            q_url += f"&date={start_year}-2026"
        elif end_year:
            q_url += f"&date=1900-{end_year}"
        
        # Mappings for source_type 
        # e.g., articleTypes=REV (Review articles), FLA (Research articles), etc.
        if source_type.lower() not in ["all", "全部", ""]:
            # Let's map exactly to SD's articleTypes
            q_url += f"&articleTypes={source_type}"
            
        print(f"Navigating to SD search: {q_url}")
        
        for attempt in range(2):
            await self.page.goto(q_url, wait_until="domcontentloaded")
            
            # Smart wait for DataDome/CF
            captcha_detected = False
            for _ in range(15):
                try:
                    title = await self.page.title()
                    html = await self.page.content()
                    if "Are you a robot" in title or "Are you a robot" in html or "Egy pillanat" in title or "DataDome" in html or "请稍候" in title or "Just a moment" in title:
                        captcha_detected = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
                
            if captcha_detected:
                if attempt == 0:
                    if not self.allow_headful_fallback:
                        return {
                            "total_results": "0",
                            "papers": [],
                            "error": "ScienceDirect anti-bot verification blocked the headless MCP session. Add SD to allow_headful_fallback_platforms in mcp_runtime_config.json for manual browser verification.",
                        }
                    print("[Anti-Bot] CAPTCHA detected in headless mode! Relaunching in headful mode for user intervention...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    continue # Try again with headful browser
                else:
                    print(">>> PLEASE SOLVE THE CAPTCHA IN THE BROWSER WINDOW. Waiting up to 60 seconds... <<<")
                    solved = False
                    for _ in range(60):
                        try:
                            title = await self.page.title()
                            html = await self.page.content()
                            if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "DataDome" not in html and "请稍候" not in title and "Just a moment" not in title:
                                print("[Anti-Bot] CAPTCHA completely solved! Proceeding...")
                                solved = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                    if not solved:
                        print("Captcha was not solved in time.")
            else:
                break # Not blocked!
            
        # Standard Playwright automatic wait and selection
        try:
            await self.page.wait_for_selector("li.ResultItem", timeout=15000)
        except Exception:
            print("No items found. Current title is:", await self.page.title())
            html = await self.page.content()
            if "Are you a robot?" in html:
                print("STILL STUCK IN DATADOME! Manual intervention needed.")
                
        # Check Total Results
        total_str = "未知"
        total_loc = self.page.locator(".search-body-results-text, h1.search-body-results-text, span.search-body-results-text, h1[data-testid='srp-page-title']").first
        if await total_loc.count() > 0:
            txt = await total_loc.inner_text()
            numbers = re.findall(r'[\d,]+', txt)
            if numbers:
                valid_nums = [n.replace(',', '') for n in numbers]
                total_str = str(max(int(n) for n in valid_nums if n.isdigit()))
                
        papers = []
        items = await self.page.locator("li.ResultItem").all()
        
        collected = 0        
        for i, item in enumerate(items):
            if collected >= limit:
                break
                
            title_loc = item.locator(".result-list-title-link")
            title = await title_loc.inner_text() if await title_loc.count() > 0 else "N/A"
            link = await title_loc.get_attribute('href') if await title_loc.count() > 0 else ""
            if link and link.startswith("/"):
                link = "https://www.sciencedirect.com" + link
            
            author_locs = await item.locator(".author").all()
            author_str = ", ".join([await a.inner_text() for a in author_locs]) if author_locs else "N/A"
            
            # Current SD markup nests the venue title several spans deep, so a plain
            # nth-of-type selector matches multiple elements and trips Playwright strict mode.
            # The journal/proceeding link with class subtype-srctitle-link is unique per item.
            venue_loc = item.locator("span.srctitle-date-fields a.subtype-srctitle-link").first
            venue_name = (await venue_loc.inner_text()).strip() if await venue_loc.count() > 0 else ""

            # journal filter post-check: SD's &pub= URL param is fuzzy, drop cross-publication
            # hits when caller wanted a specific journal.
            if journal and venue_name:
                target = journal.strip().lower()
                vn = venue_name.lower()
                if target and target not in vn and vn not in target:
                    continue

            # Date sits as the second direct child <span> of .srctitle-date-fields (e.g. "March 2025").
            date_loc = item.locator("span.srctitle-date-fields > span").nth(1)
            if await date_loc.count() == 0:
                date_loc = item.locator("span.srctitle-date-fields > span").last
            date = await date_loc.inner_text() if await date_loc.count() > 0 else "N/A"

            doc_type = "Article"
            type_loc = item.locator(".article-type")
            if await type_loc.count() > 0:
                doc_type = await type_loc.inner_text()

            uid = hashlib.md5(link.encode()).hexdigest()[:8]

            papers.append({
                "id": uid,
                "title": title.strip(),
                "author": author_str.strip(),
                "source": venue_name or "ScienceDirect",
                "venue_name": venue_name,
                "date": date.strip(),
                "db_type": doc_type.strip(),
                "detail_link": link,
            })
            collected += 1
            
        return {
            "total_results": total_str,
            "papers": papers
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        await self._ensure_browser()
        await self.page.goto(detail_url, wait_until="domcontentloaded")
        
        for _ in range(15):
            try:
                title = await self.page.title()
                html = await self.page.content()
                if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "请稍候" not in title and "Cloudflare" not in title and "Just a moment" not in title and "DataDome" not in html:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
            
        await asyncio.sleep(2)
        
        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")
        
        abstract_div = soup.select_one(".abstract.author, #abstracts, #aep-abstract-id")
        abstract = abstract_div.text.strip() if abstract_div else "No abstract provided."
        
        keywords = []
        kw_tags = soup.select(".keyword, .keywords-section .keyword")
        for tag in kw_tags:
            keywords.append(tag.text.strip())
            
        doi_match = re.search(r'doi\.org/(10\.[^/]+/[^"]+)', html)
        doi = doi_match.group(1) if doi_match else ""
            
        return {
            "url": detail_url,
            "abstract": abstract.replace('\n', ' '),
            "keywords": keywords,
            "doi": doi
        }

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        await self._ensure_browser()
        os.makedirs(output_dir, exist_ok=True)
        
        # To get the valid PDF URL with the encrypted md5 signature, we MUST first go to the page and find the View PDF button.
        await self.page.goto(detail_url, wait_until="domcontentloaded")
        
        import re
        pii_match = re.search(r'/pii/([A-Z0-9]+)', detail_url)
        safe_name = pii_match.group(1) if pii_match else "unknown_pii"
        
        page_title = await self.page.title()
        print(f"Page Title: {page_title}")
        
        try:
            # Wait for client-side React/Vue components to render the download button
            try:
                await self.page.wait_for_selector(f'a[href*="/science/article/pii/{safe_name}/pdfft"]', timeout=15000)
            except Exception as e:
                print(f"Selector wait timeout, button might be delayed or unavailable: {e}")
                
            import re
            pii_match = re.search(r'/pii/([A-Z0-9]+)', detail_url)
            safe_name = pii_match.group(1) if pii_match else "unknown_pii"
            
            # Check for DataDome or wait for the PDF link to appear
            try:
                await self.page.wait_for_selector(f'a[href*="/pdfft"]', timeout=20000)
            except Exception:
                # Might be DataDome on the detail page!
                html = await self.page.content()
                title = await self.page.title()
                if "Are you a robot" in title or "Are you a robot" in html or "DataDome" in html or "请稍候" in title or "Just a moment" in title:
                    if not getattr(self, 'is_headful', False):
                        if not self.allow_headful_fallback:
                            return "Error: ScienceDirect verification blocked the detail page in headless mode. Add SD to allow_headful_fallback_platforms for manual verification."
                        print("[Anti-Bot] CAPTCHA detected on detail page! Relaunching in headful mode...")
                        await self.close()
                        await self._ensure_browser(force_headful=True)
                        self.is_headful = True
                        return await self.download_paper(detail_url, output_dir)
                    else:
                        print(">>> PLEASE SOLVE DATADOME CAPTCHA... Waiting up to 60 seconds... <<<")
                        for _ in range(60):
                            html = await self.page.content()
                            title = await self.page.title()
                            if "Are you a robot" not in title and "Are you a robot" not in html and "DataDome" not in html and "请稍候" not in title and "Just a moment" not in title:
                                break
                            await asyncio.sleep(1)
                        await self.page.wait_for_selector(f'a[href*="/pdfft"]', timeout=15000)

            # Dynamically extract the PDF link matching the current PII, bypassing text quirks like &nbsp;
            js_code_extract = """
            (targetPii) => {
                let btn = Array.from(document.querySelectorAll('a')).find(el => 
                    el.href && el.href.includes('/science/article/pii/' + targetPii + '/pdfft')
                );
                return btn ? btn.href : null;
            }
            """
            pdf_url = await self.page.evaluate(js_code_extract, safe_name)
            
            if not pdf_url:
                html = await self.page.content()
                with open("scratch/dump_fail.html", "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"Could not find the true PDF link with md5 token on the page for {safe_name}. HTML dumped to dump_fail.html")
                return "Error: Could not locate the native View PDF button."
                
            print(f"Discovered signed PDF URL: {pdf_url}")
            
            import re
            pii_match = re.search(r'/pii/([A-Z0-9]+)', detail_url)
            safe_name = pii_match.group(1) if pii_match else "unknown_pii"
            file_path = os.path.join(output_dir, f"sd_{safe_name}.pdf")

            direct_status = None
            direct_type = ""
            direct_size = 0
            try:
                direct_response = await self.context.request.get(
                    pdf_url,
                    headers={"Referer": detail_url, "Accept": "application/pdf,*/*"},
                    timeout=60000,
                )
                direct_body = await direct_response.body()
                direct_status = direct_response.status
                direct_type = direct_response.headers.get("content-type", "").lower()
                direct_size = len(direct_body)
                if direct_status == 200 and (
                    direct_body.startswith(b"%PDF") or "application/pdf" in direct_type
                ):
                    with open(file_path, "wb") as f:
                        f.write(direct_body)
                    return file_path
                # Silent DataDome: 200 with text/html body, or 403 from the asset host.
                if b"datadome" in direct_body[:4096].lower() or b"are you a robot" in direct_body[:4096].lower():
                    return (
                        "Error: ScienceDirect served a DataDome challenge for the PDF stream "
                        f"(direct request returned HTTP {direct_status}, {direct_size} bytes of "
                        f"content-type {direct_type or 'unknown'}). Re-run with manual verification "
                        f"enabled. PDF URL: {pdf_url}"
                    )
                print(
                    f"SD direct PDF request did not return a PDF: status={direct_status}, content-type={direct_type}, body={direct_size}B"
                )
            except Exception as e:
                print(f"SD direct PDF request failed, falling back to viewer flow: {e}")
            
            # Navigate to the EPDF viewer. The /pdfft entry triggers a redirect chain that
            # ultimately serves PDF bytes from pdf.sciencedirectassets.com (an S3 signed URL).
            # The trick: hook expect_response BEFORE navigating so we capture the PDF body in
            # flight -- this is more reliable than the legacy "click button, wait for download
            # event" path because (a) the download event doesn't always fire on Firefox with
            # pdfjs disabled and (b) the signed URL only appears in page.url after the click.
            def is_pdf_response(resp):
                ct = resp.headers.get("content-type", "").lower()
                if "application/pdf" in ct:
                    return True
                if "sciencedirectassets.com" in resp.url and ".pdf" in resp.url:
                    return True
                return False

            try:
                async with self.page.expect_response(is_pdf_response, timeout=45000) as pdf_resp_info:
                    try:
                        await self.page.goto(pdf_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        # Inline PDF responses can abort the goto with NS_BINDING_ABORTED on
                        # Firefox. The response we want has already fired by then.
                        print(f"SD viewer goto raised (likely the PDF response already fired): {e}")
                pdf_response = await pdf_resp_info.value
                pdf_body = await pdf_response.body()
                if pdf_response.status == 200 and pdf_body.startswith(b"%PDF"):
                    with open(file_path, "wb") as f:
                        f.write(pdf_body)
                    print(
                        f"SD asset-direct path succeeded "
                        f"({len(pdf_body)} bytes intercepted from {pdf_response.url[:80]}...)."
                    )
                    return file_path
                print(
                    f"SD intercepted PDF response was not a valid PDF: status={pdf_response.status}, "
                    f"url={pdf_response.url[:120]!r}, body={len(pdf_body)}B"
                )
            except Exception as e:
                print(f"SD response interception failed, falling back to viewer button flow: {e}")

            # Check for DataDome on the EPDF page (only relevant if the viewer didn't already
            # redirect to a signed PDF URL above).
            captcha_detected = False
            for _ in range(15):
                try:
                    title = await self.page.title()
                    html = await self.page.content()
                    if "Are you a robot" in title or "Are you a robot" in html or "Egy pillanat" in title or "DataDome" in html or "请稍候" in title or "Just a moment" in title:
                        captcha_detected = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
                
            if captcha_detected:
                if not getattr(self, 'is_headful', False):
                    if not self.allow_headful_fallback:
                        return "Error: ScienceDirect verification blocked the PDF viewer in headless mode. Add SD to allow_headful_fallback_platforms for manual verification."
                    print("[Anti-Bot] CAPTCHA detected on PDF download! Relaunching in headful mode...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    return await self.download_paper(detail_url, output_dir)
                else:
                    print(">>> PLEASE SOLVE DATADOME CAPTCHA FOR PDF VIEW. Waiting up to 60 seconds... <<<")
                    solved = False
                    for _ in range(60):
                        try:
                            title = await self.page.title()
                            html = await self.page.content()
                            if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "DataDome" not in html and "请稍候" not in title and "Just a moment" not in title:
                                print("[Anti-Bot] PDF CAPTCHA solved! Proceeding...")
                                solved = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                    if not solved:
                        return "Error: Did not pass PDF DataDome captcha."
                        
            # We are now strictly in the EPDF viewer. Click the actual download button.
            js_click_download = """
            () => {
                let btn = document.querySelector('button[aria-label="Download PDF"], a[download], a.pdf-download-btn, a#pdfLink');
                if(btn) { btn.click(); return true; }
                
                // Fallback fuzzy search
                let links = Array.from(document.querySelectorAll('a, button'));
                let dl = links.find(el => el.innerText && el.innerText.toLowerCase().includes("download"));
                if(dl) { dl.click(); return true; }
                
                return false;
            }
            """
            
            print("Attempting to trigger actual file download...")
            
            # Setup a listener to catch any response that looks like the PDF stream
            pdf_body = []
            async def handle_response(response):
                if response.status == 200 and ("application/pdf" in response.headers.get("content-type", "") or response.url.endswith(".pdf") or "pdf.sciencedirectassets" in response.url):
                    try:
                        body = await response.body()
                        if b"%PDF" in body[:20]:
                            pdf_body.append(body)
                            print(f"Intercepted direct PDF stream from {response.url[:60]}...")
                    except Exception:
                        pass
                        
            self.page.on("response", handle_response)
            
            download_event_task = asyncio.create_task(self.page.wait_for_event("download"))
            
            success = await self.page.evaluate(js_click_download)
            if not success:
                print("Warning: Could not find obvious download button via JS. The browser might download it automatically or it failed.")
                
            is_downloaded = False
            for _ in range(60):
                # 1. Native Download triggered?
                if download_event_task.done():
                    try:
                        download = download_event_task.result()
                        await download.save_as(file_path)
                        is_downloaded = True
                    except Exception as e:
                        print("Native download event failed:", e)
                    break

                # 2. Intercepted PDF bytes from navigation?
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
                # Build a self-explanatory error so the caller can tell apart
                # silent CAPTCHA / asset 403 / no-download-button on the EPDF viewer.
                try:
                    viewer_title = await self.page.title()
                except Exception:
                    viewer_title = "(title unavailable)"
                try:
                    viewer_html = await self.page.content()
                except Exception:
                    viewer_html = ""
                viewer_url = self.page.url
                silent_block = any(
                    marker in (viewer_html or "").lower()
                    for marker in ("datadome", "are you a robot", "captcha-delivery", "geo.captcha")
                )
                reason = (
                    "DataDome silently blocked the EPDF viewer (no native captcha banner but the "
                    "PDF stream never fired)."
                    if silent_block
                    else "EPDF viewer loaded but the download button did not produce a download event "
                         "(possibly a missing entitlement or layout change)."
                )
                return (
                    f"Error: SD PDF download timed out after 60s.\n"
                    f"Reason: {reason}\n"
                    f"Viewer URL: {viewer_url}\n"
                    f"Viewer title: {viewer_title}\n"
                    f"Direct PDF asset request: status={direct_status}, content-type={direct_type or '?'}, size={direct_size}B\n"
                    f"Signed PDF URL: {pdf_url}\n"
                    f"Hint: rerun with allow_headful_fallback enabled, solve the CAPTCHA in the headful window, "
                    f"and retry. If the asset returned a 403/empty body, your cf_clearance / DataDome cookie has expired."
                )
            
            # Additional check
            if os.path.exists(file_path):
                with open(file_path, "rb") as f:
                    head = f.read(20)
                if b"%PDF" not in head:
                    return f"Error: The downloaded payload ({os.path.getsize(file_path)} bytes) is NOT a valid PDF document (Missing %PDF header)."
                
            return file_path
        except Exception as e:
            return f"Error downloading PDF: {e}"

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not pdf_path or not os.path.exists(pdf_path) or "Error" in pdf_path or "Failed" in pdf_path:
            return f"Failed to download PDF: {pdf_path}"
            
        if not pdf_path.lower().endswith(".pdf"):
            return f"Downloaded file is not a PDF. Saved at: {pdf_path}"
            
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

scraper_instance = ScienceDirectScraper()
