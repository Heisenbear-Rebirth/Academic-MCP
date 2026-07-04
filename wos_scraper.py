from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List

from bs4 import BeautifulSoup

from mcp_logging import safe_stderr_print
from runtime_config import (
    allow_headful_fallback_for,
    ensure_runtime_environment,
    manual_verification_timeout_seconds,
    profile_path,
)

print = safe_stderr_print
ensure_runtime_environment()


class WebOfScienceScraper:
    """Discovery-only Web of Science scraper.

    WoS' normal browser flow posts NDJSON requests to `/api/wosnx/core/*`.
    In the current Tongji VPN route, plain Python HTTP clients and Playwright's
    APIRequestContext fail before TLS completes, while Chromium page networking
    succeeds. We therefore issue API-level `fetch()` calls inside a verified
    persistent Chromium profile. This still avoids UI scraping/clicking for
    search data; the visible browser is only used for manual verification.
    """

    BASE_URL = "https://webofscience.clarivate.cn"
    START_URL = f"{BASE_URL}/wos/woscc/basic-search"
    PROFILE_BASE = ".wos_profile"
    WARMUP_QUERY = "aeroelastic flutter"

    ANALYZES = [
        "TP.Value.6",
        "REVIEW.Value.6",
        "EARLY ACCESS.Value.6",
        "OA.Value.6",
        "DR.Value.6",
        "ECR.Value.6",
        "PY.Field_D.6",
        "FPY.Field_D.6",
        "DT.Value.6",
        "AU.Value.6",
        "DX2NG.Value.6",
        "PEERREVIEW.Value.6",
        "STK.Value.10",
    ]

    FIELD_MAP = {
        "": "TS",
        "all": "TS",
        "topic": "TS",
        "ts": "TS",
        "title": "TI",
        "ti": "TI",
        "author": "AU",
        "authors": "AU",
        "au": "AU",
        "doi": "DO",
        "do": "DO",
        "abstract": "AB",
        "ab": "AB",
        "accession": "UT",
        "accession number": "UT",
        "ut": "UT",
    }
    ADVANCED_FIELD_ALIASES = {
        "advanced",
        "advanced search",
        "advanced-search",
        "adv",
        "query",
        "query builder",
        "query-builder",
        "wql",
        "wos advanced",
        "wos query",
        "高级",
        "高级检索",
        "高级搜索",
    }

    def __init__(self):
        self.context = None
        self.page = None
        self.playwright_cm = None
        self.allow_headful_fallback = allow_headful_fallback_for("WOS")
        self.manual_verification_timeout = manual_verification_timeout_seconds()
        self._record_cache: dict[str, dict] = {}
        self._query_cache: dict[str, dict] = {}
        self._profile_dir = None
        self._profile_ephemeral = False

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @classmethod
    def _html_to_text(cls, html: str) -> str:
        if not html:
            return ""
        return cls._clean_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))

    @staticmethod
    def _first_title(group: dict, *keys: str) -> str:
        for key in keys:
            value = (((group or {}).get(key) or {}).get("en") or [])
            for item in value:
                if isinstance(item, dict) and item.get("title"):
                    return item["title"]
        return ""

    @staticmethod
    def _identifier(record: dict, wanted: str) -> str:
        wanted = wanted.lower()
        for item in record.get("identifiers") or []:
            if isinstance(item, dict) and str(item.get("type", "")).lower() == wanted:
                return str(item.get("value") or "")
        return ""

    @classmethod
    def _abstract_from_record(cls, record: dict) -> str:
        abstract = (((record.get("abstract") or {}).get("basic") or {}).get("en") or {}).get("abstract") or ""
        return cls._html_to_text(abstract)

    @classmethod
    def _authors_from_record(cls, record: dict) -> list[str]:
        authors = []
        raw_authors = (((record.get("names") or {}).get("author") or {}).get("en") or [])
        for item in raw_authors:
            if not isinstance(item, dict):
                continue
            name = item.get("display_name") or item.get("wos_standard")
            if not name:
                first = item.get("first_name") or ""
                last = item.get("last_name") or ""
                name = f"{first} {last}".strip()
            name = cls._clean_text(name)
            if name and name not in authors:
                authors.append(name)
        corp_authors = (((record.get("names") or {}).get("book_corp") or {}).get("en") or [])
        for item in corp_authors:
            if isinstance(item, dict):
                name = cls._clean_text(item.get("display_name") or "")
                if name and name not in authors:
                    authors.append(name)
        return authors

    @staticmethod
    def _wos_id_from_url(url: str) -> str:
        decoded = urllib.parse.unquote(url or "")
        match = re.search(r"(WOS:[A-Za-z0-9]+)", decoded, re.IGNORECASE)
        return match.group(1).upper() if match else ""

    @classmethod
    def _detail_link(cls, ut: str) -> str:
        return f"{cls.BASE_URL}/wos/woscc/full-record/{urllib.parse.quote(ut, safe=':')}"

    @staticmethod
    def _year_from_record(record: dict) -> int | None:
        pub_info = record.get("pub_info") or {}
        for key in ("pubyear", "sortdate", "pubdate", "coverdate"):
            value = str(pub_info.get(key) or "")
            match = re.search(r"\b(19|20)\d{2}\b", value)
            if match:
                return int(match.group(0))
        return None

    @staticmethod
    def _sort_value(sort_by: str) -> str:
        value = str(sort_by or "relevance").strip().lower()
        if value in {"date_desc", "date", "newest", "year_desc"}:
            return "PY.D"
        if value in {"date_asc", "oldest", "year_asc"}:
            return "PY.A"
        if value in {"citations", "cited"}:
            return "TC.D"
        return "relevance"

    @classmethod
    def _advanced_search_requested(cls, search_field: str) -> bool:
        value = str(search_field or "").strip().lower()
        return value in cls.ADVANCED_FIELD_ALIASES

    @classmethod
    def _build_search_clause(cls, query: str, search_field: str) -> tuple[str, dict]:
        if cls._advanced_search_requested(search_field):
            return (
                "advanced",
                {
                    "mode": "general",
                    "database": "WOSCC",
                    "query": [{"rowText": query}],
                    "sets": [],
                    "options": {"lemmatize": "On"},
                },
            )

        field = cls.FIELD_MAP.get(
            str(search_field or "TS").strip().lower(),
            str(search_field or "TS").strip().upper(),
        )
        return (
            "general",
            {
                "mode": "general",
                "database": "WOSCC",
                "query": [{"rowField": field, "rowText": query}],
            },
        )

    def _build_search_payload(
        self,
        query: str,
        *,
        search_field: str,
        sort_by: str,
        count: int,
    ) -> dict:
        search_mode, search = self._build_search_clause(query, search_field)
        return {
            "product": "WOSCC",
            "searchMode": search_mode,
            "viewType": "search",
            "serviceMode": "summary",
            "search": search,
            "retrieve": {
                "count": max(1, min(100, int(count or 20))),
                "history": True,
                "jcr": True,
                "sort": self._sort_value(sort_by),
                "analyzes": self.ANALYZES,
                "locale": "en",
            },
            "eventMode": None,
        }

    async def _ensure_browser(self, *, force_headful: bool = True):
        if self.context and self.page:
            return
        from playwright.async_api import async_playwright
        from scraper_utils import apply_browser_cookies, pooled_profile

        profile_dir, self._profile_ephemeral = pooled_profile(self.PROFILE_BASE, "WOS")
        self._profile_dir = profile_dir
        for lock_name in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = os.path.join(profile_dir, lock_name)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass

        print(f"[WOS] Initializing Chromium profile (Headless: {not force_headful})...")
        self.playwright_cm = async_playwright()
        playwright = await self.playwright_cm.__aenter__()
        self.context = await playwright.chromium.launch_persistent_context(
            profile_dir,
            headless=not force_headful,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            timezone_id="Asia/Shanghai",
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await apply_browser_cookies(self.context, "WOS")
        try:
            await self.page.goto(self.START_URL, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"[WOS] Initial navigation warning: {type(e).__name__}: {e}")
        await self.page.wait_for_timeout(5000)
        try:
            accept = self.page.get_by_role("button", name="Accept all")
            if await accept.count() == 1:
                try:
                    await accept.click(timeout=3000)
                except Exception:
                    pass
        except Exception:
            pass

    async def _page_fetch_ndjson(self, payload: dict) -> list[dict]:
        await self._ensure_browser(force_headful=True)
        result = await self.page.evaluate(
            """async ({ payload }) => {
                const sid = (
                    (localStorage.getItem('wos_sid') || '').replaceAll('"', '')
                    || (document.cookie.match(/(?:^|; )WOSSID=([^;]+)/) || [])[1]
                    || ''
                );
                const response = await fetch('/api/wosnx/core/runQuerySearch?SID=' + encodeURIComponent(sid), {
                    method: 'POST',
                    headers: {
                        accept: 'application/x-ndjson',
                        'content-type': 'text/plain;charset=UTF-8'
                    },
                    body: JSON.stringify(payload)
                });
                return {
                    status: response.status,
                    contentType: response.headers.get('content-type') || '',
                    text: await response.text()
                };
            }""",
            {"payload": payload},
        )
        text = result.get("text") or ""
        if result.get("status", 0) >= 400:
            raise RuntimeError(f"WoS API request failed: HTTP {result.get('status')} {text[:300]}")
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                items.append({"key": "raw", "payload": line})
        errors = [obj for obj in items if obj.get("key") == "error"]
        if errors:
            payloads = [p for obj in errors for p in (obj.get("payload") or [])]
            if any("passiveVerificationRequired" in str(p) for p in payloads):
                raise RuntimeError(
                    'WoS requires human verification. Run warmup_platform_auth(platforms="WOS") '
                    "and complete the hCaptcha in the browser window, then retry."
                )
            raise RuntimeError(f"WoS API returned error: {payloads or errors}")
        return items

    async def _warmup_until_fetch_ok(self, timeout_seconds: int) -> bool:
        payload = self._build_search_payload(
            self.WARMUP_QUERY,
            search_field="TS",
            sort_by="relevance",
            count=1,
        )
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                await self._page_fetch_ndjson(payload)
                return True
            except Exception:
                await asyncio.sleep(3)
        return False

    async def warmup_auth(self, timeout_seconds: int = None) -> Dict:
        if timeout_seconds is not None:
            self.manual_verification_timeout = max(30, int(timeout_seconds))
        self.allow_headful_fallback = True
        if self.context:
            await self.close()
        started = time.perf_counter()
        await self._ensure_browser(force_headful=True)

        ok = False
        first_error = ""
        try:
            await self._page_fetch_ndjson(
                self._build_search_payload(
                    self.WARMUP_QUERY,
                    search_field="TS",
                    sort_by="relevance",
                    count=1,
                )
            )
            ok = True
        except Exception as e:
            first_error = f"{type(e).__name__}: {e}"
            try:
                box = self.page.get_by_role(
                    "textbox",
                    name="Search box 1 Topic, Example: oil spill* mediterranean",
                )
                if await box.count() == 1:
                    await box.fill(self.WARMUP_QUERY, timeout=10000)
                    await self.page.locator("button.search").click(timeout=10000)
            except Exception:
                pass
            print(
                ">>> PLEASE COMPLETE WOS HUMAN VERIFICATION IN THE CHROMIUM WINDOW. "
                f"Waiting up to {self.manual_verification_timeout}s... <<<"
            )
            ok = await self._warmup_until_fetch_ok(self.manual_verification_timeout)

        try:
            from scraper_utils import capture_browser_cookies

            await capture_browser_cookies(
                self.context,
                "WOS",
                note="manual warmup verified" if ok else "manual warmup attempted",
            )
        except Exception:
            pass

        return {
            "platform": "WOS",
            "ok": ok,
            "seconds": round(time.perf_counter() - started, 3),
            "title": await self.page.title() if self.page else "",
            "url": self.page.url if self.page else "",
            "first_error": first_error,
        }

    @classmethod
    def _record_to_paper(cls, record: dict, *, rank: int = None) -> Dict:
        titles = record.get("titles") or {}
        ut = record.get("ut") or record.get("colluid") or ((record.get("id") or {}).get("value") if isinstance(record.get("id"), dict) else "")
        doi = record.get("doi") or cls._identifier(record, "doi")
        source = cls._first_title(titles, "source")
        pub_info = record.get("pub_info") or {}
        pub_date = pub_info.get("pubdate") or pub_info.get("coverdate") or pub_info.get("sortdate") or pub_info.get("pubyear") or ""
        authors = cls._authors_from_record(record)
        abstract = cls._abstract_from_record(record)
        citation_counts = ((record.get("citation_related") or {}).get("counts") or {})
        doctypes = record.get("doctypes") or []
        raw_full_text_links = (((record.get("link") or {}).get("fullText") or []) if isinstance(record.get("link"), dict) else [])
        full_text_providers = []
        for item in raw_full_text_links:
            if not isinstance(item, dict):
                continue
            full_text_providers.append(
                {
                    "publisher_id": item.get("publisher_id") or "",
                    "content_type": item.get("content_type") or "",
                    "oa": bool(item.get("oa")),
                    "desc": item.get("desc") or "",
                }
            )
        paper = {
            "id": ut or doi or record.get("docid") or "",
            "title": cls._clean_text(cls._first_title(titles, "item")),
            "author": ", ".join(authors),
            "authors": authors,
            "source": cls._clean_text(source),
            "venue_name": cls._clean_text(source),
            "date": str(pub_date),
            "pub_date": str(pub_date),
            "abstract": abstract,
            "_abstract": abstract,
            "doi": doi,
            "detail_link": cls._detail_link(ut) if ut else "",
            "db_type": "WOS",
            "recommended_platform": "WOS",
            "pdf_url": "",
            "extra": {
                "ut": ut,
                "docid": record.get("docid"),
                "doctypes": doctypes,
                "pub_info": pub_info,
                "times_cited_wos": citation_counts.get("WOSCC"),
                "times_cited_all": citation_counts.get("ALLDB"),
                "rank": rank,
                "full_text_providers": full_text_providers,
            },
        }
        return paper

    def _parse_search_items(
        self,
        items: list[dict],
        *,
        limit: int,
        start_index: int,
        source_type: str = "all",
        journal: str = None,
        start_year: int = None,
        end_year: int = None,
    ) -> Dict:
        total = "0"
        query_id = ""
        records: dict[str, dict] = {}
        links: dict[str, dict] = {}
        for item in items:
            key = item.get("key")
            payload = item.get("payload") or {}
            if key == "searchInfo" and isinstance(payload, dict):
                total = str(payload.get("RecordsAvailable") or payload.get("RecordsFound") or "0")
                query_id = payload.get("QueryID") or ""
            elif key == "records" and isinstance(payload, dict):
                records.update(payload)
            elif key == "link" and isinstance(payload, dict):
                links.update(payload)

        papers = []
        wanted_type = str(source_type or "all").strip().lower()
        journal_clean = str(journal or "").strip().lower()
        for rank_text, record in sorted(records.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999999):
            if not isinstance(record, dict):
                continue
            ut = record.get("ut") or record.get("colluid")
            if ut and ut in links:
                record = dict(record)
                record["link"] = links.get(ut)
            year = self._year_from_record(record)
            if start_year is not None and (year is None or year < int(start_year)):
                continue
            if end_year is not None and (year is None or year > int(end_year)):
                continue
            doctypes = " ".join(record.get("doctypes") or []).lower()
            if wanted_type not in {"", "all"} and wanted_type not in doctypes:
                continue
            source = self._first_title(record.get("titles") or {}, "source").lower()
            if journal_clean and journal_clean not in source:
                continue
            rank = int(rank_text) if str(rank_text).isdigit() else None
            paper = self._record_to_paper(record, rank=rank)
            if not paper.get("detail_link"):
                continue
            if ut:
                self._record_cache[ut] = record
            papers.append(paper)

        if start_index:
            papers = papers[int(start_index):]
        result = {"total_results": total, "papers": papers[: max(0, int(limit or 10))]}
        if query_id:
            result["query_id"] = query_id
            self._query_cache[query_id] = {"items": items, "records": records}
        return result

    async def search_papers(
        self,
        query: str,
        search_field: str = "TS",
        db_scope: str = "",
        source_type: str = "all",
        journal: str = None,
        start_year: int = None,
        end_year: int = None,
        sort_by: str = "relevance",
        start_index: int = 0,
        limit: int = 10,
    ) -> Dict:
        start_index = max(0, int(start_index or 0))
        limit = max(1, int(limit or 10))
        fetch_count = min(100, start_index + limit)
        payload = self._build_search_payload(
            query,
            search_field=search_field,
            sort_by=sort_by,
            count=fetch_count,
        )
        try:
            items = await self._page_fetch_ndjson(payload)
        except Exception as first_error:
            if not self.allow_headful_fallback:
                raise
            print(f"[WOS] API fetch needs warmup ({first_error}); opening verification flow.")
            await self.warmup_auth()
            items = await self._page_fetch_ndjson(payload)
        return self._parse_search_items(
            items,
            limit=limit,
            start_index=start_index,
            source_type=source_type,
            journal=journal,
            start_year=start_year,
            end_year=end_year,
        )

    async def get_paper_details(self, detail_url: str) -> Dict[str, object]:
        ut = self._wos_id_from_url(detail_url)
        record = self._record_cache.get(ut) if ut else None
        if record:
            paper = self._record_to_paper(record)
            return {
                "url": detail_url,
                "title": paper.get("title", ""),
                "authors": paper.get("authors") or [],
                "author": paper.get("author", ""),
                "source": paper.get("source", ""),
                "venue_name": paper.get("venue_name", ""),
                "pub_date": paper.get("pub_date", ""),
                "abstract": paper.get("abstract") or "No abstract found",
                "keywords": [],
                "doi": paper.get("doi", ""),
                "wos_uid": ut,
                "times_cited_wos": (paper.get("extra") or {}).get("times_cited_wos"),
                "times_cited_all": (paper.get("extra") or {}).get("times_cited_all"),
            }
        return {
            "url": detail_url,
            "abstract": "No abstract found",
            "keywords": [],
            "doi": "",
            "wos_uid": ut,
            "note": "WoS details are discovery-only; call search_papers first so the record metadata is cached.",
        }

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        return "Download is not supported by WOS; use the DOI or publisher platform returned in the search result."

    async def read_paper_content(self, detail_url: str, output_dir: str):
        return "Read is not supported by WOS; use the DOI or publisher platform returned in the search result."

    async def fetch_ris(self, detail_url: str) -> str:
        return ""

    async def close(self):
        if self.context:
            try:
                from scraper_utils import capture_browser_cookies

                await capture_browser_cookies(self.context, "WOS")
            except Exception:
                pass
        if self.context:
            try:
                await self.context.close()
            except Exception as e:
                print(f"[WOS] context close failed: {e}")
            self.context = None
            self.page = None
        if self.playwright_cm:
            try:
                await self.playwright_cm.__aexit__(None, None, None)
            except Exception as e:
                print(f"[WOS] playwright close failed: {e}")
            self.playwright_cm = None
        if self._profile_dir:
            from scraper_utils import cleanup_pooled_profile

            cleanup_pooled_profile(self._profile_dir, self._profile_ephemeral)
            self._profile_dir = None


scraper_instance = WebOfScienceScraper()
