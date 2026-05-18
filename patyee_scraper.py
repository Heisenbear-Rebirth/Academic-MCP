import asyncio
import functools
import json
import os
import sys
from mcp_logging import safe_stderr_print
from typing import Dict, List
import urllib.parse
import re
from bs4 import BeautifulSoup
import aiohttp
from pdf_utils import convert_pdf_to_markdown
from runtime_config import allow_headful_fallback_for, ensure_runtime_environment, project_path

print = safe_stderr_print
ensure_runtime_environment()

class PatyeeScraper:
    def __init__(self):
        self.context = None
        self.page = None
        self.is_headful = False
        self.camoufox_cm = None
        self.playwright = None
        self.allow_headful_fallback = allow_headful_fallback_for("PATYEE")

    async def _launch_plain_playwright(self, profile_dir: str, force_headful: bool):
        print("Launching Patyee Playwright Chromium.")
        self.camoufox_cm = None
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=not force_headful,
            accept_downloads=True,
            ignore_https_errors=True,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    @staticmethod
    def _extract_year(date_text: str) -> int | None:
        if not date_text:
            return None
        match = re.search(r"(?:19|20)\d{2}", str(date_text))
        return int(match.group(0)) if match else None

    @staticmethod
    def _year_allowed(year: int | None, start_year: int = None, end_year: int = None) -> bool:
        if start_year is None and end_year is None:
            return True
        if year is None:
            return False
        if start_year is not None and year < start_year:
            return False
        if end_year is not None and year > end_year:
            return False
        return True

    @staticmethod
    def _as_text(value) -> str:
        if isinstance(value, list):
            return "; ".join(str(v).split("-", 1)[-1] for v in value if v)
        return str(value or "").strip()

    def _papers_from_search_payload(self, payload: dict, limit: int, start_year: int = None, end_year: int = None) -> Dict:
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}

        papers = []
        for item in data.get("result") or []:
            if len(papers) >= limit:
                break
            date_info = item.get("publication_date") or item.get("application_date") or ""
            if not self._year_allowed(self._extract_year(date_info), start_year=start_year, end_year=end_year):
                continue

            pn = item.get("publication_number_sear") or item.get("application_num_sear") or item.get("family_info", [""])[0]
            title = item.get("title_sear") or item.get("title_translate_sear") or "Unknown Title"
            applicant = self._as_text(item.get("applicants_normal_stat") or item.get("applicants_normal_all"))
            papers.append(
                {
                    "id": pn,
                    "title": title,
                    "author": applicant,
                    "source": f"Patyee ({pn})" if pn else "Patyee",
                    "date": date_info,
                    "db_type": item.get("patent_type_cn_stat") or "Patent",
                    "detail_link": f"https://www.patyee.com/innovationSpace/searchDetail?pn={urllib.parse.quote(pn)}" if pn else "",
                }
            )

        return {"total_results": data.get("total") or len(papers), "papers": papers}
        
    async def initialize(self):
        await self._ensure_browser()

    async def _ensure_browser(self, force_headful=False):
        if not self.context:
            print(f"Initializing Patyee Persistent Browser Context (Headless: {not force_headful})...")
            profile_dir = project_path(".patyee_profile")
            
            # Firefox-style parent.lock cleanup for Camoufox crashes.
            for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
                lfile = os.path.join(profile_dir, lock_name)
                if os.path.exists(lfile):
                    try: os.remove(lfile)
                    except: pass
            
            self.is_headful = force_headful
            await self._launch_plain_playwright(profile_dir, force_headful)
            
            print("Navigating to Patyee...")
            await self.page.goto("https://www.patyee.com/", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            
            # Smart wait for Anti-Bot or Login if needed
            title = await self.page.title()
            if "安全" in title or "验证" in title:
                print("Encountered Patyee security check!")
                if not force_headful:
                    if not self.allow_headful_fallback:
                        print("[Anti-Bot] Patyee security check blocked headless mode; headful fallback is disabled.")
                        return
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    return
            print("Patyee context initialized successfully.")

    async def search_papers(self, query: str, limit: int = 10, source_type: str = "all", start_year: int = None, end_year: int = None, **kwargs) -> Dict:
        await self._ensure_browser()
        
        # Build smart query
        smart_query = query
        
        # We append standard keywords to the query so Patyee's intelligent engine can parse it
        if source_type != "all" and source_type:
            smart_query += f" {source_type}"
            
        if start_year and end_year:
            smart_query += f" {start_year}-{end_year}"
        elif start_year:
            smart_query += f" {start_year}"
        elif end_year:
            smart_query += f" {end_year}"
            
        print(f"[Patyee] Searching for '{smart_query}'...")
        
        # Go to home page to use search box
        await self.page.goto("https://www.patyee.com/home", wait_until="domcontentloaded", timeout=60000)
        await self.page.wait_for_selector("#content-table", timeout=30000)
        await asyncio.sleep(2)
        
        # Click the contenteditable div directly, then type
        try:
            print("[Patyee] Evaluating search...")
            # Use javascript to set value and click
            escaped_query = json.dumps(smart_query, ensure_ascii=False)
            await self.page.evaluate(f'''() => {{
                let el = document.querySelector('#content-table');
                if(el) {{
                    el.innerText = {escaped_query};
                    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: {escaped_query} }}));
                }}
            }}''')
            await asyncio.sleep(1)
            async with self.page.expect_response(
                lambda resp: "/ourchem-patyee/patyee/search/search" in resp.url and resp.request.method == "POST",
                timeout=60000,
            ) as response_info:
                await self.page.evaluate('''() => {
                    let btn = document.querySelector('.pat-search');
                    if (btn) btn.click();
                }''')
            response = await response_info.value
            api_result = self._papers_from_search_payload(await response.json(), limit, start_year=start_year, end_year=end_year)
            if api_result["papers"]:
                print(f"[Patyee] API search found {api_result['total_results']} total results, returned {len(api_result['papers'])} patents.")
                return api_result
            
        except Exception as e:
            print(f"[Patyee] API path failed ({e}); falling back to HTML parsing.")

        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        try:
            await self.page.wait_for_selector(".pn-info-wrap", timeout=8000)
        except Exception:
            pass

        html = await self.page.content()
        soup = BeautifulSoup(html, 'html.parser')
        
        # Try to parse total results (e.g. class containing 'total' or pagination)
        total_results = 0
        try:
            total_text = soup.find(string=re.compile(r'1\s*/\s*([\d,]+)'))
            if total_text:
                m = re.search(r'/\s*([\d,]+)', total_text)
                if m:
                    total_results = int(m.group(1).replace(',', ''))
        except:
            pass
            
        items = soup.select('.pn-info-wrap')
        if total_results == 0:
            total_results = len(items)
            
        papers = []
        for item in items:
            if len(papers) >= limit:
                break
            data = {}
            for wrap in item.select('.item-wrap'):
                label = wrap.select_one('.label-wrap')
                value = wrap.select_one('.value-wrap')
                if label and value:
                    data[label.text.strip()] = value.text.strip()
                    
            pn = data.get('公开号', '') or data.get('公开(公告)号', '')
            title = data.get('专利名称', '') or data.get('专利标题', 'Unknown Title')
            applicant = data.get('申请人', '') or data.get('申请人(权利人)', '')
            date_info = data.get('公开日', '') or data.get('申请日', '')
            if not self._year_allowed(self._extract_year(date_info), start_year=start_year, end_year=end_year):
                continue
            
            detail_link = f"https://www.patyee.com/innovationSpace/searchDetail?pn={pn}"
            
            if pn:
                papers.append({
                    "id": pn,
                    "title": title,
                    "author": applicant,
                    "source": f"Patyee ({pn})",
                    "date": date_info,
                    "detail_link": detail_link
                })
                    
        print(f"[Patyee] Search found {total_results} total results, returned {len(papers)} patents.")
        return {
            "total_results": total_results,
            "papers": papers
        }

    async def get_paper_details(self, url: str) -> Dict:
        await self._ensure_browser()
        print(f"[Patyee] Fetching details for {url}")

        from scraper_utils import goto_with_retry
        await goto_with_retry(self.page, url, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(3)
        
        html = await self.page.content()
        soup = BeautifulSoup(html, 'html.parser')
        
        abstract = "No abstract found"
        keywords = []
        
        # Try the precise abstract extraction
        try:
            for el in soup.find_all(string=lambda t: t and '摘要' == t.strip()):
                if el.parent:
                    sibling = el.parent.find_next_sibling()
                    if sibling and sibling.name == 'div':
                        abstract = sibling.get_text(separator=' ', strip=True)
                        break
        except Exception as e:
            print("Failed precise abstract parsing:", e)

        # Fallback if precise extraction failed
        if abstract == "No abstract found":
            text_content = soup.get_text(separator=' ', strip=True)
            if "摘要" in text_content:
                try:
                    abs_header = soup.find(string=re.compile(r'^摘要$'))
                    if abs_header and abs_header.parent:
                        next_node = abs_header.parent.find_next_sibling()
                        if next_node:
                            abstract = next_node.get_text(separator=' ', strip=True)
                except:
                    pass
                
        # Also try extracting IPC/CPC
        try:
            ipc_nodes = soup.find_all(string=re.compile(r'IPC分类号:'))
            if ipc_nodes and ipc_nodes[0].parent:
                ipc_text = ipc_nodes[0].parent.parent.get_text(strip=True)
                keywords.append(ipc_text)
        except:
            pass

        pn = ""
        if "pn=" in url:
            pn = url.split("pn=")[1].split("&")[0]

        return {
            "abstract": abstract,
            "keywords": keywords,
            "doi": pn
        }

    async def download_paper(self, url: str, output_dir: str) -> str:
        await self._ensure_browser()
        print(f"[Patyee] Navigating to download: {url}")
        os.makedirs(output_dir, exist_ok=True)
        
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(4)
        
        try:
            print("[Patyee] Clicking '专利原文' tab...")
            # Look for the Original Patent text tab
            tabs = await self.page.locator('li').all()
            clicked = False
            for tab in tabs:
                text = await tab.inner_text()
                if "专利原文" in text:
                    await tab.click()
                    clicked = True
                    break
                    
            if not clicked:
                raise Exception("Could not find '专利原文' tab.")
                
            await asyncio.sleep(4)
            
            # Patyee exposes the raw OSS URL in the HTML directly
            html = await self.page.content()
            match = re.search(r'file=(https?[^&"]+)', html)
            if not match:
                raise Exception("Could not extract PDF URL from iframe or viewer link.")
                
            pdf_url = urllib.parse.unquote(match.group(1))
            print(f"[Patyee] Intercepted Direct PDF URL: {pdf_url[:80]}...")
            
            # Download it using aiohttp
            pn = url.split("pn=")[1].split("&")[0] if "pn=" in url else "patyee_doc"
            file_name = f"{pn}.pdf"
            file_path = os.path.join(output_dir, file_name)
            
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(pdf_url, ssl=False, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            with open(file_path, "wb") as f:
                                f.write(content)
                            print(f"[Patyee] Downloaded PDF successfully: {file_path}")
                            return file_path
                        else:
                            raise Exception(f"Failed to download PDF. Status: {resp.status}")
                except Exception as dl_e:
                    print(f"[Patyee] Direct download failed ({dl_e}), trying playwright download...")
                    # Fallback to playwright
                    await self.page.goto(pdf_url)
                    await asyncio.sleep(10) # Let it load
                    return "Fallback not implemented fully, manual download required"
                        
        except Exception as e:
            print(f"[Patyee] Download failed: {e}")
            raise e

    async def read_paper_content(self, url: str, output_dir: str) -> tuple[str, str]:
        os.makedirs(output_dir, exist_ok=True)
        try:
            pdf_path = await self.download_paper(url, output_dir)
        except Exception as e:
            print(f"Error downloading patent: {e}")
            raise e
            
        print(f"Parsing downloaded PDF with checked converter: {pdf_path}")
        converted = convert_pdf_to_markdown(pdf_path, output_dir, image_format="png")
        if converted.image_only:
            print("PDF has no extractable text; generated image-only Markdown.")
        print(f"Markdown generation complete. Saved to: {converted.md_path}")
        return converted.md_path, converted.preview

    async def close(self):
        try:
            if self.context:
                await self.context.close()
                self.context = None
            if self.camoufox_cm:
                self.camoufox_cm = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            print("Patyee Context closed cleanly.")
        except Exception as e:
            print(f"Error closing Patyee context: {e}")
