import asyncio
import functools
import json
import os
import secrets
import sys
from mcp_logging import safe_stderr_print
from typing import Dict, List
import urllib.parse
import re
from bs4 import BeautifulSoup
import aiohttp
from pdf_utils import convert_pdf_to_markdown, safe_stem
from runtime_config import allow_headful_fallback_for, ensure_runtime_environment, project_path

print = safe_stderr_print
ensure_runtime_environment()

class PatyeeScraper:
    BASE_URL = "https://www.patyee.com"
    API_BASE_URL = "https://alpha.patyee.com"
    TOKEN_URL = f"{API_BASE_URL}/ourchem-middle/oauth/ip/token?client_id=client_2&client_secret=123456&grant_type=password"
    SEARCH_API_URL = f"{API_BASE_URL}/ourchem-patyee/patyee/search/search"
    FILE_LIST_URL = f"{API_BASE_URL}/ourchem-middle/data/file/list/files"
    MODEL_ID = "75c3b76f-1cb9-11eb-b1c6-70106fc9fcee"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    )
    SEARCH_REQUEST_COLS = [
        {"alias": "abstract_ab"},
        {"alias": "publication_date"},
        {"alias": "application_date"},
        {"alias": "priority_day"},
        {"alias": "inventors"},
        {"alias": "family_id"},
        {"alias": "last_legal_status_stat"},
        {"alias": "legal_status"},
        {"alias": "family_info"},
        {"alias": "cited_info"},
        {"alias": "citation_info"},
        {"alias": "title_translate_sear"},
        {"alias": "score", "sortType": "DESC", "sortOrder": 0},
        {"alias": "claims_num"},
        {"alias": "applicants_normal_all"},
        {"alias": "applicant_stand_all"},
        {"alias": "title_sear"},
        {"alias": "patent_type_cn_stat"},
        {"alias": "tech_problem"},
        {"alias": "tech_problem_phrase_stat"},
        {"alias": "tech_means"},
        {"alias": "tech_benefit"},
        {"alias": "tech_benefit_phrase_stat"},
        {"alias": "priority_date"},
        {"alias": "pct_application_date"},
        {"alias": "pct_publication_date"},
        {"alias": "application_num_sear"},
        {"alias": "publication_number_sear"},
        {"alias": "pct_application_num_sear"},
        {"alias": "pct_publication_number_sear"},
        {"alias": "applicants_normal_stat"},
        {"alias": "business_name_list"},
        {"alias": "applicant_stand_stat"},
        {"alias": "ipc_main_stat"},
        {"alias": "publication_country_stat"},
        {"alias": "citation_count"},
        {"alias": "cited_count"},
        {"alias": "applicants_count"},
        {"alias": "inventors_count"},
        {"alias": "assignees_count"},
        {"alias": "family_count"},
        {"alias": "family_country_size"},
        {"alias": "review_count"},
        {"alias": "invalid_count"},
        {"alias": "licensed_count"},
        {"alias": "transfered_count"},
        {"alias": "pledges_count"},
        {"alias": "lawsuit_count"},
        {"alias": "patent_status_stat"},
        {"alias": "award_level"},
        {"alias": "award_sess"},
        {"alias": "award_name"},
        {"alias": "user_label"},
        {"alias": "read_label_id"},
        {"alias": "patent_value_score"},
    ]
    DETAIL_REQUEST_COLS = [
        {"alias": "publication_number"},
        {"alias": "application_num"},
        {"alias": "title"},
        {"alias": "patent_status"},
        {"alias": "last_legal_status"},
        {"alias": "legal_status"},
        {"alias": "publication_date"},
        {"alias": "application_date"},
        {"alias": "applicants_normal_stat"},
        {"alias": "applicants_stat"},
        {"alias": "applicants"},
        {"alias": "patent_type_cn_stat"},
        {"alias": "main_cpc_num"},
        {"alias": "abstract_ab"},
        {"alias": "inventors"},
        {"alias": "assignees_normal_stat"},
        {"alias": "assignees_stat"},
        {"alias": "assignees"},
        {"alias": "application_country"},
        {"alias": "publication_country"},
        {"alias": "country_name"},
        {"alias": "province_name"},
        {"alias": "city_name"},
        {"alias": "district_name"},
        {"alias": "abstract_ab_tag"},
        {"alias": "claims_tag"},
        {"alias": "description_tag"},
        {"alias": "priority_num"},
        {"alias": "publication_type"},
        {"alias": "agent"},
        {"alias": "agency"},
        {"alias": "applicant_addr"},
        {"alias": "description"},
        {"alias": "claims_num"},
        {"alias": "claims"},
        {"alias": "patent_duration"},
        {"alias": "expired_date"},
        {"alias": "award_sess"},
        {"alias": "award_level"},
        {"alias": "priority_date"},
        {"alias": "ipc_main_stat"},
        {"alias": "ipc_main"},
        {"alias": "ipc_la"},
        {"alias": "ipc_li"},
        {"alias": "cpc_fi"},
        {"alias": "cpc_la"},
        {"alias": "cpc_li"},
        {"alias": "loc"},
        {"alias": "family_info"},
        {"alias": "citation_info"},
        {"alias": "cited_info"},
        {"alias": "title_translate_sear"},
        {"alias": "grant_date"},
    ]
    SOURCE_TYPE_ALIASES = {
        "\u53d1\u660e": {"\u53d1\u660e\u7533\u8bf7", "\u53d1\u660e\u6388\u6743", "\u53d1\u660e\u4e13\u5229"},
        "\u53d1\u660e\u7533\u8bf7": {"\u53d1\u660e\u7533\u8bf7"},
        "\u53d1\u660e\u6388\u6743": {"\u53d1\u660e\u6388\u6743", "\u53d1\u660e\u4e13\u5229"},
        "\u53d1\u660e\u4e13\u5229": {"\u53d1\u660e\u6388\u6743", "\u53d1\u660e\u4e13\u5229"},
        "\u5b9e\u7528\u65b0\u578b": {"\u5b9e\u7528\u65b0\u578b"},
        "\u5916\u89c2\u8bbe\u8ba1": {"\u5916\u89c2\u8bbe\u8ba1"},
    }

    def __init__(self):
        self.context = None
        self.page = None
        self.is_headful = False
        self.camoufox_cm = None
        self.playwright = None
        self.allow_headful_fallback = allow_headful_fallback_for("PATYEE")
        self._api_token = None
        self._api_user_id = None

    async def _launch_plain_playwright(self, profile_dir: str, force_headful: bool):
        print("Launching Patyee Playwright Chromium.")
        self.camoufox_cm = None
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=not force_headful,
            accept_downloads=True,
            ignore_https_errors=True,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    @staticmethod
    def _extract_year(date_text: str) -> int | None:
        if not date_text:
            return None
        match = re.search(r"(?:19|20)\d{2}", str(date_text))
        return int(match.group(0)) if match else None

    @staticmethod
    def _year_allowed(year: int | None, start_year: int = None, end_year: int = None) -> bool:
        if start_year is None and end_year is None:
            return True
        if year is None:
            return False
        if start_year is not None and year < start_year:
            return False
        if end_year is not None and year > end_year:
            return False
        return True

    @staticmethod
    def _as_text(value) -> str:
        if isinstance(value, list):
            return "; ".join(str(v).split("-", 1)[-1] for v in value if v)
        return str(value or "").strip()

    @staticmethod
    def _strip_html(value) -> str:
        if not value:
            return ""
        return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)

    @staticmethod
    def _api_body(payload: dict):
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        return data

    @staticmethod
    def _extract_pn_from_url(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        pn = (query.get("pn") or query.get("PNM") or query.get("publicNum") or [""])[0]
        if not pn and "=" not in url:
            pn = url.rsplit("/", 1)[-1]
        return urllib.parse.unquote(str(pn or "").strip())

    @staticmethod
    def _looks_like_expression(query: str) -> bool:
        return bool(re.search(r"(?:^|[\s(])[\w\u4e00-\u9fff（）()]+(?:\(\u7cbe\u786e\))?\s*=", query or ""))

    def _build_search_expression(self, query: str, search_field: str = None) -> str:
        query = (query or "").strip()
        if not query:
            return ""
        if self._looks_like_expression(query):
            return query

        field_map = {
            "all": "SS",
            "\u5168\u90e8": "SS",
            "\u4e3b\u9898": "SS",
            "\u7bc7\u5173\u6458": "TAC",
            "\u7bc7\u540d": "TI",
            "\u6807\u9898": "TI",
            "\u6458\u8981": "AB",
            "\u4f5c\u8005": "IN",
            "\u53d1\u660e\u4eba": "IN",
            "\u7533\u8bf7\u4eba": "PAS",
            "ti": "TI",
            "title": "TI",
            "abs": "AB",
            "abstract": "AB",
            "au": "IN",
        }
        field = field_map.get(str(search_field or "all").strip().lower()) or field_map.get(str(search_field or "all").strip(), "SS")
        return f"({field}=({query}))"

    def _source_type_allowed(self, patent_type: str, source_type: str = "all") -> bool:
        if not source_type or str(source_type).lower() == "all":
            return True
        patent_type = str(patent_type or "").strip()
        source_type = str(source_type or "").strip()
        allowed = self.SOURCE_TYPE_ALIASES.get(source_type)
        if allowed:
            return patent_type in allowed
        return source_type in patent_type

    def _request_cols(self, sort_by: str = "relevance") -> list[dict]:
        cols = [dict(col) for col in self.SEARCH_REQUEST_COLS]
        if sort_by == "date_desc":
            for col in cols:
                col.pop("sortType", None)
                col.pop("sortOrder", None)
            cols.insert(0, {"alias": "publication_date", "sortType": "DESC", "sortOrder": 0})
        return cols

    def _api_headers(self, *, json_content: bool = True) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.USER_AGENT,
            "Referer": f"{self.BASE_URL}/",
            "Origin": self.BASE_URL,
            "x-b3-traceid": secrets.token_hex(16),
            "x-b3-spanid": secrets.token_hex(8),
        }
        if self._api_token:
            headers["Authorization"] = self._api_token
        if json_content:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        return headers

    async def _api_login(self, *, force: bool = False) -> str:
        if self._api_token and not force:
            return self._api_token
        timeout = aiohttp.ClientTimeout(total=45, connect=15, sock_read=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self.TOKEN_URL, headers=self._api_headers(json_content=False), ssl=False) as resp:
                data = await resp.json(content_type=None)
        body = ((data.get("data") or {}).get("body") or {}) if isinstance(data, dict) else {}
        access_token = (body.get("access_token") or "").strip()
        token_type = (body.get("token_type") or "bearer").strip()
        if not access_token:
            raise RuntimeError(f"Patyee ip token did not return access_token: {data.get('message') if isinstance(data, dict) else ''}")
        self._api_token = f"{token_type} {access_token}"
        self._api_user_id = str(body.get("userId") or self._api_user_id or "1598585147396067328")
        return self._api_token

    async def _api_json(self, method: str, url: str, *, payload: dict = None, retry: bool = True) -> dict:
        await self._api_login()
        timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            request = session.post if method.upper() == "POST" else session.get
            kwargs = {"headers": self._api_headers(json_content=payload is not None), "ssl": False}
            if payload is not None:
                kwargs["json"] = payload
            async with request(url, **kwargs) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception as e:
                    raise RuntimeError(f"Patyee API returned non-JSON HTTP {resp.status}: {text[:200]}") from e

        status = data.get("status") if isinstance(data, dict) else None
        code = data.get("code") if isinstance(data, dict) else None
        if (status in {401, 403} or code in {401, 403}) and retry:
            self._api_token = None
            await self._api_login(force=True)
            return await self._api_json(method, url, payload=payload, retry=False)
        if status not in (0, 1, None) or code not in (0, None):
            raise RuntimeError(f"Patyee API error: {code or status} {data.get('message') if isinstance(data, dict) else ''}")
        return data

    def _search_payload(
        self,
        search_expression: str,
        *,
        limit: int,
        offset: int,
        sort_by: str,
        search: bool = True,
    ) -> dict:
        return {
            "useScroll": False,
            "expreType": 1,
            "modelId": self.MODEL_ID,
            "offset": offset,
            "queryId": None,
            "requestCols": self._request_cols(sort_by),
            "labelUserIds": [self._api_user_id or "1598585147396067328"],
            "pageSize": limit,
            "searchExpre": search_expression,
            "translation": False,
            "includeLabels": [],
            "excludeLabels": [],
            "mergeIncludeLabels": [],
            "mergeExcludeLabels": [],
            "readLabelParam": {"includeReadLabels": [], "excludeReadLabels": []},
            "mergeType": 0,
            "customizeFieldParam": {},
            "conditions": [],
            "searchNew": False,
            "batchSearch": False,
            "readLabelParse": True,
            "search": search,
        }

    def _detail_payload(self, pn: str) -> dict:
        return {
            "useScroll": False,
            "expreType": 1,
            "modelId": self.MODEL_ID,
            "offset": 0,
            "pageSize": 1,
            "queryId": None,
            "requestCols": [dict(col) for col in self.DETAIL_REQUEST_COLS],
            "details": True,
            "searchExpre": f"PN=({pn})",
            "pn": "",
        }

    def _detail_from_item(self, item: dict, pn: str) -> Dict:
        abstract = self._strip_html(item.get("abstract_ab") or item.get("abstract") or "")
        keywords = []
        for key in ["ipc_main_stat", "ipc_main", "ipc_la", "main_cpc_num", "cpc_la"]:
            value = self._as_text(item.get(key))
            if value and value not in keywords:
                keywords.append(value)
        return {
            "abstract": abstract or "No abstract found",
            "keywords": keywords,
            "doi": pn,
        }

    async def _api_load_detail_item(self, pn: str) -> dict:
        data = await self._api_json("POST", self.SEARCH_API_URL, payload=self._detail_payload(pn))
        body = self._api_body(data)
        items = body.get("result") if isinstance(body, dict) else []
        return (items or [{}])[0]

    async def _api_get_paper_details(self, url: str) -> Dict:
        pn = self._extract_pn_from_url(url)
        if not pn:
            raise RuntimeError("Patyee detail URL did not contain pn")
        item = await self._api_load_detail_item(pn)
        return self._detail_from_item(item, pn)

    async def _api_extract_pdf_url(self, url: str) -> str:
        pn = self._extract_pn_from_url(url)
        if not pn:
            raise RuntimeError("Patyee detail URL did not contain pn")
        payload = {"fileType": 4, "internal": False, "publicNums": [pn], "needWaterMark": True}
        data = await self._api_json("POST", self.FILE_LIST_URL, payload=payload)
        files = data.get("data") if isinstance(data, dict) else []
        for entry in files or []:
            for pdf in entry.get("pdfFile") or []:
                file_path = (pdf.get("filePath") or "").strip()
                if file_path:
                    return file_path
        raise RuntimeError("Patyee file API did not return a PDF URL")

    async def _api_search_papers(
        self,
        query: str,
        limit: int = 10,
        source_type: str = "all",
        start_year: int = None,
        end_year: int = None,
        search_field: str = "all",
        sort_by: str = "relevance",
        start_index: int = 0,
    ) -> Dict:
        limit = max(0, int(limit or 0))
        start_index = max(0, int(start_index or 0))
        fetch_limit = max(20, min(100, limit * 5 if (source_type and source_type != "all") or start_year or end_year else limit or 20))
        search_expression = self._build_search_expression(query, search_field=search_field)
        payload = self._search_payload(search_expression, limit=fetch_limit, offset=start_index, sort_by=sort_by)
        data = await self._api_json("POST", self.SEARCH_API_URL, payload=payload)
        return self._papers_from_search_payload(
            data,
            limit,
            start_year=start_year,
            end_year=end_year,
            source_type=source_type,
        )

    def _papers_from_search_payload(self, payload: dict, limit: int, start_year: int = None, end_year: int = None, source_type: str = "all") -> Dict:
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}

        papers = []
        for item in data.get("result") or []:
            if len(papers) >= limit:
                break
            date_info = item.get("publication_date") or item.get("application_date") or ""
            if not self._year_allowed(self._extract_year(date_info), start_year=start_year, end_year=end_year):
                continue
            if not self._source_type_allowed(item.get("patent_type_cn_stat"), source_type=source_type):
                continue

            pn = item.get("publication_number_sear") or item.get("application_num_sear") or item.get("family_info", [""])[0]
            title = self._strip_html(item.get("title_sear") or item.get("title_translate_sear") or "Unknown Title")
            applicant = self._as_text(item.get("applicants_normal_stat") or item.get("applicants_normal_all"))
            papers.append(
                {
                    "id": pn,
                    "title": title,
                    "author": applicant,
                    "source": f"Patyee ({pn})" if pn else "Patyee",
                    "date": date_info,
                    "db_type": item.get("patent_type_cn_stat") or "Patent",
                    "detail_link": f"https://www.patyee.com/innovationSpace/searchDetail?pn={urllib.parse.quote(pn)}" if pn else "",
                }
            )

        return {"total_results": data.get("total") or len(papers), "papers": papers}
        
    async def initialize(self):
        await self._ensure_browser()

    async def _ensure_browser(self, force_headful=False):
        if not self.context:
            print(f"Initializing Patyee Persistent Browser Context (Headless: {not force_headful})...")
            # Patyee runs plain Chromium; pooled profile for concurrency safety.
            from scraper_utils import pooled_profile
            profile_dir, self._profile_ephemeral = pooled_profile(".patyee_profile", "PATYEE")
            self._profile_dir = profile_dir

            # Firefox-style parent.lock cleanup for Camoufox crashes.
            for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
                lfile = os.path.join(profile_dir, lock_name)
                if os.path.exists(lfile):
                    try: os.remove(lfile)
                    except: pass
            
            self.is_headful = force_headful
            await self._launch_plain_playwright(profile_dir, force_headful)
            
            print("Navigating to Patyee...")
            await self.page.goto("https://www.patyee.com/", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            
            # Smart wait for Anti-Bot or Login if needed
            title = await self.page.title()
            if "安全" in title or "验证" in title:
                print("Encountered Patyee security check!")
                if not force_headful:
                    if not self.allow_headful_fallback:
                        print("[Anti-Bot] Patyee security check blocked headless mode; headful fallback is disabled.")
                        return
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    return
            print("Patyee context initialized successfully.")

    async def search_papers(self, query: str, limit: int = 10, source_type: str = "all", start_year: int = None, end_year: int = None, **kwargs) -> Dict:
        search_field = kwargs.get("search_field") or "all"
        sort_by = kwargs.get("sort_by") or "relevance"
        start_index = kwargs.get("start_index") or 0
        try:
            api_result = await self._api_search_papers(
                query,
                limit=limit,
                source_type=source_type,
                start_year=start_year,
                end_year=end_year,
                search_field=search_field,
                sort_by=sort_by,
                start_index=start_index,
            )
            if api_result["papers"] or api_result["total_results"]:
                print(f"[Patyee] Direct API search found {api_result['total_results']} total results, returned {len(api_result['papers'])} patents.")
                return api_result
        except Exception as e:
            print(f"[Patyee] Direct API search failed ({e}); falling back to browser.")

        await self._ensure_browser()
        
        # Build smart query
        smart_query = query
        
        # We append standard keywords to the query so Patyee's intelligent engine can parse it
        if source_type != "all" and source_type:
            smart_query += f" {source_type}"
            
        if start_year and end_year:
            smart_query += f" {start_year}-{end_year}"
        elif start_year:
            smart_query += f" {start_year}"
        elif end_year:
            smart_query += f" {end_year}"
            
        print(f"[Patyee] Searching for '{smart_query}'...")
        
        # Go to home page to use search box
        await self.page.goto("https://www.patyee.com/home", wait_until="domcontentloaded", timeout=60000)
        await self.page.wait_for_selector("#content-table", timeout=30000)
        await asyncio.sleep(2)
        
        # Click the contenteditable div directly, then type
        try:
            print("[Patyee] Evaluating search...")
            # Use javascript to set value and click
            escaped_query = json.dumps(smart_query, ensure_ascii=False)
            await self.page.evaluate(f'''() => {{
                let el = document.querySelector('#content-table');
                if(el) {{
                    el.innerText = {escaped_query};
                    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: {escaped_query} }}));
                }}
            }}''')
            await asyncio.sleep(1)
            async with self.page.expect_response(
                lambda resp: "/ourchem-patyee/patyee/search/search" in resp.url and resp.request.method == "POST",
                timeout=60000,
            ) as response_info:
                await self.page.evaluate('''() => {
                    let btn = document.querySelector('.pat-search');
                    if (btn) btn.click();
                }''')
            response = await response_info.value
            api_result = self._papers_from_search_payload(
                await response.json(),
                limit,
                start_year=start_year,
                end_year=end_year,
                source_type=source_type,
            )
            if api_result["papers"]:
                print(f"[Patyee] API search found {api_result['total_results']} total results, returned {len(api_result['papers'])} patents.")
                return api_result
            
        except Exception as e:
            print(f"[Patyee] API path failed ({e}); falling back to HTML parsing.")

        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        try:
            await self.page.wait_for_selector(".pn-info-wrap", timeout=8000)
        except Exception:
            pass

        html = await self.page.content()
        soup = BeautifulSoup(html, 'html.parser')
        
        # Try to parse total results (e.g. class containing 'total' or pagination)
        total_results = 0
        try:
            total_text = soup.find(string=re.compile(r'1\s*/\s*([\d,]+)'))
            if total_text:
                m = re.search(r'/\s*([\d,]+)', total_text)
                if m:
                    total_results = int(m.group(1).replace(',', ''))
        except:
            pass
            
        items = soup.select('.pn-info-wrap')
        if total_results == 0:
            total_results = len(items)
            
        papers = []
        for item in items:
            if len(papers) >= limit:
                break
            data = {}
            for wrap in item.select('.item-wrap'):
                label = wrap.select_one('.label-wrap')
                value = wrap.select_one('.value-wrap')
                if label and value:
                    data[label.text.strip()] = value.text.strip()
                    
            pn = data.get('公开号', '') or data.get('公开(公告)号', '')
            title = data.get('专利名称', '') or data.get('专利标题', 'Unknown Title')
            applicant = data.get('申请人', '') or data.get('申请人(权利人)', '')
            date_info = data.get('公开日', '') or data.get('申请日', '')
            if not self._year_allowed(self._extract_year(date_info), start_year=start_year, end_year=end_year):
                continue
            
            detail_link = f"https://www.patyee.com/innovationSpace/searchDetail?pn={pn}"
            
            if pn:
                papers.append({
                    "id": pn,
                    "title": title,
                    "author": applicant,
                    "source": f"Patyee ({pn})",
                    "date": date_info,
                    "detail_link": detail_link
                })
                    
        print(f"[Patyee] Search found {total_results} total results, returned {len(papers)} patents.")
        return {
            "total_results": total_results,
            "papers": papers
        }

    async def get_paper_details(self, url: str) -> Dict:
        try:
            details = await self._api_get_paper_details(url)
            print(f"[Patyee] Direct API detail fetched for {url}")
            return details
        except Exception as e:
            print(f"[Patyee] Direct API detail failed ({e}); falling back to browser.")

        await self._ensure_browser()
        print(f"[Patyee] Fetching details for {url}")

        from scraper_utils import goto_with_retry
        await goto_with_retry(self.page, url, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(3)
        
        html = await self.page.content()
        soup = BeautifulSoup(html, 'html.parser')
        
        abstract = "No abstract found"
        keywords = []
        
        # Try the precise abstract extraction
        try:
            for el in soup.find_all(string=lambda t: t and '摘要' == t.strip()):
                if el.parent:
                    sibling = el.parent.find_next_sibling()
                    if sibling and sibling.name == 'div':
                        abstract = sibling.get_text(separator=' ', strip=True)
                        break
        except Exception as e:
            print("Failed precise abstract parsing:", e)

        # Fallback if precise extraction failed
        if abstract == "No abstract found":
            text_content = soup.get_text(separator=' ', strip=True)
            if "摘要" in text_content:
                try:
                    abs_header = soup.find(string=re.compile(r'^摘要$'))
                    if abs_header and abs_header.parent:
                        next_node = abs_header.parent.find_next_sibling()
                        if next_node:
                            abstract = next_node.get_text(separator=' ', strip=True)
                except:
                    pass
                
        # Also try extracting IPC/CPC
        try:
            ipc_nodes = soup.find_all(string=re.compile(r'IPC分类号:'))
            if ipc_nodes and ipc_nodes[0].parent:
                ipc_text = ipc_nodes[0].parent.parent.get_text(strip=True)
                keywords.append(ipc_text)
        except:
            pass

        pn = ""
        if "pn=" in url:
            pn = url.split("pn=")[1].split("&")[0]

        return {
            "abstract": abstract,
            "keywords": keywords,
            "doi": pn
        }

    async def download_paper(self, url: str, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)

        pn = self._extract_pn_from_url(url) or "patyee_doc"
        try:
            pdf_url = await self._api_extract_pdf_url(url)
            file_path = os.path.join(output_dir, f"{safe_stem(pn)}.pdf")
            timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = {"User-Agent": self.USER_AGENT, "Referer": f"{self.BASE_URL}/"}
                async with session.get(pdf_url, headers=headers, ssl=False) as resp:
                    content = await resp.read()
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    if not content.lstrip().startswith(b"%PDF"):
                        raise RuntimeError("response was not a PDF")
            with open(file_path, "wb") as f:
                f.write(content)
            print(f"[Patyee] Downloaded PDF via direct API: {file_path}")
            return file_path
        except Exception as e:
            print(f"[Patyee] Direct API PDF download failed ({e}); falling back to browser.")

        await self._ensure_browser()
        print(f"[Patyee] Navigating to download: {url}")
        
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(4)
        
        try:
            print("[Patyee] Clicking '专利原文' tab...")
            # Look for the Original Patent text tab
            tabs = await self.page.locator('li').all()
            clicked = False
            for tab in tabs:
                text = await tab.inner_text()
                if "专利原文" in text:
                    await tab.click()
                    clicked = True
                    break
                    
            if not clicked:
                raise Exception("Could not find '专利原文' tab.")
                
            await asyncio.sleep(4)
            
            # Patyee exposes the raw OSS URL in the HTML directly
            html = await self.page.content()
            match = re.search(r'file=(https?[^&"]+)', html)
            if not match:
                raise Exception("Could not extract PDF URL from iframe or viewer link.")
                
            pdf_url = urllib.parse.unquote(match.group(1))
            print(f"[Patyee] Intercepted Direct PDF URL: {pdf_url[:80]}...")
            
            # Download it using aiohttp
            pn = url.split("pn=")[1].split("&")[0] if "pn=" in url else "patyee_doc"
            file_name = f"{pn}.pdf"
            file_path = os.path.join(output_dir, file_name)
            
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(pdf_url, ssl=False, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            with open(file_path, "wb") as f:
                                f.write(content)
                            print(f"[Patyee] Downloaded PDF successfully: {file_path}")
                            return file_path
                        else:
                            raise Exception(f"Failed to download PDF. Status: {resp.status}")
                except Exception as dl_e:
                    print(f"[Patyee] Direct download failed ({dl_e}), trying playwright download...")
                    # Fallback to playwright
                    await self.page.goto(pdf_url)
                    await asyncio.sleep(10) # Let it load
                    return "Fallback not implemented fully, manual download required"
                        
        except Exception as e:
            print(f"[Patyee] Download failed: {e}")
            raise e

    async def read_paper_content(self, url: str, output_dir: str) -> tuple[str, str]:
        os.makedirs(output_dir, exist_ok=True)
        try:
            pdf_path = await self.download_paper(url, output_dir)
        except Exception as e:
            print(f"Error downloading patent: {e}")
            raise e
            
        print(f"Parsing downloaded PDF with checked converter: {pdf_path}")
        converted = convert_pdf_to_markdown(pdf_path, output_dir, image_format="png")
        if converted.image_only:
            print("PDF has no extractable text; generated image-only Markdown.")
        print(f"Markdown generation complete. Saved to: {converted.md_path}")
        return converted.md_path, converted.preview

    async def close(self):
        try:
            if self.context:
                await self.context.close()
                self.context = None
            if self.camoufox_cm:
                self.camoufox_cm = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            if getattr(self, "_profile_dir", None):
                from scraper_utils import cleanup_pooled_profile
                cleanup_pooled_profile(self._profile_dir, getattr(self, "_profile_ephemeral", False))
                self._profile_dir = None
            print("Patyee Context closed cleanly.")
        except Exception as e:
            print(f"Error closing Patyee context: {e}")
