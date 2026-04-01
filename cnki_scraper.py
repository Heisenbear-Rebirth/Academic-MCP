import os
import asyncio
from typing import List, Dict, Optional
from playwright.async_api import async_playwright
import bs4
import pymupdf4llm

class CNKIScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
    
    async def initialize(self):
        self.playwright = await async_playwright().start()
        # Launch headless Chromium
        self.browser = await self.playwright.chromium.launch(headless=True)
        # We use a user agent to look like a normal browser
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

    async def search_papers(self, query: str, search_field: str = "主题", db_scope: str = "总库", source_type: str = "all", start_index: int = 0, limit: int = 10) -> Dict:
        """
        search_papers with pagination, precise filtering, and search field support.
        """
        if not self.page:
            await self.initialize()
            
        await self.page.goto("https://www.cnki.net/", wait_until="networkidle")
        
        # Select Search Field if not '主题'
        if search_field != "主题":
            try:
                # Try to click the dropdown. Common class names on CNKI for this are .search-item, .sort-default, .choose-type
                dropdown = self.page.locator(".search-item, .sort-default, .choose-type, #DBField").first
                if await dropdown.count() > 0:
                    await dropdown.click()
                    await asyncio.sleep(0.5)
                    # Click the option
                    option = self.page.locator(f"li:text-is('{search_field}'), a:text-is('{search_field}')").filter(visible=True).first
                    if await option.count() > 0:
                        await option.click()
            except Exception as e:
                print(f"Error selecting search field: {e}")
        
        # Fill search input
        search_box = self.page.locator("input.search-input, #txt_search")
        if await search_box.count() > 0:
            await search_box.first.fill(query)
            await self.page.locator("input.search-btn").first.click()
        else:
            search_box = self.page.get_by_role("textbox", name="中文文献、外文文献")
            if await search_box.count() > 0:
                await search_box.fill(query)
                search_btn = self.page.get_by_text("检索", exact=True)
                await search_btn.click()
            
        # Wait for results page
        try:
            await self.page.wait_for_selector("table.result-table-list tbody tr", timeout=15000)
        except Exception:
            return []

        # Wait a moment for dynamic panels to load
        await asyncio.sleep(2)

        # 1. DB Scope filter ("总库", "中文", "外文")
        if db_scope in ["中文", "外文"]:
            # CNKI places these tabs usually at the top of the search result filter area.
            # Using precise text matching.
            db_tab = self.page.locator(f"a:text-is('{db_scope}'), span:text-is('{db_scope}')").first
            if await db_tab.count() > 0:
                await db_tab.click()
                await asyncio.sleep(2)
                await self.page.wait_for_selector("table.result-table-list tbody tr", timeout=10000)

        # 2. Source Type filter ("学术期刊", "学位论文", "会议", etc.)
        if source_type != "all":
            # Finding the category link
            filter_link = self.page.locator(f"a:has-text('{source_type}')").first
            try:
                if await filter_link.count() > 0:
                    await filter_link.click()
                    await asyncio.sleep(2)
                    await self.page.wait_for_selector("table.result-table-list tbody tr", timeout=10000)
            except Exception:
                pass # Continue if category not clickable

        # Parse the table & extract total count
        html = await self.page.content()
        soup = bs4.BeautifulSoup(html, "html.parser")
        
        # 尝试提取命中数量 (例如 "3760")
        total_count = "未知"
        import re
        try:
            # 1. 直接尝试通过常见类名寻找
            count_elem = soup.select_one(".search-count, .res-count em, .res-count span, li.active span.count, .group-list li.active span, .search-type li.active span")
            if count_elem:
                match = re.search(r'[\d,]+', count_elem.text)
                if match:
                    total_count = match.group(0).replace(",", "")
            
            # 2. 从选中状态的分类标签提取文本数字
            if total_count == "未知":
                active_li = soup.select("li.active, a.active")
                for li in active_li:
                    text_content = li.text.replace('\n', '').strip()
                    if db_scope in text_content or source_type in text_content:
                        match = re.search(r'\(?([\d,]+)\)?', text_content.replace(db_scope, '').replace(source_type, ''))
                        if match:
                            total_count = match.group(1).replace(",", "")
                            break
                            
            # 3. 页面粗暴全局正则匹配 "共找到 xxx 条" 或 "找到 xxx 条"
            if total_count == "未知":
                html_text = soup.text
                match = re.search(r'找到\s*([\d,]+)\s*条', html_text) or re.search(r'共\s*([\d,]+)\s*条', html_text)
                if match:
                    total_count = match.group(1).replace(",", "")
        except Exception as e:
            print("Extract count error:", e)
        
        rows = soup.select("table.result-table-list tbody tr")
        
        results = []
        collected = 0
        current_index = start_index

        target_start_page = (start_index // 20) + 1
        offset_in_first_page = start_index % 20

        # Jump to target start page if needed
        # We loop clicking "下一页" or the specific page number
        if target_start_page > 1:
            try:
                # Try clicking direct page number
                page_btn = self.page.locator(f"a#Page_{target_start_page}, a.page:has-text('{target_start_page}')").first
                if await page_btn.count() > 0:
                    await page_btn.click()
                    await asyncio.sleep(2)
                    await self.page.wait_for_selector("table.result-table-list tbody tr", timeout=10000)
                else:
                    # Fallback simulating next page clicks
                    for _ in range(target_start_page - 1):
                        next_btn = self.page.locator("a#PageNext").first
                        if await next_btn.count() > 0:
                            await next_btn.click()
                            await asyncio.sleep(2)
                            await self.page.wait_for_selector("table.result-table-list tbody tr", timeout=10000)
            except Exception as e:
                print(f"Pagination error jumping to page {target_start_page}: {e}")
        
        # Now collect items spanning across pages as necessary
        while collected < limit:
            html = await self.page.content()
            soup = bs4.BeautifulSoup(html, "html.parser")
            rows = soup.select("table.result-table-list tbody tr")
            
            if not rows:
                break # No more results
                
            startIndexToProcess = offset_in_first_page if collected == 0 else 0
            
            for row in rows[startIndexToProcess:]:
                if collected >= limit:
                    break
                    
                title_elem = row.select_one("td.name a")
                title = title_elem.text.strip() if title_elem else "N/A"
                detail_link = title_elem.get("href") if title_elem else ""
                if detail_link and detail_link.startswith("/"):
                    detail_link = "https://kns.cnki.net" + detail_link
                    
                author_elem = row.select_one("td.author")
                author = author_elem.text.strip() if author_elem else "N/A"
                author = author.replace("\n", "").replace(" ", "")
                
                source_elem = row.select_one("td.source")
                source = source_elem.text.strip() if source_elem else "N/A"
                source = source.replace("\n", "").replace(" ", "")
                
                date_elem = row.select_one("td.date")
                date = date_elem.text.strip() if date_elem else "N/A"
                
                data_elem = row.select_one("td.data")
                db_type = data_elem.text.strip() if data_elem else "N/A"
                
                import hashlib
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
                    next_btn = self.page.locator("a#PageNext").first
                    if await next_btn.count() > 0:
                        await next_btn.click()
                        await asyncio.sleep(2)
                        await self.page.wait_for_selector("table.result-table-list tbody tr", timeout=10000)
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
            
        await self.page.goto(detail_url, wait_until="domcontentloaded")
        
        try:
            await asyncio.sleep(2)
            html = await self.page.content()
            soup = bs4.BeautifulSoup(html, "html.parser")
            
            abstract_elem = soup.select_one("span#ChDivSummary")
            abstract = abstract_elem.text.strip() if abstract_elem else "No abstract found."
            
            keywords_elems = soup.select('p.keywords a')
            keywords = [k.text.strip().strip(';') for k in keywords_elems] if keywords_elems else []
            
            doi = ""
            for p in soup.select("ul.docinfo li, div.docinfo p"):
                text = p.text.strip()
                if "DOI：" in text or "DOI:" in text:
                    doi = text.replace("DOI：", "").replace("DOI:", "").strip()
                    break

            return {
                "url": detail_url,
                "abstract": abstract,
                "keywords": keywords,
                "doi": doi
            }
        except Exception as e:
            return {"error": str(e)}

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        """Download PDF from the detail page to output_dir."""
        if not self.page:
            await self.initialize()
            
        os.makedirs(output_dir, exist_ok=True)
        await self.page.goto(detail_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        try:
            pdf_btn = self.page.locator("#pdfDown, a:has-text('PDF下载')").first
            if await pdf_btn.count() == 0:
                pdf_btn = self.page.locator("a#DownLoadParts, a:has-text('整本下载')").first
                
            if await pdf_btn.count() > 0:
                async with self.page.expect_download(timeout=60000) as download_info:
                    await pdf_btn.click()
                download = await download_info.value
                file_path = os.path.join(output_dir, download.suggested_filename)
                await download.save_as(file_path)
                return file_path
            else:
                return "Download button not found."
        except Exception as e:
            return f"Error downloading: {str(e)}"

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        """
        Downloads PDF and converts to HD MD storing extracted images.
        """
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not pdf_path or not os.path.exists(pdf_path):
            return f"Failed to download PDF: {pdf_path}"
            
        if not pdf_path.lower().endswith(".pdf"):
            return f"Downloaded file is not a PDF, conversion not supported. Saved at: {pdf_path}"
            
        # Parse PDF to MD
        try:
            images_dir = os.path.join(output_dir, "images")
            os.makedirs(images_dir, exist_ok=True)
            
            # Using pymupdf4llm for HD extraction
            md_text = pymupdf4llm.to_markdown(
                doc=pdf_path,
                write_images=True,
                image_path=images_dir
            )
            
            # Save MD text alongside
            base_name = os.path.basename(pdf_path)
            md_filename = os.path.splitext(base_name)[0] + ".md"
            md_path = os.path.join(output_dir, md_filename)
            
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md_text)
                
            # Provide snippet as answer
            snippet = md_text[:1000] + "\n...(More Content Available)"
            res = (f"=== 转换成功 ===\n"
                   f"PDF已下载: {pdf_path}\n"
                   f"Markdown及高清图像已保存至目录: {output_dir}\n"
                   f"完整的MD文件路径: {md_path}\n"
                   f"--- 以下为前1000字预览 ---\n\n{snippet}")
            return res
        except Exception as e:
            return f"PDF downloaded to {pdf_path} but Markdown conversion failed: {str(e)}"

# Singleton scraper instance
scraper_instance = CNKIScraper()
