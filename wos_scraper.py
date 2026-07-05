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
    verification_window_size,
)

print = safe_stderr_print
ensure_runtime_environment()


class WebOfScienceScraper:
    """Discovery-only Web of Science scraper.

    WoS' normal browser flow posts NDJSON requests to `/api/wosnx/core/*`.
    Generic TLS clients (Python HTTP, Playwright's APIRequestContext) are dropped
    before TLS completes on the Tongji VPN route, but a real browser's page
    networking succeeds -- Camoufox (Firefox) works here too. We issue API-level
    `fetch()` calls inside a persistent Camoufox context with a *pinned*
    fingerprint, so a headless session reuses the same cf_clearance a headful
    verification produced. Runs headless by default; the visible window only
    appears for manual verification (never for a routine, already-verified
    search).
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
        "all fields": "ALL",
        "all field": "ALL",
        "allfield": "ALL",
        "allfields": "ALL",
        "all": "ALL",
        "everything": "ALL",
        "any": "ALL",
        "全部": "ALL",
        "全部字段": "ALL",
        "": "TS",
        "topic": "TS",
        "主题": "TS",
        "ts": "TS",
        "title": "TI",
        "题名": "TI",
        "标题": "TI",
        "ti": "TI",
        "author": "AU",
        "authors": "AU",
        "作者": "AU",
        "au": "AU",
        "publication title": "SO",
        "publication titles": "SO",
        "source": "SO",
        "source title": "SO",
        "journal": "SO",
        "journal title": "SO",
        "刊名": "SO",
        "来源": "SO",
        "so": "SO",
        "year": "PY",
        "year published": "PY",
        "publication year": "PY",
        "published year": "PY",
        "年份": "PY",
        "发表年份": "PY",
        "py": "PY",
        "affiliation": "OG",
        "affiliations": "OG",
        "organization": "OG",
        "organisation": "OG",
        "institution": "OG",
        "机构": "OG",
        "单位": "OG",
        "og": "OG",
        "funding agency": "FO",
        "funding agencies": "FO",
        "funder": "FO",
        "funders": "FO",
        "基金资助机构": "FO",
        "资助机构": "FO",
        "fo": "FO",
        "publisher": "PUBL",
        "出版社": "PUBL",
        "出版商": "PUBL",
        "publ": "PUBL",
        "pu": "PUBL",
        "publication date": "DOP",
        "date of publication": "DOP",
        "发表日期": "DOP",
        "出版日期": "DOP",
        "dop": "DOP",
        "abstract": "AB",
        "摘要": "AB",
        "ab": "AB",
        "accession": "UT",
        "accession number": "UT",
        "入藏号": "UT",
        "ut": "UT",
        "address": "AD",
        "地址": "AD",
        "ad": "AD",
        "author identifiers": "AI",
        "author identifier": "AI",
        "author id": "AI",
        "researcherid": "AI",
        "orcid": "AI",
        "ai": "AI",
        "author keywords": "AK",
        "author keyword": "AK",
        "keywords": "AK",
        "keyword": "AK",
        "关键词": "AK",
        "作者关键词": "AK",
        "ak": "AK",
        "conference": "CF",
        "会议": "CF",
        "cf": "CF",
        "document type": "DT",
        "doc type": "DT",
        "doctype": "DT",
        "文献类型": "DT",
        "dt": "DT",
        "doi": "DO",
        "do": "DO",
        "editor": "ED",
        "editors": "ED",
        "编者": "ED",
        "ed": "ED",
        "grant number": "FG",
        "grant no": "FG",
        "grant": "FG",
        "基金号": "FG",
        "项目号": "FG",
        "fg": "FG",
        "group author": "GP",
        "group authors": "GP",
        "团体作者": "GP",
        "gp": "GP",
        "keyword plus": "KP",
        "keywords plus": "KP",
        "keyword plus ®": "KP",
        "kp": "KP",
        "language": "LA",
        "语言": "LA",
        "la": "LA",
        "pubmed id": "PMID",
        "pubmed": "PMID",
        "pmid": "PMID",
        "web of science categories": "WC",
        "web of science category": "WC",
        "wos categories": "WC",
        "wos category": "WC",
        "category": "WC",
        "categories": "WC",
        "学科类别": "WC",
        "wc": "WC",
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

    # "Search in" database selector -> WoS `product` / `search.database` code.
    # Codes are the official WoS values (the dropdown's `option-<CODE>` ids). All
    # collections share the same normalised NX record schema and are keyed by
    # their UT accession prefix (see _COLLECTION_PATH), so search /
    # get_paper_details / caching work across databases. Default is WOSCC; an
    # unrecognised value is passed through upper-cased.
    DATABASE_MAP = {
        "": "WOSCC",
        "wos": "WOSCC",
        "woscc": "WOSCC",
        "core collection": "WOSCC",
        "web of science core collection": "WOSCC",
        "核心合集": "WOSCC",
        "web of science 核心合集": "WOSCC",
        "总库": "WOSCC",
        "all": "ALLDB",
        "alldb": "ALLDB",
        "all databases": "ALLDB",
        "所有数据库": "ALLDB",
        "ccc": "CCC",
        "current contents connect": "CCC",
        "cscd": "CSCD",
        "chinese science citation database": "CSCD",
        "中国科学引文数据库": "CSCD",
        "diidw": "DIIDW",
        "derwent": "DIIDW",
        "derwent innovations index": "DIIDW",
        "德温特": "DIIDW",
        "grants": "GRANTS",
        "grants index": "GRANTS",
        "kjd": "KJD",
        "kci": "KJD",
        "kci-korean journal database": "KJD",
        "korean": "KJD",
        "medline": "MEDLINE",
        "medline®": "MEDLINE",
        "pprn": "PPRN",
        "preprint": "PPRN",
        "preprint citation index": "PPRN",
        "pqdt": "PQDT",
        "proquest": "PQDT",
        "dissertations": "PQDT",
        "proquest dissertations & theses citation index": "PQDT",
        "rc": "RC",
        "research commons": "RC",
        "scielo": "SCIELO",
        "scielo citation index": "SCIELO",
    }

    # WOSCC "Editions" selector. The UI labels/codes differ from the API codes
    # the runQuerySearch endpoint expects (e.g. CPCI-S -> ISTP), so every alias
    # is normalised to the API code and sent as `search.editions=["WOS.<CODE>"]`.
    # Leaving it empty (or "all") omits the field, which means all editions.
    _EDITION_CODES = {"SCI", "SSCI", "AHCI", "ISTP", "ISSHP", "ESCI", "CCR", "IC"}
    EDITION_MAP = {
        "sci": "SCI",
        "scie": "SCI",
        "sci-expanded": "SCI",
        "science citation index expanded": "SCI",
        "science citation index": "SCI",
        "ssci": "SSCI",
        "social sciences citation index": "SSCI",
        "social science citation index": "SSCI",
        "ahci": "AHCI",
        "a&hci": "AHCI",
        "arts & humanities citation index": "AHCI",
        "arts and humanities citation index": "AHCI",
        "cpci-s": "ISTP",
        "istp": "ISTP",
        "conference proceedings citation index - science": "ISTP",
        "conference proceedings citation index – science": "ISTP",
        "conference proceedings citation index science": "ISTP",
        "cpci-ssh": "ISSHP",
        "isshp": "ISSHP",
        "conference proceedings citation index - social science & humanities": "ISSHP",
        "conference proceedings citation index – social science & humanities": "ISSHP",
        "esci": "ESCI",
        "emerging sources citation index": "ESCI",
        "ccr": "CCR",
        "ccr-expanded": "CCR",
        "current chemical reactions": "CCR",
        "ic": "IC",
        "index chemicus": "IC",
    }

    def __init__(self):
        self.context = None
        self.page = None
        self.camoufox_cm = None
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

    # WoS accession numbers (UT) are `PREFIX:VALUE`; the prefix identifies the
    # collection and maps to the segment in a full-record URL. The segment is the
    # lowercase product code (verified live for woscc/medline/ccc/cscd/kjd/pprn);
    # only WOSCC differs from its "WOS" UT prefix. Any prefix not listed falls
    # back to prefix.lower() (harmless -- the pipeline keys off the UT, not the
    # URL path). For an All-Databases search, each record carries its home
    # database's prefix, so links resolve to the right collection automatically.
    _COLLECTION_PATH = {
        "WOS": "woscc",
        "MEDLINE": "medline",
        "CCC": "ccc",
        "CSCD": "cscd",
        "DIIDW": "diidw",
        "KJD": "kjd",
        "PPRN": "pprn",
        "PQDT": "pqdt",
        "SCIELO": "scielo",
        "GRANTS": "grants",
        "RC": "rc",
    }

    @staticmethod
    def _wos_id_from_url(url: str) -> str:
        """Extract the WoS accession (UT) from a detail URL, any collection.

        The UT is whatever follows ``/full-record/`` (e.g. ``WOS:000...``,
        ``MEDLINE:39012345``, ``CCC:...``); a bare accession is matched too.
        """
        decoded = urllib.parse.unquote(url or "")
        match = re.search(r"/full-record/([^/?#]+)", decoded)
        if match:
            return match.group(1).strip()
        match = re.search(r"([A-Za-z]{2,}:[A-Za-z0-9._-]+)", decoded)
        return match.group(1) if match else ""

    @classmethod
    def _detail_link(cls, ut: str) -> str:
        prefix = ut.split(":", 1)[0].upper() if ":" in ut else ""
        collection = cls._COLLECTION_PATH.get(prefix) or (prefix.lower() if prefix else "woscc")
        return f"{cls.BASE_URL}/wos/{collection}/full-record/{urllib.parse.quote(ut, safe=':')}"

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
        # WoS NX sort descriptors, verified live. The old field-code style
        # (PY.D / PY.A / TC.D) is rejected by the API with "Invocation error".
        value = str(sort_by or "relevance").strip().lower()
        if value in {"date_desc", "date", "newest", "year_desc"}:
            return "date-descending"
        if value in {"date_asc", "oldest", "year_asc"}:
            return "date-ascending"
        if value in {"citations", "cited", "citations_desc", "most_cited"}:
            return "times-cited-descending"
        if value in {"citations_asc", "least_cited"}:
            return "times-cited-ascending"
        return "relevance"

    @classmethod
    def _advanced_search_requested(cls, search_field: str) -> bool:
        value = str(search_field or "").strip().lower()
        return value in cls.ADVANCED_FIELD_ALIASES

    @classmethod
    def _field_code(cls, search_field: str) -> str:
        value = str(search_field or "TS").strip()
        return cls.FIELD_MAP.get(value.lower(), value.upper())

    @classmethod
    def _database_code(cls, db_scope: str) -> str:
        """Map a friendly 'Search in' value to a WoS product/database code.

        Defaults to WOSCC (the only database this scraper fully parses).
        Unknown values are passed through upper-cased so an explicit product
        code still works, but callers should expect best-effort parsing there.
        """
        value = str(db_scope or "").strip()
        if not value:
            return "WOSCC"
        return cls.DATABASE_MAP.get(value.lower(), value.upper())

    @classmethod
    def _edition_code(cls, token: str) -> str | None:
        """Resolve one edition alias/label to its API code (e.g. CPCI-S -> ISTP)."""
        text = str(token or "").strip()
        if not text:
            return None
        low = text.lower()
        if low in cls.EDITION_MAP:
            return cls.EDITION_MAP[low]
        if low.startswith("wos."):
            low = low[4:]
            if low in cls.EDITION_MAP:
                return cls.EDITION_MAP[low]
        # A full UI label carries the code in parentheses, e.g.
        # "Science Citation Index Expanded (SCI-EXPANDED)--1975-present".
        match = re.search(r"\(([^)]+)\)", low)
        if match and match.group(1).strip() in cls.EDITION_MAP:
            return cls.EDITION_MAP[match.group(1).strip()]
        if text.upper() in cls._EDITION_CODES:
            return text.upper()
        return None

    @classmethod
    def _parse_editions(cls, editions) -> list[str] | None:
        """Normalise an editions request to `["WOS.<CODE>", ...]` or None (=all).

        Accepts a comma/semicolon-separated string or a list of aliases, UI
        labels, or API codes. Unknown tokens are skipped.
        """
        if editions is None:
            return None
        if isinstance(editions, str):
            text = editions.strip()
            if not text or text.lower() in {"all", "all editions", "全部", "所有", "全部版本"}:
                return None
            tokens = [t.strip() for t in re.split(r"[;,]", text) if t.strip()]
        elif isinstance(editions, (list, tuple, set)):
            tokens = [str(t).strip() for t in editions if str(t).strip()]
        else:
            return None
        codes: list[str] = []
        for token in tokens:
            code = cls._edition_code(token)
            if code and code not in codes:
                codes.append(code)
        return [f"WOS.{code}" for code in codes] or None

    @staticmethod
    def _row_boolean(value: str, default: str = "AND") -> str:
        op = str(value or default).strip().upper()
        return op if op in {"AND", "OR", "NOT"} else default

    @classmethod
    def _is_known_field(cls, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return text.lower() in cls.FIELD_MAP or text.upper() in set(cls.FIELD_MAP.values())

    @classmethod
    def _row_from_mapping(cls, item: dict, *, default_field: str, default_boolean: str) -> dict | None:
        field = (
            item.get("rowField")
            or item.get("field")
            or item.get("search_field")
            or item.get("searchField")
            or default_field
        )
        text = item.get("rowText")
        if text is None:
            text = item.get("text")
        if text is None:
            text = item.get("query")
        text = str(text or "").strip()
        if not text:
            return None
        boolean = (
            item.get("rowBoolean")
            or item.get("op")
            or item.get("operator")
            or item.get("boolean")
            or default_boolean
        )
        return {
            "rowField": cls._field_code(field),
            "rowText": text,
            "rowBoolean": cls._row_boolean(boolean, default_boolean),
        }

    @classmethod
    def _row_from_text(cls, line: str, *, default_field: str, default_boolean: str) -> dict | None:
        text = str(line or "").strip()
        if not text:
            return None
        boolean = default_boolean
        op_match = re.match(r"^(AND|OR|NOT)\s+(.+)$", text, re.IGNORECASE)
        if op_match:
            boolean = cls._row_boolean(op_match.group(1), default_boolean)
            text = op_match.group(2).strip()

        field = default_field
        field_match = re.match(r"^(.+?)\s*(?:=|:)\s*(.+)$", text)
        if field_match and cls._is_known_field(field_match.group(1)):
            field = field_match.group(1).strip()
            text = field_match.group(2).strip()
        if not text:
            return None
        return {
            "rowField": cls._field_code(field),
            "rowText": text,
            "rowBoolean": boolean,
        }

    @classmethod
    def _parse_fielded_rows(cls, query, search_field: str) -> list[dict]:
        default_field = cls._field_code(search_field)
        row_items = None
        if isinstance(query, list):
            row_items = query
        elif isinstance(query, dict):
            row_items = query.get("rows") or query.get("query")
        else:
            query_text = str(query or "").strip()
            if query_text[:1] in {"[", "{"}:
                try:
                    decoded = json.loads(query_text)
                    if isinstance(decoded, list):
                        row_items = decoded
                    elif isinstance(decoded, dict):
                        row_items = decoded.get("rows") or decoded.get("query")
                except Exception:
                    row_items = None

        rows = []
        if row_items is not None:
            for index, item in enumerate(row_items):
                default_boolean = "AND" if index else ""
                if isinstance(item, dict):
                    row = cls._row_from_mapping(item, default_field=default_field, default_boolean=default_boolean)
                else:
                    row = cls._row_from_text(str(item), default_field=default_field, default_boolean=default_boolean)
                if row:
                    rows.append(row)
        else:
            parts = [part.strip() for part in re.split(r"[\r\n;]+", str(query or "")) if part.strip()]
            if not parts:
                parts = [str(query or "").strip()]
            for index, part in enumerate(parts):
                row = cls._row_from_text(
                    part,
                    default_field=default_field,
                    default_boolean="AND" if index else "",
                )
                if row:
                    rows.append(row)

        if not rows:
            rows = [{"rowField": default_field, "rowText": str(query or "").strip()}]
        for index, row in enumerate(rows):
            if index == 0:
                row.pop("rowBoolean", None)
            elif not row.get("rowBoolean"):
                row["rowBoolean"] = "AND"
        return rows

    @classmethod
    def _build_search_clause(
        cls,
        query: str,
        search_field: str,
        database: str = "WOSCC",
        editions: list[str] | None = None,
    ) -> tuple[str, dict]:
        if cls._advanced_search_requested(search_field):
            search_mode, search = (
                "advanced",
                {
                    "mode": "general",
                    "database": database,
                    "query": [{"rowText": query}],
                    "sets": [],
                    "options": {"lemmatize": "On"},
                },
            )
        else:
            search_mode, search = (
                "general",
                {
                    "mode": "general",
                    "database": database,
                    "query": cls._parse_fielded_rows(query, search_field),
                },
            )
        # Editions are a WOSCC-only refinement; omitting the key means "All".
        if editions and database == "WOSCC":
            search["editions"] = list(editions)
        return search_mode, search

    def _build_search_payload(
        self,
        query: str,
        *,
        search_field: str,
        sort_by: str,
        count: int,
        database: str = "WOSCC",
        editions: list[str] | None = None,
    ) -> dict:
        search_mode, search = self._build_search_clause(query, search_field, database, editions)
        # The analyze buckets and JCR enrichment are WOSCC-only; sending them for
        # another database makes the server reject the whole request
        # (Server.invalidInput), so restrict them to WOSCC.
        is_woscc = (database or "WOSCC") == "WOSCC"
        return {
            "product": database or "WOSCC",
            "searchMode": search_mode,
            "viewType": "search",
            "serviceMode": "summary",
            "search": search,
            "retrieve": {
                "count": max(1, min(100, int(count or 20))),
                "history": True,
                "jcr": is_woscc,
                "sort": self._sort_value(sort_by),
                "analyzes": self.ANALYZES if is_woscc else [],
                "locale": "en",
            },
            "eventMode": None,
        }

    async def _ensure_browser(self, *, force_headful: bool = False):
        if self.context and self.page:
            return
        from camoufox.async_api import AsyncCamoufox
        from scraper_utils import apply_browser_cookies, pooled_profile, load_or_create_fingerprint

        profile_dir, self._profile_ephemeral = pooled_profile(self.PROFILE_BASE, "WOS")
        self._profile_dir = profile_dir
        for lock_name in ("lockfile", "SingletonLock", "parent.lock", ".parentlock"):
            lock_path = os.path.join(profile_dir, lock_name)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass

        # A pinned fingerprint is identical headless vs headful, so a
        # cf_clearance minted during a headful verification stays valid for
        # later headless fetches -- that is what lets routine searches stay
        # windowless.
        _shared_fp = load_or_create_fingerprint("WOS")
        # Pin a modest, on-screen window size so the (rare) headful verification
        # window fits a laptop screen and the captcha is reachable. Applied to
        # BOTH modes so the fingerprint stays identical (camoufox ignores the
        # `window=` launch arg once an explicit fingerprint is supplied).
        if _shared_fp is not None:
            try:
                from camoufox.fingerprints import handle_window_size
                _vw, _vh = verification_window_size()
                handle_window_size(_shared_fp, _vw, _vh)
                # handle_window_size centers within the fingerprint's (often
                # large/offset) virtual screen, which can land off the real
                # display. Anchor near the top-left so the window is always
                # visible.
                try:
                    _shared_fp.screen.screenX = 40
                    _shared_fp.screen.screenY = 40
                except Exception:
                    pass
            except Exception as e:
                print(f"[WOS] window-size pin skipped: {e}")
        print(f"[WOS] Initializing Camoufox context (Headless: {not force_headful})...")
        _cam_kw = dict(
            headless=not force_headful,
            user_data_dir=profile_dir,
            persistent_context=True,
            os="windows",
            humanize=True,
            geoip=True,
        )
        if _shared_fp is not None:
            _cam_kw["fingerprint"] = _shared_fp
            _cam_kw["i_know_what_im_doing"] = True
        self.camoufox_cm = AsyncCamoufox(**_cam_kw)
        self.context = await self.camoufox_cm.__aenter__()
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
        # Headless by default; warmup_auth() re-opens headful only when the API
        # replies that human verification is required.
        await self._ensure_browser(force_headful=False)
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
        editions: str = "",
    ) -> Dict:
        start_index = max(0, int(start_index or 0))
        limit = max(1, int(limit or 10))
        fetch_count = min(100, start_index + limit)
        database = self._database_code(db_scope)
        editions_list = self._parse_editions(editions)
        payload = self._build_search_payload(
            query,
            search_field=search_field,
            sort_by=sort_by,
            count=fetch_count,
            database=database,
            editions=editions_list,
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
        if self.camoufox_cm:
            try:
                await self.camoufox_cm.__aexit__(None, None, None)
            except Exception as e:
                print(f"[WOS] camoufox close failed: {e}")
            self.camoufox_cm = None
        if self._profile_dir:
            from scraper_utils import cleanup_pooled_profile

            cleanup_pooled_profile(self._profile_dir, self._profile_ephemeral)
            self._profile_dir = None


scraper_instance = WebOfScienceScraper()
