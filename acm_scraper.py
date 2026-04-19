import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import os
import hashlib
import pymupdf4llm
from typing import Dict, List
import urllib.parse
import re

class ACMScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        
    async def _ensure_browser(self, force_headful=True):
        if not self.context:
            print(f"Initializing ACM Persistent Browser Context (Headless: {not force_headful})...")
            profile_dir = os.path.abspath(".acm_profile")
            
            for lock_name in ["lockfile", "SingletonLock"]:
                lfile = os.path.join(profile_dir, lock_name)
                if os.path.exists(lfile):
                    try: os.remove(lfile)
                    except: pass
                    
            self.playwright = None # Will not be used anymore
            from camoufox.async_api import AsyncCamoufox
            
            # Using OSINT stealth browser to evade hard blocks
            self.camoufox_cm = AsyncCamoufox(
                headless=not force_headful,
                user_data_dir=profile_dir,
                persistent_context=True,
                humanize=True,
                geoip=True
            )
            self.context = await self.camoufox_cm.__aenter__()
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            
            # Navigate to ACM to acquire initial cookies and bypass CF
            print("Navigating to ACM DL (Resolving protections)...")
            await self.page.goto("https://dl.acm.org")
            
            # Smart wait for Cloudflare
            cf_blocked = True
            for _ in range(15):
                try:
                    title = await self.page.title()
                    if "Just a moment" not in title and "请稍候" not in title and "Cloudflare" not in title:
                        cf_blocked = False
                        break
                except Exception:
                    pass # Navigation in progress
                await asyncio.sleep(1)
                
            if cf_blocked:
                if not force_headful:
                    print("[Anti-Bot] ACM Cloudflare blocked in headless! Relaunching headful for manual check...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    return # Exit the current call as the recursive one handles the rest
                else:
                    print(">>> PLEASE WAIT OR SOLVE ACM CLOUDFLARE IN BROWSER WINDOW. Waiting up to 60s... <<<")
                    solved = False
                    for _ in range(60):
                        try:
                            title = await self.page.title()
                            if "Just a moment" not in title and "请稍候" not in title and "Cloudflare" not in title:
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

    async def initialize(self):
        await self._ensure_browser()

    async def close(self):
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            await self.camoufox_cm.__aexit__(None, None, None)
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None

    async def search_papers(self, query: str, search_field: str = "AllField", db_scope: str = "", source_type: str = "all", start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
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
        
        q_url = f"https://dl.acm.org/action/doSearch?{field}={urllib.parse.quote_plus(query)}&startPage={start_page}&pageSize={page_size}"
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
                    if "Just a moment" not in title and "请稍候" not in title and "Cloudflare" not in title:
                        cf_blocked = False
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
                
            if cf_blocked:
                # We assume headless if no force_headful is used in our code architecture
                if attempt == 0:
                    print("[Anti-Bot] ACM Cloudflare blocked in headless search! Relaunching headful...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    continue
                else:
                    print(">>> PLEASE WAIT OR SOLVE ACM CLOUDFLARE IN BROWSER WINDOW. Waiting 60s... <<<")
                    solved = False
                    for _ in range(60):
                        try:
                            title = await self.page.title()
                            if "Just a moment" not in title and "请稍候" not in title and "Cloudflare" not in title:
                                print("[Anti-Bot] ACM Cloudflare passed! Proceeding...")
                                solved = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
            else:
                break
                
        await asyncio.sleep(3) # Wait for page contents to render fully
        html = await self.page.content()
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
            html = await self.page.content()
            if "cf-browser-verification" in html or "Just a moment" in html:
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
            
            date_tag = item.select_one(".dot-separator span")
            date = date_tag.text.strip() if date_tag else "N/A"
            
            # Content Type
            type_tag = item.select_one(".issue-heading")
            doc_type = type_tag.text.strip() if type_tag else "Article"
            
            uid = hashlib.md5(link.encode()).hexdigest()[:8]
            
            papers.append({
                "id": uid,
                "title": title,
                "author": author_str,
                "source": "ACM Digital Library",
                "date": date,
                "db_type": doc_type,
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
        
        # Smart wait for Cloudflare
        for _ in range(15):
            try:
                title = await self.page.title()
                if "Just a moment" not in title and "请稍候" not in title and "Cloudflare" not in title:
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
        
        # Inject Javascript to fetch the blob and return it as base64 string
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
        
        import base64
        with open(file_path, "wb") as f:
            f.write(base64.b64decode(base64_data))
            
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
