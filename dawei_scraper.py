import asyncio
import functools
import hashlib
import json
import os
import re
import sys
from mcp_logging import safe_stderr_print
import urllib.parse
from typing import Dict, Optional

import aiohttp
from bs4 import BeautifulSoup
from pdf_utils import convert_pdf_to_markdown, safe_stem
from runtime_config import ensure_runtime_environment, project_path

print = safe_stderr_print
ensure_runtime_environment()


class DaweiScraper:
    BASE_URL = "https://pat.daweisoft.com"
    SEARCH_API = "**/api/innojoy-search/api/v1/patent/search"

    SOURCE_TYPE_MAP = {
        "\u53d1\u660e\u7533\u8bf7": "fmsq",
        "\u53d1\u660e\u6388\u6743": "fmzl",
        "\u53d1\u660e\u4e13\u5229": "fmzl",
        "\u5b9e\u7528\u65b0\u578b": "xx",
        "\u5916\u89c2\u8bbe\u8ba1": "wg",
    }

    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self.profile_dir = project_path(".dawei_profile")

    async def initialize(self, force_headful: bool = False):
        await self._ensure_browser(force_headful=force_headful)

    async def _ensure_browser(self, force_headful: bool = False):
        if self.context:
            return

        os.makedirs(self.profile_dir, exist_ok=True)
        # Firefox-style parent.lock cleanup for Camoufox crashes.
        for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
            lock_path = os.path.join(self.profile_dir, lock_name)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass

        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            headless=not force_headful,
            accept_downloads=True,
            ignore_https_errors=True,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def close(self):
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    @staticmethod
    def _strip_html(value) -> str:
        if not value:
            return ""
        if isinstance(value, dict):
            value = value.get("ABZH") or value.get("ABEN") or value.get("ABOL") or ""
        return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)

    @staticmethod
    def _pick_title(item: dict) -> str:
        title = item.get("TI")
        if isinstance(title, dict):
            title = title.get("TIZH") or title.get("TIEN") or title.get("TIOL")
        return BeautifulSoup(str(title or "Unknown Title"), "html.parser").get_text(" ", strip=True)

    @staticmethod
    def _extract_year(date_text: str) -> Optional[int]:
        if not date_text:
            return None
        match = re.search(r"(?:19|20)\d{2}", str(date_text))
        return int(match.group(0)) if match else None

    @staticmethod
    def _year_allowed(year: Optional[int], start_year: int = None, end_year: int = None) -> bool:
        if start_year is None and end_year is None:
            return True
        if year is None:
            return False
        if start_year is not None and year < start_year:
            return False
        if end_year is not None and year > end_year:
            return False
        return True

    def _build_query(self, query: str, start_year: int = None, end_year: int = None) -> str:
        query = (query or "").strip()
        if start_year is None and end_year is None:
            return query
        start = start_year or 1900
        end = end_year or 2100
        date_expr = f"AD=({start}.01.01 TO {end}.12.31)"
        return f"({query}) and ({date_expr})" if query else date_expr

    def _db_types(self, source_type: str) -> list[str]:
        if not source_type or str(source_type).lower() in {"all", "\u5168\u90e8"}:
            return ["fmzl", "fmsq", "xx", "wg"]
        mapped = self.SOURCE_TYPE_MAP.get(str(source_type).strip())
        return [mapped] if mapped else ["fmzl", "fmsq", "xx", "wg"]

    @staticmethod
    def _response_body(response) -> dict:
        try:
            return json.loads(response.request.post_data or "")
        except Exception:
            return {}

    @staticmethod
    def _is_patent_list_body(body: dict) -> bool:
        fields = str(body.get("fields") or "")
        return (
            body.get("searchType") in {"patent_list", ""}
            and "PNM" in fields
            and "MY_COLLECTION" in fields
        )

    @classmethod
    def _response_matches(cls, response, *, search_type: str = None, fields: str = None) -> bool:
        if "/api/innojoy-search/api/v1/patent/search" not in response.url:
            return False
        if response.request.method != "POST":
            return False
        body = cls._response_body(response)
        if body:
            if search_type == "patent_list":
                return cls._is_patent_list_body(body)
            if search_type is not None and body.get("searchType") != search_type:
                return False
            if fields is not None and body.get("fields") != fields:
                return False
            return True

        post_data = response.request.post_data or ""
        if search_type is not None and not re.search(rf'"searchType"\s*:\s*"{re.escape(search_type)}"', post_data):
            return False
        if fields is not None and not re.search(rf'"fields"\s*:\s*"{re.escape(fields)}"', post_data):
            return False
        return True

    @staticmethod
    def _detail_params(detail_url: str) -> dict:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(detail_url).query)
        return {
            "pnm": (query.get("PNM") or [""])[0],
            "an": (query.get("AN") or [""])[0],
        }

    @classmethod
    def _pick_detail_item(cls, items: list[dict], detail_url: str) -> dict:
        params = cls._detail_params(detail_url)
        target_pnm = (params.get("pnm") or "").strip()
        target_an = (params.get("an") or "").strip()
        for item in items or []:
            item_pnm = str(item.get("PNM") or item.get("PN") or "").strip()
            item_an = str(item.get("AN") or "").strip()
            if target_pnm and item_pnm == target_pnm:
                return item
            if target_an and item_an == target_an:
                return item
        return {}

    async def _goto_home(self):
        try:
            await self.page.goto(f"{self.BASE_URL}/index", wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"Dawei index navigation did not reach domcontentloaded: {e}")
        try:
            await self.page.wait_for_selector("button.search-btn", timeout=30000)
            await self.page.wait_for_selector("textarea, input.ant-input", timeout=30000)
        except Exception:
            pass

    async def search_papers(
        self,
        query: str,
        search_field: str = "all",
        db_scope: str = "",
        source_type: str = "all",
        journal: str = None,
        start_year: int = None,
        end_year: int = None,
        sort_by: str = "relevance",
        start_index: int = 0,
        limit: int = 10,
    ) -> Dict:
        await self._ensure_browser()
        search_query = self._build_query(query, start_year=start_year, end_year=end_year)
        await self._goto_home()

        search_box = self.page.locator(
            "textarea[placeholder*='\u7533\u8bf7\u4eba'], input[placeholder*='\u7533\u8bf7\u4eba'], "
            "textarea.ant-input, input.ant-input"
        ).filter(visible=True).first
        await search_box.fill(search_query)

        limit = max(0, int(limit or 0))
        start_index = max(0, int(start_index or 0))
        page_size = max(20, min(100, limit or 20))
        page_num = (start_index // page_size) + 1
        page_offset = start_index % page_size
        db_types = self._db_types(source_type)

        async def patch_search_request(route):
            request = route.request
            if request.method != "POST":
                await route.continue_()
                return

            try:
                body = json.loads(request.post_data or "")
                if not self._is_patent_list_body(body):
                    await route.continue_()
                    return
                body["dbTypes"] = db_types
                body["pageNum"] = page_num
                body["pageSize"] = page_size
                if sort_by == "date_desc":
                    body["sortBy"] = "-AD"
                await route.continue_(
                    post_data=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    headers={**request.headers, "content-type": "application/json;charset=UTF-8"},
                )
                return
            except Exception:
                await route.continue_()

        await self.page.route(self.SEARCH_API, patch_search_request)
        try:
            async with self.page.expect_response(
                lambda resp: self._response_matches(resp, search_type="patent_list"),
                timeout=60000,
            ) as response_info:
                await self.page.locator("button.search-btn").filter(visible=True).first.click()
            search_response = await response_info.value
        except Exception as e:
            print(f"Dawei search response wait failed: {e}")
            return {"total_results": 0, "papers": [], "error": "Dawei search timed out waiting for search response."}
        finally:
            await self.page.unroute(self.SEARCH_API, patch_search_request)

        payload = await search_response.json()
        data = payload.get("data") or {}
        total_results = payload.get("totalCount") or data.get("totalCount") or data.get("total") or 0
        items = data.get("patentList") or []

        papers = []
        for item in items[page_offset:]:
            if len(papers) >= limit:
                break
            date = item.get("AD") or item.get("PD") or item.get("GD") or ""
            year = self._extract_year(date)
            if not self._year_allowed(year, start_year=start_year, end_year=end_year):
                continue

            pnm = item.get("PNM") or item.get("PN") or ""
            an = item.get("AN") or ""
            cc = item.get("CC") or ""
            title = self._pick_title(item)
            detail_link = (
                f"{self.BASE_URL}/detail?AN={urllib.parse.quote(an)}&PNM={urllib.parse.quote(pnm)}"
                f"&CC={urllib.parse.quote(cc)}&pageNum=1&specialIndex={item.get('NO') or 1}"
            )
            uid = hashlib.md5(detail_link.encode()).hexdigest()[:8]
            papers.append(
                {
                    "id": pnm or an or uid,
                    "title": title.strip(),
                    "author": (item.get("INN") or "").strip(),
                    "source": f"Dawei Patent ({pnm})" if pnm else "Dawei Patent",
                    "date": date,
                    "db_type": item.get("AT") or item.get("PTS") or "Patent",
                    "detail_link": detail_link,
                }
            )

        return {"total_results": total_results, "papers": papers}

    async def _load_detail_item(self, detail_url: str) -> dict:
        await self._ensure_browser()
        try:
            if self.page.url.startswith(f"{self.BASE_URL}/detail"):
                await self.page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
            async with self.page.expect_response(
                lambda resp: self._response_matches(resp, search_type="baseinfo_detail"),
                timeout=60000,
            ) as response_info:
                await self.page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
            response = await response_info.value
            payload = await response.json()
            items = ((payload.get("data") or {}).get("patentList")) or []
            return self._pick_detail_item(items, detail_url)
        except Exception as e:
            print(f"Dawei detail response wait failed: {e}")
            return {}

    async def get_paper_details(self, url: str) -> Dict:
        item = await self._load_detail_item(url)
        if not item:
            return {"url": url, "abstract": "No abstract found", "keywords": [], "doi": ""}

        pnm = item.get("PNM") or ""
        abstract = self._strip_html(item.get("AB")) or "No abstract found"
        keywords = item.get("IPC_L") or item.get("CPC_L") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        return {"url": url, "abstract": abstract, "keywords": keywords, "doi": pnm}

    async def _click_pdf_tab(self):
        try:
            await self.page.get_by_role("listitem").filter(has_text="PDF\u539f\u6587").first.click(timeout=10000)
        except Exception:
            await self.page.get_by_text("PDF\u539f\u6587", exact=True).click(timeout=10000)

    async def _extract_pdf_url(self, detail_url: str) -> str:
        detail_item = await self._load_detail_item(detail_url)
        if not detail_item:
            return ""
        try:
            async with self.page.expect_response(
                lambda resp: self._response_matches(resp, search_type="baseinfo_detail", fields="PDF"),
                timeout=60000,
            ) as response_info:
                await self._click_pdf_tab()
            response = await response_info.value
        except Exception as e:
            print(f"Dawei PDF response wait failed: {e}")
            return ""

        payload = await response.json()
        items = ((payload.get("data") or {}).get("patentList")) or []
        item = self._pick_detail_item(items, detail_url)
        return item.get("PDF", "") if item else ""

    async def download_paper(self, url: str, output_dir: str) -> str:
        await self._ensure_browser()
        os.makedirs(output_dir, exist_ok=True)
        pdf_url = await self._extract_pdf_url(url)
        if not pdf_url:
            return "Error: Dawei did not expose a PDF URL for this patent. The original text may be unavailable."

        pnm = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("PNM", ["dawei_patent"])[0]
        file_path = os.path.join(output_dir, f"{safe_stem(pnm)}.pdf")
        headers = {"Referer": url}
        errors = []
        body = b""
        status = None

        for timeout in (60000,):
            try:
                response = await self.context.request.get(pdf_url, headers=headers, timeout=timeout)
                status = response.status
                body = await response.body()
            except Exception as e:
                errors.append(str(e))
                await asyncio.sleep(2)
                continue
            if status == 200 and body.startswith(b"%PDF"):
                break
            errors.append(f"Playwright request returned HTTP {status} or a non-PDF payload.")

        if not (status == 200 and body.startswith(b"%PDF")):
            try:
                user_agent = await self.page.evaluate("navigator.userAgent")
            except Exception:
                user_agent = "Mozilla/5.0"
            direct_headers = {"Referer": url, "User-Agent": user_agent, "Accept": "application/pdf,*/*"}
            timeout = aiohttp.ClientTimeout(total=75, connect=20, sock_read=60)
            try:
                async with aiohttp.ClientSession(headers=direct_headers, timeout=timeout) as session:
                    async with session.get(pdf_url, ssl=False) as resp:
                        status = resp.status
                        body = await resp.read()
            except Exception as e:
                errors.append(f"aiohttp fallback failed: {e}")

        if status != 200:
            return f"Error: Dawei PDF request returned HTTP {status}. Attempts: {' | '.join(errors[-3:])}"
        if not body.startswith(b"%PDF"):
            return f"Error: Dawei PDF response is not a valid PDF payload. Attempts: {' | '.join(errors[-3:])}"
        with open(file_path, "wb") as f:
            f.write(body)
        return file_path

    async def read_paper_content(self, url: str, output_dir: str) -> str:
        pdf_path = await self.download_paper(url, output_dir)
        if not pdf_path or "Error:" in pdf_path or not os.path.exists(pdf_path):
            return f"Failed to download PDF: {pdf_path}"
        try:
            converted = convert_pdf_to_markdown(pdf_path, output_dir)
            note = "\nNote: no extractable text was found; Markdown was generated from page images." if converted.image_only else ""
            return (
                f"=== Conversion Successful ===\n"
                f"PDF downloaded: {pdf_path}\n"
                f"Markdown and images saved to: {output_dir}\n"
                f"Markdown file path: {converted.md_path}{note}\n"
                f"--- Preview (first 1000 chars) ---\n\n{converted.preview}\n...(More Content Available)"
            )
        except Exception as e:
            return f"PDF downloaded to {pdf_path} but Markdown conversion failed: {str(e)}"
