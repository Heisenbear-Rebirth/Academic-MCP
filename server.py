from mcp.server.fastmcp import FastMCP
from typing import List, Dict
import asyncio
from cnki_scraper import scraper_instance
import json

# Create an MCP server
mcp = FastMCP("cnki-mcp")

@mcp.tool()
async def search_papers(query: str, search_field: str = "主题", db_scope: str = "总库", source_type: str = "all", start_index: int = 0, limit: int = 10) -> str:
    """
    Search for academic papers on CNKI.
    - query: The search term (e.g. "大语言模型").
    - search_field: Target field for the query. Defaults to "主题". Options: "主题", "篇关摘", "关键词", "篇名", "全文", "作者", "第一作者", "通讯作者", "作者单位", "基金", "摘要", "小标题", "参考文献", "分类号", "文献来源", "DOI".
    - db_scope: Database scope. Options: "总库" (All), "中文" (Chinese), "外文" (Foreign).
    - source_type: Specific category. Options: "all" (Default), "学术期刊" (Journal), "学位论文" (Dissertation), "会议" (Conference), "报纸", "图书", "标准", "专利", etc.
    - start_index: Pagination offset. E.g. start_index=20 will skip first page and start fetching from item 21. Use this to jump pages!
    - limit: Max number of results to fetch. Default is 10.
    
    Returns a JSON string containing `total_results` (number of matches found for the scope) and a `papers` array of basic paper info (id, title, author, source, date, detail_link).
    **PRO TIP**: You should read titles from this function, then pass 'detail_link' into `get_paper_details` or `read_paper_content`.
    """
    results = await scraper_instance.search_papers(query, search_field, db_scope, source_type, start_index, limit)
    return json.dumps(results, ensure_ascii=False, indent=2)

@mcp.tool()
async def get_paper_details(url: str) -> str:
    """
    Get detailed information (abstract, keywords, DOI) for a paper.
    - url: The absolute URL of the CNKI paper detail page (detail_link from search results).
    """
    details = await scraper_instance.get_paper_details(url)
    return json.dumps(details, ensure_ascii=False, indent=2)

@mcp.tool()
async def download_paper(url: str, output_dir: str) -> str:
    """
    Download the PDF of the paper to a specific local directory.
    - url: The absolute URL of the CNKI paper detail page.
    - output_dir: The local directory path to save the PDF.
    
    Returns the absolute file path of the downloaded PDF.
    """
    res = await scraper_instance.download_paper(url, output_dir)
    return f"Download result: {res}"

@mcp.tool()
async def read_paper_content(url: str, output_dir: str) -> str:
    """
    Download the PDF into `output_dir` and meticulously convert it to Markdown.
    It automatically triggers HD image extraction and saves pictures to `[output_dir]/images/`.
    The resulting `.md` file with relative image links is saved inside `output_dir`.
    - url: The absolute URL of the CNKI paper detail page. 
    - output_dir: An absolutely/relatively resolved local directory path to hold the files.
    
    Returns the local path of the final MD document and the first 1000 characters as a preview.
    """
    md_content = await scraper_instance.read_paper_content(url, output_dir)
    return md_content

if __name__ == "__main__":
    import asyncio
    mcp.run()
