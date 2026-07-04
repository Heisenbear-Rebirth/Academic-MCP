import asyncio
import os
import re
from typing import Dict
from urllib.parse import urlencode, urljoin
import urllib.request

import bs4
import pymupdf4llm
import hashlib
from mcp_logging import safe_stderr_print
from scraper_utils import remember_downloaded_pdf, reuse_downloaded_pdf

print = safe_stderr_print

ARXIV_FRONTEND_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_text_once(url: str, *, referer: str = "https://arxiv.org/", timeout: int = 20) -> str:
    headers = dict(ARXIV_FRONTEND_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _fetch_text(url: str, *, referer: str = "https://arxiv.org/") -> str:
    last_error = None
    for timeout in (15, 30):
        try:
            return _fetch_text_once(url, referer=referer, timeout=timeout)
        except Exception as e:
            last_error = e
    raise last_error


def _year_allowed(year: int | None, start_year: int = None, end_year: int = None) -> bool:
    if year is None:
        return True
    if start_year is not None and year < int(start_year):
        return False
    if end_year is not None and year > int(end_year):
        return False
    return True


def _extract_first_year(text: str) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", text or "")
    return int(m.group(0)) if m else None


class ArxivScraper:
    def __init__(self):
        self.context = None
        self.page = None
        self.camoufox_cm = None
        self._pdf_cache = {}
        
    async def initialize(self, force_headful=False):
        if not self.context:
            print(f"Initializing ArXiv Browser Context (Headless: {not force_headful})...")
            from camoufox.async_api import AsyncCamoufox
            # ArXiv has no WAF, so we can safely strip images for faster search-page loads.
            # i_know_what_im_doing silences Camoufox's WAF-detection warning since we
            # explicitly only do this on the WAF-free platform.
            self.camoufox_cm = AsyncCamoufox(
                headless=not force_headful,
                os="windows",
                block_images=not force_headful,
                humanize=True,
                i_know_what_im_doing=True,
            )
            self.context = await self.camoufox_cm.__aenter__()
            self.page = await self.context.new_page()

    async def close(self):
        if self.camoufox_cm:
            await self.camoufox_cm.__aexit__(None, None, None)
            self.camoufox_cm = None
            self.context = None
            self.page = None

    async def search_papers(self, query: str, search_field: str = "all", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        # Mapping to arXiv advanced search fields
        field_map = {
            "全部": "all", "主题": "all",
            "篇名": "title", "摘要": "abstract", "作者": "author", "关键词": "all",
            "ti": "title", "au": "author", "abs": "abstract", "cat": "cross_list_category",
        }
        prefix = field_map.get(search_field, search_field if search_field in ["all", "title", "author", "abstract"] else "all")

        order_map = {
            "date_desc": "-announced_date_first",
            "relevance": "",
            "citations": "",
        }
        order = order_map.get(sort_by, "")
        # ArXiv frontend requires specific sizes like 25, 50, 100, 200.
        size = 50
        offset = max(0, int(start_index or 0))

        params = {
            "advanced": "1",
            "terms-0-operator": "AND",
            "terms-0-term": query,
            "terms-0-field": prefix,
            "classification-include_cross_list": "include",
            "abstracts": "show",
            "size": str(size),
            "order": order,
            "start": str(offset),
        }
        if start_year is not None or end_year is not None:
            params["date-filter_by"] = "date_range"
            params["date-date_type"] = "submitted_date"
            if start_year is not None:
                params["date-from_date"] = f"{int(start_year):04d}-01-01"
            if end_year is not None:
                params["date-to_date"] = f"{int(end_year):04d}-12-31"
        else:
            params["date-filter_by"] = "all_dates"

        source_map = {
            "cs": "classification-computer_science",
            "econ": "classification-economics",
            "eess": "classification-eess",
            "math": "classification-mathematics",
            "physics": "classification-physics",
            "q-bio": "classification-q_biology",
            "q-fin": "classification-q_finance",
            "stat": "classification-statistics",
        }
        source_key = source_map.get((source_type or "").strip().lower())
        if source_key:
            params[source_key] = "y"
            if source_key == "classification-physics":
                params["classification-physics_archives"] = "all"

        url = "https://arxiv.org/search/advanced?" + urlencode(params)

        print(f"Fetching arXiv frontend HTML: {url}")
        try:
            html = await asyncio.to_thread(_fetch_text, url)
        except Exception as e:
            return {"total_results": "0", "papers": [], "error": f"arXiv frontend fetch failed: {e}"}
        soup = bs4.BeautifulSoup(html, "html.parser")
        
        total_str = "未知"
        h1 = soup.find('h1', class_='title is-clearfix')
        if h1:
            m = re.search(r'([\d,]+)\s*results?', h1.text)
            if m:
                total_str = m.group(1).replace(",", "")
        else:
            print("H1 NOT FOUND. HTML snippet:")
            print(html[:1000])
                
        results = []
        for li in soup.select('li.arxiv-result'):
            if len(results) >= limit:
                break
                
            title_p = li.find('p', class_='title')
            title = title_p.text.strip() if title_p else "N/A"
            
            authors_p = li.find('p', class_='authors')
            authors = []
            if authors_p:
                for a in authors_p.find_all('a'):
                    authors.append(a.text.strip())
            author_str = ", ".join(authors) if authors else "N/A"
            
            date_p = li.find('p', class_='is-size-7')
            date = "N/A"
            year = None
            if date_p:
                year = _extract_first_year(date_p.text)
                if year:
                    date = str(year)
            if not _year_allowed(year, start_year, end_year):
                continue
            
            link_p = li.find('p', class_='list-title')
            detail_link = ""
            if link_p:
                a_tag = link_p.find('a')
                if a_tag and 'href' in a_tag.attrs:
                    detail_link = urljoin("https://arxiv.org/", a_tag['href'])
                    
            uid = hashlib.md5(detail_link.encode()).hexdigest()[:8] if detail_link else "unk"
            
            abstract_span = li.find('span', class_='abstract-full')
            abstract = abstract_span.text.replace('△ Less', '').strip() if abstract_span else "N/A"
            
            results.append({
                "id": uid,
                "title": title,
                "author": author_str,
                "source": "arXiv: Frontend",
                "date": date,
                "db_type": "Preprint",
                "detail_link": detail_link,
                "_abstract": abstract
            })
            
        return {
            "total_results": total_str,
            "papers": results
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        # Details are mostly extracted in the search frontend. We don't need another heavy fetch
        # but to satisfy MCP interfaces we grab it.
        match = re.search(r'abs/([^\/]+)$', detail_url)
        if not match:
            return {"error": "Invalid arXiv url."}
        arxiv_id = match.group(1)
        
        try:
            html = await asyncio.to_thread(_fetch_text, detail_url)
            soup = bs4.BeautifulSoup(html, "html.parser")
            
            abs_block = soup.find('blockquote', class_='abstract')
            abstract = abs_block.text.replace('Abstract:', '').strip() if abs_block else "No abstract found."
            
            # arXiv doesn't explicitly have keywords on abstract page, usually subject classes.
            keywords = []
            subj_td = soup.find('td', class_='tablecell subjects')
            if subj_td:
                keywords.append(subj_td.text.strip())
                
            return {
                "url": detail_url,
                "abstract": abstract,
                "keywords": keywords,
                "doi": arxiv_id
            }
        except Exception as e:
            return {"error": f"Failed to parse arXiv frontend: {str(e)}"}

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)
        match = re.search(r'abs/([^\/]+)$', detail_url)
        if not match:
            return "Could not extract arxiv ID."
        
        arxiv_id = match.group(1)
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        
        # arXiv IDs can have a version suffix like v1. Sometimes filenames with . break things smoothly, we replace with _
        safe_id = arxiv_id.replace('.', '_')
        file_path = os.path.join(output_dir, f"arxiv_{safe_id}.pdf")
        cached = reuse_downloaded_pdf(self._pdf_cache, arxiv_id, output_dir, os.path.basename(file_path))
        if cached:
            print(f"[ARXIV] Reused in-process PDF cache for {arxiv_id}.")
            return cached
        
        def download():
            req = urllib.request.Request(pdf_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(file_path, 'wb') as out_file:
                out_file.write(response.read())
                
        try:
            await asyncio.to_thread(download)
            remember_downloaded_pdf(self._pdf_cache, arxiv_id, file_path)
            return file_path
        except Exception as e:
            return f"Error downloading arXiv PDF: {str(e)}"

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not pdf_path or not os.path.exists(pdf_path) or "Error" in pdf_path:
            return f"Failed to download PDF: {pdf_path}"
            
        if not pdf_path.lower().endswith(".pdf"):
            return f"Downloaded file is not a PDF, conversion not supported. Saved at: {pdf_path}"
            
        try:
            images_dir = os.path.join(output_dir, "images")
            os.makedirs(images_dir, exist_ok=True)
            
            # Since we don't have Playwright holding up resources here, this converts quite fast
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

scraper_instance = ArxivScraper()
