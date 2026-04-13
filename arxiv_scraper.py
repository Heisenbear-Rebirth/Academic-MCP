import os
import asyncio
import urllib.request
import urllib.parse
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
import bs4
import pymupdf4llm
import hashlib
import re

class ArxivScraper:
    def __init__(self):
        # We do not need playwright for arXiv due to its open API
        pass
        
    async def initialize(self):
        pass

    async def close(self):
        pass

    async def search_papers(self, query: str, search_field: str = "all", db_scope: str = "", source_type: str = "all", start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        # arXiv prefixes mapper
        field_map = {
            "全部": "all", "主题": "all",
            "篇名": "ti", "摘要": "abs",
            "作者": "au", "关键词": "all",
        }
        prefix = field_map.get(search_field, search_field if search_field in ["all", "ti", "au", "abs", "cat", "rn", "id"] else "all")
        
        # For ArXiv, exact phrase quotes ("") are too restrictive for standard LLM multi-word query.
        # Break down words and enforce boolean AND logic across the target prefix.
        # We must use proper spacing around AND so arXiv parser interprets it correctly
        # BUT explicitly doing `AND` forces strict Boolean requirement. 
        # Better precision is achieved by just putting keywords together with spaces natively.
        words = query.strip().split()
        if len(words) > 1:
            q_str = " ".join([f"{prefix}:{w}" for w in words])
        else:
            q_str = f"{prefix}:{query}"
        
        # arXiv category filtering
        if source_type.lower() not in ["all", "全部", ""]:
            # e.g., if source_type == "cs"
            q_str += f" AND cat:{source_type}*"
            
        if sort_by == "date_desc":
            sort_str = "&sortBy=lastUpdatedDate&sortOrder=descending"
        else:
            sort_str = "&sortBy=relevance&sortOrder=descending"
            
        # Use simple quote which converts space to %20
        encoded_query = urllib.parse.quote(q_str)
        api_url = f"http://export.arxiv.org/api/query?search_query={encoded_query}{sort_str}&start={start_index}&max_results={limit}"
        
        def fetch_api():
            req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            import time
            for _ in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=30) as response:
                        return response.read()
                except Exception as e:
                    time.sleep(2)
            raise Exception("Failed after 3 retries")
                
        try:
            xml_data = await asyncio.to_thread(fetch_api)
        except Exception as e:
            print(f"ArXiv API Error: {e} | URL: {api_url}")
            return {"total_results": "0", "papers": []}
            
        # Parse XML
        root = ET.fromstring(xml_data)
        
        # arXiv uses Atom namespace
        ns = {'atom': 'http://www.w3.org/2005/Atom', 'opensearch': 'http://a9.com/-/spec/opensearch/1.1/'}
        
        total_results_elem = root.find('opensearch:totalResults', ns)
        total_results = total_results_elem.text if total_results_elem is not None else "未知"
        
        results = []
        for entry in root.findall('atom:entry', ns):
            id_elem = entry.find('atom:id', ns)
            detail_link = id_elem.text if id_elem is not None else ""
            
            title_elem = entry.find('atom:title', ns)
            # Remove newlines from titles
            title = title_elem.text.replace('\n', ' ').replace('  ', ' ').strip() if title_elem is not None else "N/A"
            
            # Fetch primary author or all authors
            authors = []
            for author_node in entry.findall('atom:author', ns):
                name_elem = author_node.find('atom:name', ns)
                if name_elem is not None:
                    authors.append(name_elem.text.strip())
            author_str = ", ".join(authors) if authors else "N/A"
            
            pub_elem = entry.find('atom:published', ns)
            date = pub_elem.text[:4] if pub_elem is not None else "N/A" # Just extract year
            
            cat_elem = entry.find('atom:category', ns)
            source = cat_elem.attrib.get('term', 'ArXiv') if cat_elem is not None else "ArXiv"
            
            uid = hashlib.md5(detail_link.encode()).hexdigest()[:8]
            
            results.append({
                "id": uid,
                "title": title,
                "author": author_str,
                "source": "arXiv: " + source,
                "date": date,
                "db_type": "Preprint",
                "detail_link": detail_link
            })
            
        return {
            "total_results": total_results,
            "papers": results
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        # Extract ID from e.g. http://arxiv.org/abs/1905.12265v1
        match = re.search(r'abs/([^\/]+)$', detail_url)
        if not match:
            return {"error": "Invalid arXiv url."}
        arxiv_id = match.group(1)
        
        api_url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
        
        def fetch_details():
            req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                return response.read()
                
        try:
            xml_data = await asyncio.to_thread(fetch_details)
            root = ET.fromstring(xml_data)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            
            entry = root.find('atom:entry', ns)
            if entry is None:
                return {"error": "Paper not found."}
                
            summary_elem = entry.find('atom:summary', ns)
            abstract = summary_elem.text.replace('\n', ' ').strip() if summary_elem is not None else "No abstract found."
            
            keywords = []
            for cat in entry.findall('atom:category', ns):
                term = cat.attrib.get('term')
                if term:
                    keywords.append(term)
                    
            return {
                "url": detail_url,
                "abstract": abstract,
                "keywords": keywords,
                "doi": arxiv_id # In ArXiv, the ID operates as its identifier
            }
        except Exception as e:
            return {"error": str(e)}

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
        
        def download():
            req = urllib.request.Request(pdf_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(file_path, 'wb') as out_file:
                out_file.write(response.read())
                
        try:
            await asyncio.to_thread(download)
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
