from runtime_config import ensure_runtime_environment

ensure_runtime_environment()

from mcp.server.fastmcp import FastMCP
from typing import List, Dict
import asyncio
from cnki_scraper import scraper_instance as cnki_scraper
from ieee_scraper import scraper_instance as ieee_scraper
from arxiv_scraper import scraper_instance as arxiv_scraper
from acm_scraper import scraper_instance as acm_scraper
from sd_scraper import scraper_instance as sd_scraper
from gs_scraper import scraper_instance as gs_scraper
from patyee_scraper import PatyeeScraper
from dawei_scraper import DaweiScraper
import json
import traceback

# Create an MCP server
mcp = FastMCP("academic-mcp")

# Instantiate Patyee
patyee_scraper = PatyeeScraper()
dawei_scraper = DaweiScraper()

SUPPORTED_SCRAPERS = {
    "CNKI": cnki_scraper,
    "IEEE": ieee_scraper,
    "ARXIV": arxiv_scraper,
    "ACM": acm_scraper,
    "SD": sd_scraper,
    "GS": gs_scraper,
    "PATYEE": patyee_scraper,
    "DAWEI": dawei_scraper,
    "DAWEISOFT": dawei_scraper,
    "PAT_DAWEI": dawei_scraper,
}

def get_scraper(platform: str):
    normalized = (platform or "CNKI").upper()
    if normalized not in SUPPORTED_SCRAPERS:
        supported = ", ".join(sorted(SUPPORTED_SCRAPERS))
        raise ValueError(f"Unsupported platform '{platform}'. Supported platforms: {supported}")
    return SUPPORTED_SCRAPERS[normalized]

@mcp.tool()
async def search_papers(query: str, platform: str = "CNKI", search_field: str = "\u4e3b\u9898", db_scope: str = "\u603b\u5e93", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> str:
    """
    Search for academic papers.
    - query: The search term (e.g. "大语言模型" or "Machine Learning").
    - platform: Target platform. "CNKI", "IEEE", "ARXIV", "ACM", "SD", "GS", "PATYEE", or "DAWEI". Default is "CNKI".
    - search_field: Target field/content-type for the query.
        - CNKI defaults to "主题". Options: "主题", "篇关摘", "关键词", "篇名", "全文", "作者", "第一作者", "通讯作者", "作者单位", "基金", "摘要", "小标题", "参考文献", "分类号", "文献来源", "DOI".
        - IEEE defaults to "All". Options: "All", "Authors", "Books", "Conferences", "Courses", "Journals & Magazines", "Standards", "Citations", "Images".
        - ARXIV defaults to "all". Options: "all" (All), "ti" (Title), "au" (Author), "abs" (Abstract), "cat" (Category).
        - ACM defaults to "AllField". Options: "AllField", "Title", "Abstract", "Author".
        - SD defaults to "qs". Options: "qs" (All Keywords), "title", "authors".
        - GS defaults to "all". Google Scholar inherently performs robust fuzzy semantic matching.
        - PATYEE defaults to "all".
        - DAWEI defaults to "all".
    - db_scope: Database scope (CNKI only). Options: "总库" (All), "中文" (Chinese), "外文" (Foreign).
    - source_type: Specific category filter.
        - CNKI: "all" (Default), "学术期刊", "学位论文", "会议", "报纸", "图书", "标准", "专利", etc.
        - IEEE: "all" (Default), "Conferences", "Journals", "Magazines", "Books", "Early Access Articles", "Standards", "Courses".
        - ARXIV: "all" (Default), "cs" (Computer Science), "math" (Mathematics), "physics", "q-bio", "q-fin", "stat", "eess", "econ".
        - ACM: "all" (Default), "research-article", "short-paper", "review-article", "tutorial", "opinion".
        - SD: "all" (Default), "REV" (Review articles), "FLA" (Research articles), "TRP" (Tutorials), "chp" (Book chapters).
        - PATYEE: You can specify "发明", "实用新型", etc.
        - DAWEI: "发明申请", "发明授权", "实用新型", "外观设计".
    - journal: Optional publication/journal name to filter by. Supported on SD, GS, IEEE.
    - start_year: Optional start year (e.g. 2023).
    - end_year: Optional end year (e.g. 2026).
    - sort_by: Sorting method. Options: "relevance" (Default), "citations", "date_desc".
    - start_index: Pagination offset. E.g. start_index=20 will skip first page and start fetching from item 21. Use this to jump pages!
    - limit: Max number of results to fetch. Default is 10.
    
    Returns a JSON string containing `total_results` (number of matches found for the scope) and a `papers` array of basic paper info (id, title, author, source, date, detail_link).
    **PRO TIP**: You should read titles from this function, then pass 'detail_link' into `get_paper_details` or `read_paper_content`.
    """
    try:
        scraper = get_scraper(platform)
        results = await scraper.search_papers(
            query=query, search_field=search_field, db_scope=db_scope, source_type=source_type,
            journal=journal, start_year=start_year, end_year=end_year, sort_by=sort_by,
            start_index=start_index, limit=limit
        )
    except Exception as e:
        results = {
            "total_results": "0",
            "papers": [],
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(limit=8),
        }
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
async def get_paper_details(url: str, platform: str = "CNKI") -> str:
    """
    Get detailed information (abstract, keywords, DOI) for a paper.
    - url: The absolute URL of the paper detail page (detail_link from search results).
    - platform: Target platform. "CNKI", "IEEE", "ARXIV", "ACM", "SD", "GS", "PATYEE", or "DAWEI". Default is "CNKI".
    """
    scraper = get_scraper(platform)
    details = await scraper.get_paper_details(url)
    return json.dumps(details, ensure_ascii=False, indent=2)

@mcp.tool()
async def download_paper(url: str, output_dir: str, platform: str = "CNKI") -> str:
    """
    Download the PDF of the paper to a specific local directory.
    - url: The absolute URL of the paper detail page.
    - output_dir: The local directory path to save the PDF.
    - platform: Target platform. "CNKI", "IEEE", "ARXIV", "ACM", "SD", "GS", "PATYEE", or "DAWEI". Default is "CNKI".
    
    Returns the absolute file path of the downloaded PDF.
    """
    scraper = get_scraper(platform)
    res = await scraper.download_paper(url, output_dir)
    return f"Download result: {res}"

@mcp.tool()
async def read_paper_content(url: str, output_dir: str, platform: str = "CNKI") -> str:
    """
    Download the PDF into `output_dir` and meticulously convert it to Markdown.
    It automatically triggers HD image extraction and saves pictures to `[output_dir]/images/`.
    The resulting `.md` file with relative image links is saved inside `output_dir`.
    - url: The absolute URL of the paper detail page. 
    - output_dir: An absolutely/relatively resolved local directory path to hold the files.
    - platform: Target platform. "CNKI", "IEEE", "ARXIV", "ACM", "SD", "GS", "PATYEE", or "DAWEI". Default is "CNKI".
    
    Returns the local path of the final MD document and the first 1000 characters as a preview.
    """
    scraper = get_scraper(platform)
    md_content = await scraper.read_paper_content(url, output_dir)
    if isinstance(md_content, tuple):
        path, preview = md_content
        return f"Markdown generation complete. Saved to: {path}\n\nPreview:\n{preview}..."
    return str(md_content)

@mcp.tool()
async def convert_local_pdf(pdf_path: str, output_dir: str) -> str:
    """
    Convert a specific manually downloaded PDF into Markdown natively.
    It automatically triggers HD image extraction and saves pictures to `[output_dir]/images/`.
    The resulting `.md` file with relative image links is saved inside `output_dir`.
    - pdf_path: Absolute path to the manually downloaded local PDF file.
    - output_dir: Absolute path to the output directory where Markdown and images will go.
    
    Returns the local path of the final MD document and the first 1000 characters as a preview.
    """
    import os
    from pdf_utils import convert_pdf_to_markdown

    if not os.path.exists(pdf_path):
        return f"Error: The provided PDF path does not exist: {pdf_path}"
    if not pdf_path.lower().endswith(".pdf"):
        return f"Error: The provided file is not a PDF: {pdf_path}"

    try:
        converted = convert_pdf_to_markdown(pdf_path, output_dir)
        snippet = converted.preview + "\n...(More Content Available)"
        note = "\n注意: PDF未发现可提取文本，Markdown以页面图片方式生成。" if converted.image_only else ""
        res = (f"=== 本地转换成功 ===\n"
               f"输入PDF: {pdf_path}\n"
               f"Markdown及高清图像已保存至目录: {output_dir}\n"
               f"完整的MD文件路径: {converted.md_path}{note}\n"
               f"--- 以下为前1000字预览 ---\n\n{snippet}")
        return res
    except Exception as e:
        return f"Conversion failed: {str(e)}"

if __name__ == "__main__":
    mcp.run()
