import os
import asyncio
from typing import List, Dict, Optional
from playwright.async_api import async_playwright
import bs4
import pymupdf4llm
import re
import hashlib

class IEEEScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
    
    async def initialize(self):
        self.playwright = await async_playwright().start()
        # Launch headless Chromium
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            accept_downloads=True
        )
        self.page = await self.context.new_page()

    async def close(self):
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def search_papers(self, query: str, search_field: str = "All", db_scope: str = "", source_type: str = "all", start_index: int = 0, limit: int = 10) -> Dict:
        if not self.page:
            await self.initialize()
            
        import urllib.parse
        encoded_query = urllib.parse.quote(query)
        base_url = f"https://ieeexplore.ieee.org/search/searchresult.jsp?newsearch=true&queryText={encoded_query}"
        
        await self.page.goto(base_url, wait_until="networkidle")
        await asyncio.sleep(4)
        
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
                await asyncio.sleep(4)
            except Exception as e:
                print(f"Pagination error jumping to page {target_start_page}: {e}")

        results = []
        collected = 0
        
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
                    
                author_elem = row.select_one(".author, p.author")
                author = author_elem.text.strip() if author_elem else "N/A"
                author = author.replace("\n", "").replace("  ", "")
                
                # Try finding publisher info
                publisher_elem = row.select_one("div.publisher-info-container, span:-soup-contains('Publisher:')")
                source = publisher_elem.text.strip() if publisher_elem else "IEEE"
                
                # Year info
                year_elem = text_elem = row.select_one("div.description, div.publisher-info-container")
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
                    "date": date,
                    "db_type": db_type,
                    "detail_link": detail_link
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
            
        await self.page.goto(detail_url, wait_until="networkidle")
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
        
        # Inject Javascript to fetch the PDF and save it as a Blob, then trigger HTML5 download
        # This keeps the request in context containing the Tongji cookies!
        js_code = f"""
        async () => {{
            const response = await fetch('{pdf_url}');
            if (!response.ok) throw new Error('PDF Network response was not ok');
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = 'ieee_{arnumber}.pdf';
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
        }}
        """
        try:
            async with self.page.expect_download(timeout=60000) as download_info:
                await self.page.evaluate(js_code)
            download = await download_info.value
            file_path = os.path.join(output_dir, download.suggested_filename)
            await download.save_as(file_path)
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
