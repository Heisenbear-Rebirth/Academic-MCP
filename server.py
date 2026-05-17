from runtime_config import ensure_runtime_environment

ensure_runtime_environment()

import asyncio
import json
import os
import traceback
from pathlib import Path
from typing import Dict, List

from mcp.server.fastmcp import FastMCP

from cnki_scraper import scraper_instance as cnki_scraper
from ieee_scraper import scraper_instance as ieee_scraper
from arxiv_scraper import scraper_instance as arxiv_scraper
from acm_scraper import scraper_instance as acm_scraper
from sd_scraper import scraper_instance as sd_scraper
from gs_scraper import scraper_instance as gs_scraper
from patyee_scraper import PatyeeScraper
from dawei_scraper import DaweiScraper
from library import extract_native_id, get_library
from mcp_logging import safe_stderr_print

print = safe_stderr_print

mcp = FastMCP("academic-mcp")

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


def _newest(paths) -> Path | None:
    items = [p for p in paths if p.exists()]
    if not items:
        return None
    return max(items, key=lambda p: p.stat().st_mtime)


@mcp.tool()
async def search_papers(query: str, platform: str = "CNKI", search_field: str = "主题", db_scope: str = "总库", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> str:
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
    norm = (platform or "CNKI").upper()
    filters = {
        "search_field": search_field,
        "db_scope": db_scope,
        "source_type": source_type,
        "journal": journal,
        "start_year": start_year,
        "end_year": end_year,
        "sort_by": sort_by,
        "start_index": start_index,
        "limit": limit,
    }
    try:
        scraper = get_scraper(norm)
        lib = get_library()

        async def fetch():
            return await scraper.search_papers(
                query=query, search_field=search_field, db_scope=db_scope, source_type=source_type,
                journal=journal, start_year=start_year, end_year=end_year, sort_by=sort_by,
                start_index=start_index, limit=limit,
            )

        results, cache_hit = await lib.search_or_fetch(norm, query, filters, fetch)
        if isinstance(results, dict):
            results = dict(results)
            results["_cache_hit"] = cache_hit
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
    norm = (platform or "CNKI").upper()
    scraper = get_scraper(norm)
    lib = get_library()
    native_id = extract_native_id(norm, url)

    if native_id and lib.enabled:
        cached = await asyncio.to_thread(lib.get_paper, norm, native_id)
        if cached and cached.get("abstract"):
            payload = {
                "url": url,
                "abstract": cached["abstract"],
                "keywords": cached.get("keywords") or [],
                "doi": cached.get("doi") or "",
                "_cache_hit": True,
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)

    details = await scraper.get_paper_details(url)
    if native_id and isinstance(details, dict) and details.get("abstract") and details.get("abstract") != "No abstract found":
        try:
            await asyncio.to_thread(
                lib.upsert_paper, norm, native_id,
                detail_link=url,
                abstract=details.get("abstract"),
                keywords=details.get("keywords"),
                doi=details.get("doi"),
            )
        except Exception as e:
            print(f"[Library] upsert after details failed: {e}")
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
    norm = (platform or "CNKI").upper()
    scraper = get_scraper(norm)
    lib = get_library()
    native_id = extract_native_id(norm, url)

    if not native_id or not lib.enabled:
        res = await scraper.download_paper(url, output_dir)
        return f"Download result: {res}"

    cached = await asyncio.to_thread(lib.get_paper, norm, native_id)
    if cached and cached.get("pdf_path") and os.path.exists(cached["pdf_path"]):
        mirrored = lib.mirror_pdf_to(Path(cached["pdf_path"]), output_dir)
        return f"Download result: {mirrored} (library cache hit)"

    canonical_dir = lib.canonical_dir(norm, native_id)
    canonical_dir.mkdir(parents=True, exist_ok=True)
    res = await scraper.download_paper(url, str(canonical_dir))

    actual_pdf: Path | None = None
    if isinstance(res, str) and os.path.exists(res):
        actual_pdf = Path(res)
    else:
        actual_pdf = _newest(canonical_dir.glob("*.pdf"))

    if actual_pdf and actual_pdf.exists():
        try:
            await asyncio.to_thread(
                lib.upsert_paper, norm, native_id,
                detail_link=url,
                pdf_path=str(actual_pdf),
            )
        except Exception as e:
            print(f"[Library] upsert after download failed: {e}")
        mirrored = lib.mirror_pdf_to(actual_pdf, output_dir)
        return f"Download result: {mirrored}"

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
    norm = (platform or "CNKI").upper()
    scraper = get_scraper(norm)
    lib = get_library()
    native_id = extract_native_id(norm, url)

    if not native_id or not lib.enabled:
        md_content = await scraper.read_paper_content(url, output_dir)
        if isinstance(md_content, tuple):
            path, preview = md_content
            return f"Markdown generation complete. Saved to: {path}\n\nPreview:\n{preview}..."
        return str(md_content)

    cached = await asyncio.to_thread(lib.get_paper, norm, native_id)

    # 1) MD already in library: mirror MD + images to caller's output_dir.
    if cached and cached.get("md_path") and os.path.exists(cached["md_path"]):
        mirrored_md = lib.mirror_markdown_to(Path(cached["md_path"]), output_dir)
        preview = ""
        try:
            preview = Path(mirrored_md).read_text(encoding="utf-8")[:1000]
        except Exception:
            pass
        return f"Markdown generation complete. Saved to: {mirrored_md}\n\nPreview:\n{preview}... (library cache hit)"

    canonical_dir = lib.canonical_dir(norm, native_id)
    canonical_dir.mkdir(parents=True, exist_ok=True)

    # 2) PDF cached but MD missing: just convert.
    if cached and cached.get("pdf_path") and os.path.exists(cached["pdf_path"]):
        try:
            from pdf_utils import convert_pdf_to_markdown
            converted = await asyncio.to_thread(
                convert_pdf_to_markdown, cached["pdf_path"], str(canonical_dir)
            )
            await asyncio.to_thread(
                lib.upsert_paper, norm, native_id,
                detail_link=url,
                md_path=converted.md_path,
                images_dir=str(Path(converted.md_path).parent / "images"),
            )
            mirrored_md = lib.mirror_markdown_to(Path(converted.md_path), output_dir)
            return f"Markdown generation complete. Saved to: {mirrored_md}\n\nPreview:\n{converted.preview}... (PDF cache hit, freshly converted)"
        except Exception as e:
            print(f"[Library] Cached-PDF→MD conversion failed: {e}")

    # 3) Full miss: run the scraper into the canonical dir, then mirror to caller.
    raw_result = await scraper.read_paper_content(url, str(canonical_dir))
    md_path = _newest(canonical_dir.glob("*.md"))
    pdf_path = _newest(canonical_dir.glob("*.pdf"))

    if md_path and md_path.exists():
        try:
            await asyncio.to_thread(
                lib.upsert_paper, norm, native_id,
                detail_link=url,
                md_path=str(md_path),
                images_dir=str(canonical_dir / "images"),
                pdf_path=str(pdf_path) if pdf_path else None,
            )
        except Exception as e:
            print(f"[Library] upsert after read failed: {e}")
        mirrored_md = lib.mirror_markdown_to(md_path, output_dir)
        try:
            preview = md_path.read_text(encoding="utf-8")[:1000]
        except Exception:
            preview = ""
        return f"Markdown generation complete. Saved to: {mirrored_md}\n\nPreview:\n{preview}..."

    if isinstance(raw_result, tuple):
        path, preview = raw_result
        return f"Markdown generation complete. Saved to: {path}\n\nPreview:\n{preview}..."
    return str(raw_result)


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
