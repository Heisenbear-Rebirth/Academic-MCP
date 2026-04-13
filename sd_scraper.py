import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import os
import hashlib
import pymupdf4llm
from typing import Dict, List
import urllib.parse
import re

class ScienceDirectScraper:
    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        
    async def _ensure_browser(self):
        if not self.context:
            print("Initializing ScienceDirect Persistent Browser Context...")
            profile_dir = os.path.abspath(".sd_profile")
            
            import json
            # Force Chrome to avoid inline rendering, allowing native Datadome-bypassed Download events
            os.makedirs(os.path.join(profile_dir, "Default"), exist_ok=True)
            prefs_path = os.path.join(profile_dir, "Default", "Preferences")
            prefs = {"plugins": {"always_open_pdf_externally": True}, "download": {"prompt_for_download": False}}
            with open(prefs_path, "w") as f:
                json.dump(prefs, f)

            self.playwright = await async_playwright().start()
            
            # Using persistent context. We MUST use headless=False to bypass DataDome since it detects pure headless.
            # But we push it off-screen and start minimized so it never blocks the user's view.
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--window-position=0,0"],
                viewport={"width": 1280, "height": 720},
                accept_downloads=True
            )
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            
            print("Navigating to SD (Resolving protections)...")
            await self.page.goto("https://www.sciencedirect.com")
            
            # Smart wait for DataDome
            for _ in range(25):
                try:
                    title = await self.page.title()
                    if "Are you a robot" not in title and "请稍候" not in title and "Cloudflare" not in title:
                        break
                except Exception:
                    pass # Navigation in progress
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
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def search_papers(self, query: str, search_field: str = "qs", db_scope: str = "", source_type: str = "all", start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
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
        res = await self.page.goto(q_url, wait_until="domcontentloaded")
        
        # Smart wait for DataDome/CF
        for _ in range(20):
            try:
                title = await self.page.title()
                if "Are you a robot" not in title and "请稍候" not in title:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
            
        # Playwright automatic wait and selection
        try:
            await self.page.wait_for_selector("li.ResultItem", timeout=15000)
        except Exception:
            print("No items found. Current title is:", await self.page.title())
            html = await self.page.content()
            if "Are you a robot?" in html:
                print("STILL STUCK IN DATADOME! Manual intervention needed.")
                
        # Check Total Results
        total_str = "未知"
        total_loc = self.page.locator(".search-body-results-text, h1.search-body-results-text").first
        if await total_loc.count() > 0:
            txt = await total_loc.inner_text()
            m = re.search(r'([\d,]+)\s*results?', txt)
            if m:
                total_str = m.group(1).replace(",", "")
                
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
            
            date_loc = item.locator(".srctitle-date-fields span:nth-of-type(2)")
            if await date_loc.count() == 0:
                date_loc = item.locator(".srctitle-date-fields span:last-child")
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
                "source": "ScienceDirect",
                "date": date.strip(),
                "db_type": doc_type.strip(),
                "detail_link": link
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
                if "Are you a robot" not in title and "请稍候" not in title:
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
                print(f"Could not find the true PDF link with md5 token on the page for {safe_name}.")
                return "Error: Could not locate the native View PDF button."
                
            print(f"Discovered signed PDF URL: {pdf_url}")
            
            # We strictly bypass Datadome proxy walls by emulating a true navigation event!
            js_code = f"window.location.href = '{pdf_url}';"
            
            import re
            pii_match = re.search(r'/pii/([A-Z0-9]+)', detail_url)
            safe_name = pii_match.group(1) if pii_match else "unknown_pii"
            
            file_path = os.path.join(output_dir, f"sd_{safe_name}.pdf")
            
            async with self.page.expect_download(timeout=60000) as download_info:
                await self.page.evaluate(js_code)
                
            download = await download_info.value
            await download.save_as(file_path)
            
            # Additional check: Did we download a 51KB HTML blob indicating blockage?
            if os.path.getsize(file_path) < 100000:
                # Still failing, Elsevier is aggressively blocking our background traffic!
                return f"Error: The downloaded payload ({os.path.getsize(file_path)} bytes) appears to be an HTML captcha trap instead of the PDF."
                
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
