import os
import asyncio
import functools
import sys
from mcp_logging import safe_stderr_print
from typing import List, Dict, Optional
from playwright.async_api import async_playwright
import bs4
import hashlib
import urllib.parse
import re

print = safe_stderr_print

class GoogleScholarScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
    
    async def initialize(self, force_headful=False):
        import random
        # Clean obsolete lockfiles to prevent context launch crashing
        profile_dir = os.path.join(os.getcwd(), ".gs_profile")
        for lock_name in ["lockfile", "SingletonLock"]:
            lfile = os.path.join(profile_dir, lock_name)
            if os.path.exists(lfile):
                try: os.remove(lfile)
                except: pass

        self.playwright = None # Will not be used anymore
        from camoufox.async_api import AsyncCamoufox
        
        self.camoufox_cm = AsyncCamoufox(
            headless=not force_headful,
            user_data_dir=profile_dir,
            persistent_context=True,
            humanize=True,
            geoip=True
        )
        self.context = await self.camoufox_cm.__aenter__()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def close(self):
        if self.page:
            await self.page.close()
            self.page = None
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            await self.camoufox_cm.__aexit__(None, None, None)
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None

    async def search_papers(self, query: str, search_field: str = "all", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        import random
        if not self.page:
            await self.initialize()
            
        encoded_query = urllib.parse.quote(query)
        # start_index maps directly to GS 'start' param (0, 10, 20...)
        if journal:
            base_url = f"https://scholar.google.com/scholar?hl=en&as_q={encoded_query}&start={start_index}&as_publication={urllib.parse.quote(journal)}"
        else:
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
            # We assume headless if no explicit tracking is used
            if not hasattr(self, 'is_headful'):
                print("[Anti-Bot] GS CAPTCHA detected! Relaunching headful for manual check...")
                await self.close()
                self.is_headful = True
                await self.initialize(force_headful=True)
                return await self.search_papers(query, search_field, db_scope, source_type, journal, start_year, end_year, sort_by, start_index, limit)
            else:
                print(">>> PLEASE SOLVE GS CAPTCHA IN BROWSER WINDOW. Waiting up to 60s... <<<")
                solved = False
                for _ in range(60):
                    html = await self.page.content()
                    soup = bs4.BeautifulSoup(html, 'html.parser')
                    if not soup.select_one("form#captcha-form, div.g-recaptcha, h1:-soup-contains('One more step')"):
                        solved = True
                        break
                    await asyncio.sleep(1)
                
                if not solved:
                    return {
                        "total_results": "CAPTCHA (403)",
                        "papers": [{
                            "id": "errCaptcha",
                            "title": "Google Scholar requested CAPTCHA but it was not solved.",
                            "author": "System",
                            "source": "GS Blocked",
                            "date": "N/A",
                            "db_type": "Error",
                            "detail_link": "N/A"
                        }]
                    }
                else: # refresh soup after solve
                    html = await self.page.content()
                    soup = bs4.BeautifulSoup(html, 'html.parser')
            
        if "We're sorry" in html or "but your computer or network may be sending automated queries" in html:
            return {
                "total_results": "IP_BANNED",
                "papers": [{
                    "id": "errIpBan",
                    "title": "Google Scholar has HARD-BANNED this IP address. Please change your VPN/Proxy node or wait 24 hours.",
                    "author": "System",
                    "source": "GS Blocked",
                    "date": "N/A",
                    "db_type": "Error",
                    "detail_link": "N/A"
                }]
            }

        total_match = soup.select_one("div#gs_ab_md")
        total_text = total_match.text if total_match else "未知"
        # usually looks like "About 1,230,000 results (0.05 sec)" or "找到约 1,230,000 条结果"
        m = re.search(r'([\d,]+)\s*(?:results|条结果)', total_text)
        total_results = m.group(1).replace(',', '') if m else "未知"
        
        results = []
        rows = soup.select("div.gs_ri, div.gs_r")
        
        collected = 0
        seen_links = set()
        
        for row in rows:
            if collected >= limit:
                break
                
            title_elem = row.select_one("h3.gs_rt a") or row.select_one("h3.gs_rt")
            if not title_elem:
                continue
                
            title = title_elem.text.strip()
            detail_link = title_elem.get("href", "") if title_elem.name == "a" else ""
            
            if detail_link in seen_links and detail_link:
                continue
            seen_links.add(detail_link)
            
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
