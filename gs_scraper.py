import os
import asyncio
from typing import List, Dict, Optional
from playwright.async_api import async_playwright
import bs4
import hashlib
import urllib.parse
import re

class GoogleScholarScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
    
    async def initialize(self):
        import random
        self.playwright = await async_playwright().start()
        profile_dir = os.path.join(os.getcwd(), ".gs_profile")
        
        ua_list = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
        ]
        
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-minimized",
                "--window-position=0,0"
            ],
            user_agent=random.choice(ua_list),
            accept_downloads=True,
            viewport={"width": 1280, "height": 800}
        )
        self.page = await self.context.new_page()

    async def close(self):
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

    async def search_papers(self, query: str, search_field: str = "all", db_scope: str = "", source_type: str = "all", start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        import random
        if not self.page:
            await self.initialize()
            
        encoded_query = urllib.parse.quote(query)
        # start_index maps directly to GS 'start' param (0, 10, 20...)
        base_url = f"https://scholar.google.com/scholar?hl=en&q={encoded_query}&start={start_index}"
        
        if sort_by == "date_desc":
            base_url += "&scisbd=1"
            
        if start_year:
            base_url += f"&as_ylo={start_year}"
        if end_year:
            base_url += f"&as_yhi={end_year}"
            
        # Human-like delay strategy to avoid fast CAPTCHA blocks
        await asyncio.sleep(random.uniform(2.5, 6.0))
        
        try:
            await self.page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 3.0))
            
            # Simulate human reading / scrolling
            for _ in range(random.randint(2, 4)):
                await self.page.mouse.wheel(0, random.randint(200, 600))
                await asyncio.sleep(random.uniform(0.5, 1.5))
                
        except Exception as e:
            print(f"Error navigating to Google Scholar: {e}")
            return {"total_results": "0", "papers": []}
            
        html = await self.page.content()
        soup = bs4.BeautifulSoup(html, 'html.parser')
        
        # Check for captcha
        if soup.select_one("form#captcha-form, div.g-recaptcha, h1:-soup-contains('One more step')"):
            return {
                "total_results": "CAPTCHA (403)",
                "papers": [{
                    "id": "errCaptcha",
                    "title": "Google Scholar requested CAPTCHA. Please run gs_auth.py manually.",
                    "author": "System",
                    "source": "GS Blocked",
                    "date": "N/A",
                    "db_type": "Error",
                    "detail_link": "N/A"
                }]
            }

        total_match = soup.select_one("div#gs_ab_md")
        total_text = total_match.text if total_match else "未知"
        # usually looks like "About 1,230,000 results (0.05 sec)"
        m = re.search(r'([\d,]+)\s+results', total_text)
        total_results = m.group(1).replace(',', '') if m else "未知"
        
        results = []
        rows = soup.select("div.gs_ri")
        
        collected = 0
        for row in rows:
            if collected >= limit:
                break
                
            title_elem = row.select_one("h3.gs_rt a")
            if not title_elem:
                continue
                
            title = title_elem.text.strip()
            detail_link = title_elem.get("href", "")
            
            author_pub_elem = row.select_one("div.gs_a")
            author_pub_str = author_pub_elem.text.replace('\xa0', ' ').strip() if author_pub_elem else "N/A"
            
            # Author pub string format: "Author1, Author2 - Journal Name, 2023 - PublisherSite"
            author = "N/A"
            date = "N/A"
            source = "Google Scholar"
            
            if " - " in author_pub_str:
                parts = author_pub_str.split(" - ")
                author = parts[0]
                if len(parts) > 1:
                    date_m = re.search(r'\b(19|20)\d{2}\b', parts[1])
                    if date_m:
                        date = date_m.group()
                    source = parts[-1].strip()
                    if source.startswith("…"):
                        source_guess = urllib.parse.urlparse(detail_link).netloc
                        source = source_guess if source_guess else source
            else:
                author = author_pub_str
                
            snippet_elem = row.select_one("div.gs_rs")
            snippet = snippet_elem.text.replace('\n', ' ').strip() if snippet_elem else ""
            
            uid = hashlib.md5(detail_link.encode()).hexdigest()[:8]
            
            results.append({
                "id": uid,
                "title": title,
                "author": author,
                "source": "GS: " + source,
                "date": date,
                "db_type": "GS Aggregated",
                "detail_link": detail_link,
                "_gs_snippet": snippet
            })
            collected += 1
            
        return {
            "total_results": total_results,
            "papers": results
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        # Google scholar direct links point to the publisher.
        # We just return advising the LLM to use the specific platform link directly.
        return {
            "url": detail_url,
            "abstract": "Google Scholar acts as a router. Please inspect the source URL. If it's an IEEE/ACM/SD/CNKI link, use their native MCP scraper or directly download using 'read_paper_content' with the specific platform.",
            "keywords": ["GoogleScholar", "Redirected"],
            "doi": "Fetch via Native Tool."
        }

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        return "Not supported via GS. Please use native read_paper_content with target platform (e.g., IEEE, SD) on this URL."

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        return "Not supported via GS. Please use native read_paper_content with target platform (e.g., IEEE, SD) on this URL."

scraper_instance = GoogleScholarScraper()
