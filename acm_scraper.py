import asyncio
import functools
from bs4 import BeautifulSoup
import os
import hashlib
import pymupdf4llm
from typing import Dict, List
import urllib.parse
import re
import sys
from mcp_logging import safe_stderr_print
from runtime_config import (
    allow_headful_fallback_for,
    ensure_runtime_environment,
    manual_verification_timeout_seconds,
    project_path,
)

print = safe_stderr_print
ensure_runtime_environment()

class ACMScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.camoufox_cm = None
        self.allow_headful_fallback = allow_headful_fallback_for("ACM")
        self.manual_verification_timeout = manual_verification_timeout_seconds()

    @staticmethod
    def _is_cloudflare_title(title: str) -> bool:
        title = (title or "").lower()
        return any(
            marker in title
            for marker in [
                "just a moment",
                "un momento",
                "cloudflare",
                "checking your browser",
                "attention required",
            ]
        )
        
    def _context_is_alive(self) -> bool:
        """True when self.context exists and its underlying browser is still connected.
        A previous CF-induced timeout can leave context/page non-None but referring to a
        torn-down browser process; subsequent calls then trip TargetClosedError."""
        if not self.context:
            return False
        try:
            browser = self.context.browser
            if browser is not None and not browser.is_connected():
                return False
            page = self.page
            if page is None or page.is_closed():
                return False
        except Exception:
            return False
        return True

    async def _reset_state(self) -> None:
        """Drop references to a wedged browser/context so the next _ensure_browser rebuilds it."""
        try:
            if self.camoufox_cm:
                await self.camoufox_cm.__aexit__(None, None, None)
        except Exception:
            pass
        self.camoufox_cm = None
        self.context = None
        self.page = None

    async def _ensure_browser(self, force_headful=False):
        if self.context and not self._context_is_alive():
            print("ACM context detected as closed; rebuilding from scratch.")
            await self._reset_state()
        if not self.context:
            print(f"Initializing ACM Persistent Browser Context (Headless: {not force_headful})...")
            from runtime_config import profile_path
            from scraper_utils import acquire_profile
            profile_dir = profile_path(".acm_profile")
            acquire_profile(profile_dir, "ACM")
            self._profile_dir = profile_dir

            # Include Firefox-style parent.lock / .parentlock so a crashed prior Camoufox
            # process can't keep us from launching.
            for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
                lfile = os.path.join(profile_dir, lock_name)
                if os.path.exists(lfile):
                    try: os.remove(lfile)
                    except: pass
                    
            self.playwright = None # Will not be used anymore
            from camoufox.async_api import AsyncCamoufox
            
            # Using OSINT stealth browser to evade hard blocks.
            # Keep ACM Camoufox-only: ordinary Chromium is less useful against ACM/Cloudflare.
            # ACM rides Cloudflare Turnstile + DataDome; image blocking is detectable.
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
            
            # Navigate to ACM to acquire initial cookies and bypass CF
            print("Navigating to ACM DL (Resolving protections)...")
            await self.page.goto("https://dl.acm.org", wait_until="domcontentloaded", timeout=45000)
            
            # Smart wait for Cloudflare
            cf_blocked = True
            for _ in range(15):
                try:
                    title = await self.page.title()
                    if not self._is_cloudflare_title(title):
                        cf_blocked = False
                        break
                except Exception:
                    pass # Navigation in progress
                await asyncio.sleep(1)
                
            if cf_blocked:
                if not force_headful:
                    if not self.allow_headful_fallback:
                        print("[Anti-Bot] ACM Cloudflare blocked headless mode; headful fallback is disabled.")
                        return
                    print("[Anti-Bot] ACM Cloudflare blocked in headless! Relaunching headful for manual check...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    return # Exit the current call as the recursive one handles the rest
                else:
                    print(f">>> PLEASE SOLVE ACM CLOUDFLARE IN THE CAMOUFOX WINDOW. Waiting up to {self.manual_verification_timeout}s... <<<")
                    solved = False
                    for _ in range(self.manual_verification_timeout):
                        try:
                            title = await self.page.title()
                            if not self._is_cloudflare_title(title):
                                print("[Anti-Bot] ACM Cloudflare passed! Proceeding...")
                                solved = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                    if not solved:
                        print("ACM Cloudflare not solved in time!")
            
            # Click accept cookies banner
            try:
                # Cookiebot or similar consent managers
                btn = self.page.locator('button:has-text("Allow all cookies"), button:has-text("Accept")')
                if await btn.count() > 0:
                    await btn.first.click()
                    print("Accepted ACM cookies.")
                    await asyncio.sleep(1)
            except Exception as e:
                pass
                
            print("ACM context initialized successfully.")

    async def _safe_page_content(self, retries: int = 8) -> str:
        last_error = None
        for attempt in range(retries):
            try:
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                return await self.page.content()
            except Exception as e:
                last_error = e
                if "navigating" not in str(e).lower() and attempt >= 2:
                    break
                await asyncio.sleep(1)
        raise last_error

    async def initialize(self):
        await self._ensure_browser()

    async def close(self):
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            try:
                await self.camoufox_cm.__aexit__(None, None, None)
            except Exception as e:
                print(f"ACM context close failed: {e}")
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        if getattr(self, "_profile_dir", None):
            from scraper_utils import release_profile
            release_profile(self._profile_dir)
            self._profile_dir = None

    async def search_papers(self, query: str, search_field: str = "AllField", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        await self._ensure_browser()
        
        # ACM handles pages by startPage (0-indexed). By default, pageSize is 20.
        page_size = 20
        start_page = start_index // page_size
        offset_in_first_page = start_index % page_size
        
        # Mappings
        field_map = {
            "全部": "AllField",
            "主题": "AllField",
            "篇名": "Title",
            "摘要": "Abstract",
            "作者": "Author"
        }
        field = field_map.get(search_field, search_field if search_field in ["AllField", "Title", "Abstract", "Author"] else "AllField")
        
        # ACM Digital Library accepts publication-name filtering via the AllField query using
        # the indexed field "PubName" (or "SeriesId" for proceedings series). The most reliable
        # cross-content-type filter is to AND PubName with the user query.
        journal_clean = (journal or "").strip()
        if journal_clean:
            quoted_journal = journal_clean.replace('"', '\\"')
            combined_q = f'"{quoted_journal}" AND ({query})'
        else:
            combined_q = query

        q_url = f"https://dl.acm.org/action/doSearch?{field}={urllib.parse.quote_plus(combined_q)}&startPage={start_page}&pageSize={page_size}"
        if journal_clean:
            # ACM also exposes a dedicated SeriesKey/PubName facet on the search URL. Appending
            # both narrows the result set further when the AllField AND does not match strictly.
            q_url += f"&PubName={urllib.parse.quote_plus(journal_clean)}"
        if source_type.lower() not in ["all", "全部", ""]:
            # Example param passing in ACM for filter
            # Although ACM usually adds something like &ConceptID= or filters via POST,
            # For simplicity, we can append to the query or use &content=...
            # A common way to filter by research-article: &ContentGroups=research-article
            # Or we just add it to the AllField query
            q_url += f"&ConceptID={source_type}"
            
        print(f"Navigating to ACM search: {q_url}")
        for attempt in range(2):
            res = await self.page.goto(q_url, wait_until="domcontentloaded")
            
            # Smart wait for Cloudflare
            cf_blocked = True
            for _ in range(15):
                try:
                    title = await self.page.title()
                    if not self._is_cloudflare_title(title):
                        cf_blocked = False
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
                
            if cf_blocked:
                # We assume headless if no force_headful is used in our code architecture
                if attempt == 0:
                    if not self.allow_headful_fallback:
                        return {
                            "total_results": "0",
                            "papers": [],
                            "error": "ACM Cloudflare verification blocked the headless MCP session. Add ACM to allow_headful_fallback_platforms in mcp_runtime_config.json for manual browser verification.",
                        }
                    print("[Anti-Bot] ACM Cloudflare blocked in headless search! Relaunching headful...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    continue
                else:
                    print(f">>> PLEASE SOLVE ACM CLOUDFLARE IN THE CAMOUFOX WINDOW. Waiting up to {self.manual_verification_timeout}s... <<<")
                    solved = False
                    for _ in range(self.manual_verification_timeout):
                        try:
                            title = await self.page.title()
                            if not self._is_cloudflare_title(title):
                                print("[Anti-Bot] ACM Cloudflare passed! Proceeding...")
                                solved = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                    if not solved:
                        return {
                            "total_results": "0",
                            "papers": [],
                            "error": "ACM Cloudflare verification was not completed before the manual verification timeout.",
                        }
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    break
            else:
                break
                
        await asyncio.sleep(3) # Wait for page contents to render fully
        html = await self._safe_page_content()
        soup = BeautifulSoup(html, "html.parser")
        
        total_str = "未知"
        hits_elem = soup.select_one(".hitsLength, .result__count, span.limit, div.issue-heading")
        if hits_elem:
            txt = hits_elem.text.strip()
            numbers = re.findall(r'[\d,]+', txt)
            if numbers:
                valid_nums = [n.replace(',', '') for n in numbers]
                total_str = str(max(int(n) for n in valid_nums if n.isdigit()))
                
        papers = []
        # issue-item cards contain the results
        items = soup.select(".issue-item")
        
        if not items:
            print("No items found. Current title is:", await self.page.title())
            html = await self._safe_page_content()
            if "cf-browser-verification" in html or "Just a moment" in html or "Un momento" in html:
                print("Still stuck in Cloudflare...")
                
        collected = 0        
        for i, item in enumerate(items):
            if i < offset_in_first_page:
                continue
                
            if collected >= limit:
                break
                
            title_tag = item.select_one("h5.issue-item__title a, h2.issue-item__title a, .hlFld-Title a")
            title = title_tag.text.strip() if title_tag else "N/A"
            link = "https://dl.acm.org" + title_tag['href'] if title_tag and title_tag.has_attr('href') else ""

            authors_tags = item.select(".author-name")
            authors = [a.text.strip() for a in authors_tags] if authors_tags else []
            author_str = ", ".join(authors) if authors else "N/A"

            item_text = item.get_text(" ", strip=True)
            year_match = re.search(r"\b(?:19|20)\d{2}\b", item_text)
            date_tag = item.select_one(".dot-separator span")
            date = year_match.group(0) if year_match else (date_tag.text.strip() if date_tag else "N/A")
            if date.lower().startswith("pages"):
                date = "N/A"

            # Content Type
            type_tag = item.select_one(".issue-heading")
            doc_type = type_tag.text.strip() if type_tag else "Article"

            # ACM detail URLs always carry the DOI (/doi/10.xxxx/yyyy). Surface it so callers
            # can disambiguate venues / dedupe against other platforms without an extra fetch.
            doi_match = re.search(r'/doi/(?:abs/|full/|pdf/)?(10\.[^/?#]+/[^/?#]+)', link)
            doi = doi_match.group(1) if doi_match else ""

            # Venue (journal / proceedings name) sits in the meta-line of the item card.
            venue_tag = item.select_one(".issue-item__detail a, .epub-section__title")
            venue_name = venue_tag.text.strip() if venue_tag else ""

            # Belt and suspenders: even with PubName + AllField filter, ACM occasionally
            # returns cross-publication hits when the journal token appears in body text.
            from scraper_utils import venue_matches
            if journal_clean and venue_name and not venue_matches(journal_clean, venue_name):
                continue

            uid = hashlib.md5(link.encode()).hexdigest()[:8]

            papers.append({
                "id": uid,
                "title": title,
                "author": author_str,
                "source": venue_name or "ACM Digital Library",
                "venue_name": venue_name,
                "doi": doi,
                "date": date,
                "db_type": doc_type,
                "detail_link": link,
            })
            collected += 1
            
        return {
            "total_results": total_str,
            "papers": papers
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        await self._ensure_browser()
        from scraper_utils import goto_with_retry
        await goto_with_retry(self.page, detail_url, wait_until="domcontentloaded")
        
        # Smart wait for Cloudflare
        for _ in range(15):
            try:
                title = await self.page.title()
                if not self._is_cloudflare_title(title):
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
            
        await asyncio.sleep(2)
        
        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")
        
        abstract_div = soup.select_one(".abstractSection, #abstract")
        abstract = abstract_div.text.strip() if abstract_div else "No abstract provided."
        
        # Keywords usually in .rlist--inline or .keywords
        keywords = []
        kw_tags = soup.select(".core-concept, .loa__concept, .chapter-concept")
        for tag in kw_tags:
            keywords.append(tag.text.strip())
            
        # doi is in the URL usually
        doi_match = re.search(r'doi/(10\.[^/]+/[^/]+)', detail_url)
        doi = doi_match.group(1) if doi_match else detail_url.split('/')[-1]
            
        return {
            "url": detail_url,
            "abstract": abstract.replace('\n', ' '),
            "keywords": keywords,
            "doi": doi
        }

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        await self._ensure_browser()
        os.makedirs(output_dir, exist_ok=True)
        
        doi_match = re.search(r'doi/(10\.[^/]+/[^?]+)', detail_url)
        doi = doi_match.group(1) if doi_match else detail_url.split('/')[-1]
        
        # ACM direct PDF link
        pdf_url = f"https://dl.acm.org/doi/pdf/{doi}"
        
        # Due to ACM's CF, normal urllib might fail. We use Playwright Request Context to download.
        # Alternatively, we can navigate to the pdf URL and wait for download event, but ACM's PDF url serves the stream.
        # So we can capture the response from a fetch evaluate inside page context, which carries the cookies.
        safe_name = doi.replace('/', '_').replace('.', '_')
        file_path = os.path.join(output_dir, f"acm_{safe_name}.pdf")
        
        print(f"Fetching PDF via secure Playwright context: {pdf_url}")
        
        import base64

        try:
            response = await self.context.request.get(pdf_url, headers={"Referer": detail_url}, timeout=60000)
            body = await response.body()
            if response.status == 200 and body.startswith(b"%PDF"):
                with open(file_path, "wb") as f:
                    f.write(body)
                return file_path
            print(f"ACM context request PDF failed: status={response.status}")
        except Exception as e:
            print(f"ACM context request PDF failed: {e}")

        try:
            base64_data = await self.page.evaluate("""async (url) => {
                const response = await fetch(url);
                if (!response.ok) throw new Error('Fetch failed: ' + response.status);
                const blob = await response.blob();
                return new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result.split(',')[1]);
                    reader.onerror = reject;
                    reader.readAsDataURL(blob);
                });
            }""", pdf_url)
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(base64_data))
        except Exception as e:
            return f"Error downloading ACM PDF: {e}"

        try:
            with open(file_path, "rb") as f:
                if f.read(4) != b"%PDF":
                    return f"Error downloading ACM PDF: payload is not a valid PDF: {file_path}"
        except Exception as e:
            return f"Error validating ACM PDF: {e}"
            
        return file_path

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

scraper_instance = ACMScraper()
