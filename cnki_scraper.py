import os
import asyncio
import hashlib
import json
import math
import re
import sys
import time
import urllib.parse
from typing import List, Dict, Optional
import aiohttp
from playwright.async_api import async_playwright
import bs4
import pymupdf4llm
from mcp_logging import safe_stderr_print

class CNKIScraper:
    CROSSDB_CODES = "YSTT4HG0,LSTPFY1C,JUP3MUPD,MPMFIG1A,WQ0UVIAA,BLZOG7CK,PWFIRAGL,EMRPGLPA,NLBO1Z6R,NN3FJMUV"
    PRODUCT_STR = (
        "YSTT4HG0,LSTPFY1C,RMJLXHZ3,JQIRZIYA,JUP3MUPD,1UR4K4HZ,"
        "BPBAFJ5S,R79MZMCB,MPMFIG1A,WQ0UVIAA,NB3BWEHK,XVLO76FD,"
        "HR1YT1Z9,BLZOG7CK,PWFIRAGL,EMRPGLPA,J708GVCE,ML4DRIDX,"
        "NLBO1Z6R,NN3FJMUV,"
    )
    STARTER_RC = "CJFQ,CDMD,CIPD,CCND,CISD,SNAD,CCJD,BDZK,CCVD,CJFN"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.is_headful = False
        from runtime_config import profile_path
        self.profile_dir = profile_path(".cnki_profile")
        self.storage_state_path = os.path.abspath(os.environ.get("CNKI_STORAGE_STATE", ".cnki_storage_state.json"))

    def _log(self, *parts) -> None:
        safe_stderr_print(*parts)
    
    async def initialize(self, force_headful: bool = False):
        await self._ensure_browser(force_headful=force_headful)

    async def _ensure_browser(self, force_headful: bool = False):
        if self.context:
            return

        self.is_headful = force_headful
        # CNKI runs plain Chromium (not Camoufox) and keeps its own storage_state
        # cookie file, so no shared-fingerprint here -- but it still needs a
        # pooled profile dir so two MCP clients don't fight over one Chromium
        # SingletonLock.
        from scraper_utils import pooled_profile
        self.profile_dir, self._profile_ephemeral = pooled_profile(".cnki_profile", "CNKI")
        os.makedirs(self.profile_dir, exist_ok=True)
        # Firefox-style parent.lock cleanup for Camoufox crashes.
        for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
            lock_path = os.path.join(self.profile_dir, lock_name)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass

        self.playwright = await async_playwright().start()

        # Persistent context keeps CNKI login / verification cookies between MCP calls.
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            headless=not force_headful,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            accept_downloads=True,
            ignore_https_errors=True
        )
        await self._load_storage_state_cookies()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def _load_storage_state_cookies(self) -> None:
        if not os.path.exists(self.storage_state_path):
            return
        try:
            with open(self.storage_state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            cookies = state.get("cookies", [])
            if cookies:
                await self.context.add_cookies(cookies)
                self._log(f"Loaded CNKI storage-state cookies: {len(cookies)}")
        except Exception as e:
            self._log(f"Could not load CNKI storage state cookies: {e}")

    async def _save_storage_state_cookies(self) -> None:
        if not self.context:
            return
        try:
            await self.context.storage_state(path=self.storage_state_path)
        except Exception as e:
            self._log(f"Could not save CNKI storage state cookies: {e}")

    def _storage_state_cookies(self) -> List[Dict]:
        if not os.path.exists(self.storage_state_path):
            return []
        try:
            with open(self.storage_state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state.get("cookies", []) or []
        except Exception as e:
            self._log(f"Could not read CNKI storage state for direct request: {e}")
            return []

    @staticmethod
    def _domain_matches(cookie_domain: str, host: str) -> bool:
        cookie_domain = (cookie_domain or "").lstrip(".").lower()
        host = (host or "").lower()
        return bool(cookie_domain and (host == cookie_domain or host.endswith("." + cookie_domain)))

    def _cookie_header_for_host(self, host: str) -> str:
        now = time.time()
        matches = []
        for cookie in self._storage_state_cookies():
            expires = cookie.get("expires", -1)
            if expires not in (-1, None) and expires < now:
                continue
            if self._domain_matches(cookie.get("domain", ""), host):
                matches.append(cookie)

        matches.sort(key=lambda c: len((c.get("domain") or "").lstrip(".")), reverse=True)
        pairs = []
        seen = set()
        for cookie in matches:
            name = cookie.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            pairs.append(f"{name}={cookie.get('value', '')}")
        return "; ".join(pairs)

    def _active_cookie_value(self, host: str, name: str) -> str:
        now = time.time()
        for cookie in self._storage_state_cookies():
            expires = cookie.get("expires", -1)
            if expires not in (-1, None) and expires < now:
                continue
            if cookie.get("name") == name and self._domain_matches(cookie.get("domain", ""), host):
                return cookie.get("value", "")
        return ""

    def _has_fresh_cnki_login(self, min_ttl_seconds: int = 60) -> bool:
        now = time.time()
        required = {"LID", "Ecp_LoginStuts", "c_m_LinID"}
        found = set()
        for cookie in self._storage_state_cookies():
            name = cookie.get("name")
            if name not in required:
                continue
            if not self._domain_matches(cookie.get("domain", ""), "kns.cnki.net"):
                continue
            expires = cookie.get("expires", -1)
            if expires in (-1, None) or expires > now + min_ttl_seconds:
                if cookie.get("value"):
                    found.add(name)
        return required.issubset(found)

    @staticmethod
    def _js_encode_uri_component(value: str) -> str:
        return urllib.parse.quote(str(value or ""), safe="-_.!~*'()")

    @staticmethod
    def _cnki_expire_timestamp(expire_text: str) -> float:
        try:
            return time.mktime(time.strptime(expire_text, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return time.time() + 20 * 60

    def _cnki_login_sign_headers(self, sign_url: str) -> Dict[str, str]:
        client_id = self._active_cookie_value("login.cnki.net", "Ecp_ClientId")
        if not client_id:
            return {}

        parsed = urllib.parse.urlparse(sign_url)
        params = {}
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if key:
                params[key] = urllib.parse.unquote(value)
        sorted_params = []
        for key in sorted(params, key=lambda item: item.lower()):
            sorted_params.extend([key, "=", params[key]])

        timestamp = str(int(time.time() * 1000))
        nonce = str(math.sin(int(timestamp)))[6:]
        signature_source = (
            timestamp
            + nonce
            + "t8b52yrsoyx66f35tk0p4nubrmrcglv5"
            + "".join(sorted_params)
            + client_id
        ).replace(" ", "")
        return {
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": hashlib.md5(signature_source.encode("utf-8")).hexdigest(),
            "appID": "LoginWap",
            "ClientID": client_id,
        }

    def _build_cnki_login_cookies(self, result: Dict) -> List[Dict]:
        expire_text = result.get("Expire") or ""
        expires = self._cnki_expire_timestamp(expire_text)
        expire_plus = expires + 5
        uid = result.get("Uid") or ""

        members = []
        for member in result.get("Members") or []:
            members.append(
                {
                    "Le": self._js_encode_uri_component(urllib.parse.unquote(str(member.get("Le", "")))),
                    "ET": self._js_encode_uri_component(urllib.parse.unquote(str(member.get("ET", "")))),
                }
            )

        login_status = {
            "IsAutoLogin": bool(result.get("IsAutoLogin")),
            "UserName": result.get("UserName") or "",
            "ShowName": self._js_encode_uri_component(urllib.parse.unquote(result.get("ShowName") or "")),
            "UserType": result.get("UserType") or "",
            "BUserName": result.get("BUserName") or "",
            "BShowName": self._js_encode_uri_component(urllib.parse.unquote(result.get("BShowName") or "")),
            "BUserType": result.get("BUserType") or "",
            "r": result.get("r") or "",
            "Members": members,
        }

        def cookie(name: str, value: str, cookie_expires: float):
            return {
                "name": name,
                "value": str(value),
                "domain": ".cnki.net",
                "path": "/",
                "expires": cookie_expires,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }

        expire_local = time.localtime(expires)
        expire_plus_local = time.localtime(expire_plus)
        ot = urllib.parse.quote(time.strftime("%m/%d/%Y %H:%M:%S", expire_local), safe="")
        expire_value = urllib.parse.quote(time.strftime("%Y-%m-%d %H:%M:%S", expire_plus_local), safe="")

        cookies = [
            cookie("Ecp_notFirstLogin", result.get("r") or "", expires),
            cookie("Ecp_LoginStuts", json.dumps(login_status, ensure_ascii=False, separators=(",", ":")), expires),
            cookie("c_m_expire", expire_value, expire_plus),
            cookie("Ecp_session", "1", -1),
        ]
        if uid:
            cookies.extend(
                [
                    cookie("c_m_LinID", f"LinID={uid}&ot={ot}", expires),
                    cookie("LID", uid, expires),
                ]
            )
        return cookies

    async def _save_direct_cnki_login_cookies(self, cookies: List[Dict]) -> None:
        try:
            if os.path.exists(self.storage_state_path):
                with open(self.storage_state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            else:
                state = {"cookies": [], "origins": []}

            replace_names = {cookie.get("name") for cookie in cookies}
            replace_names.update({"Ecp_lout"})
            existing = state.get("cookies", []) or []
            state["cookies"] = [
                cookie
                for cookie in existing
                if not (cookie.get("domain") == ".cnki.net" and cookie.get("name") in replace_names)
            ] + cookies
            state.setdefault("origins", [])

            os.makedirs(os.path.dirname(self.storage_state_path), exist_ok=True)
            with open(self.storage_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)

            if self.context:
                await self.context.add_cookies(cookies)
        except Exception as e:
            self._log(f"Could not save refreshed CNKI institution cookies: {e}")

    async def _refresh_cnki_institution_login_direct(self, force: bool = False) -> bool:
        if not force and self._has_fresh_cnki_login():
            return True

        client_id = self._active_cookie_value("login.cnki.net", "Ecp_ClientId")
        if not client_id:
            self._log("CNKI direct institution refresh skipped: Ecp_ClientId is missing.")
            return False

        fingerprint = hashlib.md5((client_id + "|cnki").encode("utf-8")).hexdigest()
        sign_url = (
            "https://login.cnki.net/TopLoginCore/api/loginapi/IpLoginFlushPo"
            + "?fingerprint="
            + urllib.parse.quote(fingerprint)
        )
        sign_headers = self._cnki_login_sign_headers(sign_url)
        if not sign_headers:
            return False

        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/plain, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": "https://kns.cnki.net",
            "Referer": "https://kns.cnki.net/",
            **sign_headers,
        }
        cookie_header = self._cookie_header_for_host("login.cnki.net")
        if cookie_header:
            headers["Cookie"] = cookie_header

        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
                async with session.post(
                    "https://login.cnki.net/TopLoginCore/api/loginapi/IpLoginFlushPo",
                    headers=headers,
                    data=json.dumps({"fingerprint": fingerprint}, separators=(",", ":")),
                ) as response:
                    text = await response.text()
                    if response.status >= 400:
                        self._log(f"CNKI direct institution refresh failed with HTTP {response.status}.")
                        return False
        except Exception as e:
            self._log(f"CNKI direct institution refresh request failed: {e}")
            return False

        raw = text.strip()
        if raw.startswith("(") and raw.endswith(")"):
            raw = raw[1:-1]
        try:
            result = json.loads(raw)
        except Exception as e:
            self._log(f"CNKI direct institution refresh returned unparseable payload: {e}")
            return False

        if not result.get("IsSuccess"):
            self._log(
                "CNKI direct institution refresh did not log in:",
                result.get("ErrorCode"),
                result.get("ErrorMsg") or result.get("Msg"),
            )
            return False

        cookies = self._build_cnki_login_cookies(result)
        await self._save_direct_cnki_login_cookies(cookies)
        self._log(
            "CNKI institution IP login refreshed:",
            result.get("UserName"),
            result.get("ShowName"),
            "expires",
            result.get("Expire"),
        )
        return True

    def _direct_headers(self, url: str, *, referer: str = None, ajax: bool = False) -> Dict[str, str]:
        host = urllib.parse.urlparse(url).hostname or ""
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html, */*; q=0.01" if ajax else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        cookie_header = self._cookie_header_for_host(host)
        if cookie_header:
            headers["Cookie"] = cookie_header
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Origin"] = "https://kns.cnki.net"
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        return headers

    async def _direct_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        referer: str = None,
        data=None,
        ajax: bool = False,
        max_redirects: int = 10,
    ):
        current_url = url
        current_method = method.upper()
        current_data = data
        current_referer = referer
        history = []

        for _ in range(max_redirects + 1):
            headers = self._direct_headers(current_url, referer=current_referer, ajax=ajax and current_data is not None)
            async with session.request(
                current_method,
                current_url,
                headers=headers,
                data=current_data,
                allow_redirects=False,
            ) as response:
                body = await response.read()
                response_headers = dict(response.headers)
                final_url = str(response.url)
                history.append(
                    (
                        response.status,
                        final_url,
                        response_headers.get("Location"),
                        response_headers.get("Content-Type"),
                        len(body),
                    )
                )

                location = response_headers.get("Location")
                if response.status in (301, 302, 303, 307, 308) and location:
                    current_referer = final_url
                    current_url = urllib.parse.urljoin(final_url, location)
                    if response.status not in (307, 308):
                        current_method = "GET"
                        current_data = None
                    continue

                return response.status, final_url, response_headers, body, history

        return response.status, final_url, response_headers, body, history

    @staticmethod
    def _normalize_cnki_option(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return text
        try:
            repaired = text.encode("gb18030").decode("utf-8")
            if repaired and repaired != text:
                return repaired
        except UnicodeError:
            pass
        return text

    @classmethod
    def _field_spec(cls, search_field: str):
        normalized = cls._normalize_cnki_option(search_field)
        field_map = {
            "主题": ("SU", "主题"),
            "篇关摘": ("TKA", "篇关摘"),
            "关键词": ("KY", "关键词"),
            "篇名": ("TI", "篇名"),
            "全文": ("FT", "全文"),
            "作者": ("AU", "作者"),
            "第一作者": ("FI", "第一作者"),
            "通讯作者": ("RP", "通讯作者"),
            "作者单位": ("AF", "作者单位"),
            "基金": ("FU", "基金"),
            "摘要": ("AB", "摘要"),
            "小标题": ("LY", "小标题"),
            "参考文献": ("RF", "参考文献"),
            "分类号": ("CLC", "分类号"),
            "文献来源": ("SO", "文献来源"),
            "DOI": ("DOI", "DOI"),
        }
        return field_map.get(normalized)

    @classmethod
    def _is_total_db_scope(cls, db_scope: str) -> bool:
        normalized = cls._normalize_cnki_option(db_scope)
        return normalized in ("", "all", "总库")

    @staticmethod
    def _date_range_filter(start_year: int = None, end_year: int = None) -> Optional[Dict[str, str]]:
        if start_year is None and end_year is None:
            return None
        try:
            start = int(start_year) if start_year is not None else 1900
            end = int(end_year) if end_year is not None else time.localtime().tm_year + 5
        except Exception:
            return None
        if start > end:
            return None
        return {"field": "PT", "start": str(start), "end": str(end)}

    def _build_direct_search_payload(
        self,
        query: str,
        field_code: str,
        field_title: str,
        page_num: int,
        *,
        date_range: Dict[str, str] = None,
        turnpage: str = None,
        search_from: int = 1,
        bool_search: bool = True,
        sort_field: str = "",
        sort_type: str = "",
    ) -> Dict[str, str]:
        qgroups = [
            {
                "Key": "Subject",
                "Title": "",
                "Logic": 0,
                "Items": [
                    {
                        "Field": field_code,
                        "Value": query,
                        "Operator": "TOPRANK",
                        "Logic": 0,
                        "Title": field_title,
                    }
                ],
                "ChildItems": [],
            }
        ]
        if date_range:
            start = date_range["start"]
            end = date_range["end"]
            field = date_range["field"]
            qgroups.append(
                {
                    "Key": "SCDBGroup",
                    "Title": "",
                    "Logic": 0,
                    "Items": [],
                    "ChildItems": [
                        {
                            "Key": field,
                            "Title": "",
                            "Logic": 0,
                            "Items": [
                                {
                                    "Key": f"{start}-{end}",
                                    "Title": f"{start}-{end}",
                                    "Logic": 1,
                                    "Field": field,
                                    "Operator": "BETWEEN",
                                    "Value": start,
                                    "Value2": end,
                                    "Name": field,
                                    "ExtendType": 0,
                                }
                            ],
                            "ChildItems": [],
                        }
                    ],
                }
            )

        query_json = {
            "Platform": "",
            "Resource": "CROSSDB",
            "Classid": "WD0FTY92",
            "Products": "",
            "QNode": {"QGroup": qgroups},
            "ExScope": 1,
            "SearchType": 2,
            "Rlang": "BOTH",
            "KuaKuCode": self.CROSSDB_CODES,
            "Expands": {},
            "View": "changeDBCh",
            "SearchFrom": search_from,
        }
        payload = {
            "boolSearch": "true" if bool_search else "false",
            "QueryJson": json.dumps(query_json, ensure_ascii=False, separators=(",", ":")),
            "pageNum": str(page_num),
            "pageSize": "20",
            "sortField": sort_field,
            "sortType": sort_type,
            "dstyle": "listmode",
            "productStr": self.PRODUCT_STR,
            "aside": f"({field_title}：{query})" if bool_search else "",
            "searchFrom": "资源范围：总库",
            "subject": "",
            "language": "",
            "uniplatform": "",
        }
        if bool_search:
            payload["CurPage"] = "1"
        if turnpage:
            payload["turnpage"] = turnpage
        if search_from != 1 or not bool_search:
            payload["boolSortSearch"] = "false"
        if search_from != 1 and bool_search and not sort_field and not sort_type:
            payload.pop("sortField", None)
            payload.pop("sortType", None)
        return payload

    @staticmethod
    def _direct_search_url(query: str, field_code: str) -> str:
        rc = urllib.parse.quote(CNKIScraper.STARTER_RC)
        kw = urllib.parse.quote(query)
        return f"https://kns.cnki.net/starter?rc={rc}&kw={kw}&rt=crossdb&fd={field_code}"

    async def close(self):
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        if self.context:
            await self._save_storage_state_cookies()
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        from scraper_utils import cleanup_pooled_profile
        cleanup_pooled_profile(self.profile_dir, getattr(self, "_profile_ephemeral", False))

    async def _goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 45000):
        return await self.page.goto(url, wait_until=wait_until, timeout=timeout)

    async def _is_security_page(self, page=None) -> bool:
        page = page or self.page
        try:
            title = await page.title()
            html = await page.content()
            url = page.url
        except Exception:
            return False

        markers = [
            "安全验证",
            "向右滑动完成验证",
            "拖动下方拼图完成验证",
            "captchaType=blockPuzzle",
            "/verify/home",
        ]
        haystack = f"{title}\n{url}\n{html[:4000]}"
        return any(marker in haystack for marker in markers)

    async def _wait_for_manual_security_clear(self, page=None, timeout: int = 90) -> bool:
        page = page or self.page
        if not await self._is_security_page(page):
            return True

        self._log(">>> CNKI security verification is open. Please solve it in the browser window. <<<")
        for _ in range(timeout):
            if not await self._is_security_page(page):
                await self._save_storage_state_cookies()
                return True
            await asyncio.sleep(1)
        return False

    async def _recover_from_security_if_needed(self, retry_callback):
        if not await self._is_security_page():
            return None

        if not self.is_headful:
            self._log("[Anti-Bot] CNKI security verification detected. Relaunching in headful mode...")
            await self.close()
            await self._ensure_browser(force_headful=True)
            return await retry_callback()

        solved = await self._wait_for_manual_security_clear(timeout=90)
        if not solved:
            raise RuntimeError("CNKI security verification was not solved within 90 seconds.")
        return None

    async def _apply_exact_year_filter(self, year: int) -> None:
        """Use CNKI's sidebar year facet when the caller asks for one exact year."""
        try:
            await self.page.evaluate(
                """() => {
                    const yearGroup = document.querySelector('dl[groupid="YE"]');
                    if (yearGroup && yearGroup.className.includes('off')) {
                        yearGroup.querySelector('dt.tit')?.click();
                    }
                }"""
            )
            await self.page.wait_for_selector(f'dl[groupid="YE"] input[value="{year}"]', timeout=15000)
            await self.page.evaluate(
                """(yearValue) => {
                    const input = document.querySelector(`dl[groupid="YE"] input[value="${yearValue}"]`);
                    if (input && !input.checked) input.click();
                }""",
                str(year),
            )
            await self.page.wait_for_selector("table.result-table-list tbody tr", timeout=15000)
        except Exception as e:
            self._log(f"CNKI exact year facet failed for {year}, falling back to local filtering: {e}")

    @staticmethod
    def _extract_year(date_text: str) -> Optional[int]:
        if not date_text:
            return None
        match = re.search(r"(?:19|20)\d{2}", str(date_text))
        return int(match.group(0)) if match else None

    @staticmethod
    def _clean_result_text(text: str, *, compact: bool = False) -> str:
        text = str(text or "")
        had_invisible_marks = bool(re.search(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", text))
        text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", "", text)
        if had_invisible_marks:
            text = text.replace("版权", "").replace("知网", "")
        text = re.sub(r"\s+", "" if compact else " ", text)
        return text.strip()

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

    @staticmethod
    def _safe_filename(name: str, default_name: str = "cnki_paper") -> str:
        name = (name or default_name).strip()
        name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
        name = re.sub(r"\s+", " ", name).strip(" .")
        return name[:160] or default_name

    @staticmethod
    def _filename_from_content_disposition(content_disposition: str) -> Optional[str]:
        if not content_disposition:
            return None

        match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.I)
        if match:
            return urllib.parse.unquote(match.group(1))

        match = re.search(r'filename="?([^";]+)"?', content_disposition, flags=re.I)
        if match:
            return urllib.parse.unquote(match.group(1))
        return None

    async def _build_default_pdf_name(self) -> str:
        title = "cnki_paper"
        try:
            title = (await self.page.title()).replace(" - 中国知网", "").strip() or title
        except Exception:
            pass
        safe_title = self._safe_filename(title)
        return safe_title if safe_title.lower().endswith(".pdf") else f"{safe_title}.pdf"

    async def _download_via_request(self, download_url: str, output_dir: str, default_filename: str) -> Optional[str]:
        try:
            response = await self.context.request.get(
                download_url,
                headers={"Referer": self.page.url},
                timeout=60000,
            )
            body = await response.body()
        except Exception as e:
            self._log(f"CNKI direct request download failed, will try browser click fallback: {e}")
            return None

        headers = response.headers
        content_type = headers.get("content-type", "").lower()
        content_disposition = headers.get("content-disposition", "")
        final_url = getattr(response, "url", "") or ""

        if body.startswith(b"%PDF") or "application/pdf" in content_type:
            filename = self._filename_from_content_disposition(content_disposition) or default_filename
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            file_path = os.path.join(output_dir, self._safe_filename(filename))
            with open(file_path, "wb") as f:
                f.write(body)
            return file_path

        text = body[:6000].decode("utf-8", errors="ignore")
        if "login.cnki.net" in final_url or "中国知网-登录" in text or "账号登录" in text:
            self._log("CNKI direct request reached a login page, trying browser click fallback.")
            return None
        if "来源应用不正确" in text:
            self._log("CNKI rejected direct request as an invalid source application, trying browser click fallback.")
            return None
        if "安全验证" in text or "captcha" in final_url.lower():
            return "Error: CNKI security verification is required before PDF download."
        if (
            "fee_" in final_url.lower()
            or "settlement" in final_url.lower()
            or "加入购物车" in text
            or "立即支付" in text
            or "在线支付" in text
        ):
            return "Error: CNKI PDF download reached a payment/order page instead of a PDF response."

        return None

    async def _save_download(self, download, output_dir: str, default_filename: str) -> str:
        filename = download.suggested_filename or default_filename
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        file_path = os.path.join(output_dir, self._safe_filename(filename))
        await download.save_as(file_path)
        return file_path

    @staticmethod
    def _is_valid_pdf(path: str) -> bool:
        try:
            with open(path, "rb") as f:
                return f.read(4) == b"%PDF"
        except Exception:
            return False

    @staticmethod
    def _looks_like_security_or_login(final_url: str, html: str) -> bool:
        markers = [
            "/verify/home",
            "captchaType=blockPuzzle",
            "安全验证",
            "向右滑动完成验证",
            "拖动下方滑块完成拼图",
            "中国知网-登录",
            "账号登录",
        ]
        haystack = f"{final_url}\n{html[:6000]}"
        return any(marker in haystack for marker in markers)

    @staticmethod
    def _parse_total_count(soup: bs4.BeautifulSoup) -> str:
        selectors = [
            "#countPageDiv",
            ".pagerTitleCell",
            ".result-con-r",
            ".page-record",
            ".search-count",
            ".res-count",
        ]
        for selector in selectors:
            elem = soup.select_one(selector)
            if not elem:
                continue
            match = re.search(r"([\d,]+)\s*(?:条|篇)", elem.get_text(" ", strip=True))
            if match:
                return match.group(1).replace(",", "")

        text = soup.get_text(" ", strip=True)
        for pattern in (r"找到\s*([\d,]+)\s*条", r"共\s*([\d,]+)\s*条", r"([\d,]+)\s*条"):
            match = re.search(pattern, text)
            if match:
                return match.group(1).replace(",", "")
        return "未知"

    @staticmethod
    def _extract_turnpage(html: str) -> str:
        soup = bs4.BeautifulSoup(html or "", "html.parser")
        elem = soup.select_one("#hidTurnPage")
        if elem and elem.get("value"):
            return elem["value"]
        match = re.search(r'id=["\']hidTurnPage["\'][^>]*value=["\']([^"\']+)', html or "", flags=re.I)
        return match.group(1) if match else ""

    @classmethod
    def _source_type_allowed(cls, source_type: str, db_type: str, source: str) -> bool:
        normalized = cls._normalize_cnki_option(source_type)
        if normalized in ("", "all"):
            return True
        aliases = {
            "学术期刊": ["学术期刊", "期刊", "CJFQ"],
            "学位论文": ["学位论文", "硕士", "博士", "CDMD"],
            "会议": ["会议", "CIPD"],
            "报纸": ["报纸", "CCND"],
            "图书": ["图书"],
            "标准": ["标准"],
            "专利": ["专利"],
        }
        tokens = aliases.get(normalized, [normalized])
        haystack = f"{db_type} {source}".lower()
        return any(str(token).lower() in haystack for token in tokens)

    def _parse_search_rows(self, html: str) -> List[Dict[str, str]]:
        soup = bs4.BeautifulSoup(html, "html.parser")
        rows = []
        for row in soup.select("table.result-table-list tbody tr"):
            title_elem = row.select_one("td.name a")
            if not title_elem:
                continue
            title = self._clean_result_text(title_elem.text or "N/A")
            detail_link = title_elem.get("href") or ""
            detail_link = urllib.parse.urljoin("https://kns.cnki.net", detail_link)

            author_elem = row.select_one("td.author")
            author = self._clean_result_text(author_elem.text if author_elem else "N/A", compact=True)

            source_elem = row.select_one("td.source")
            source = self._clean_result_text(source_elem.text if source_elem else "N/A", compact=True)
            venue_name = source if source != "N/A" else ""

            date_elem = row.select_one("td.date")
            date = self._clean_result_text(date_elem.text if date_elem else "N/A")

            data_elem = row.select_one("td.data")
            db_type = self._clean_result_text(data_elem.text if data_elem else "N/A", compact=True)

            uid = hashlib.md5(detail_link.encode()).hexdigest()[:8]
            rows.append(
                {
                    "id": uid,
                    "title": title,
                    "author": author,
                    "source": source,
                    "venue_name": venue_name,
                    "date": date,
                    "db_type": db_type,
                    "detail_link": detail_link,
                }
            )
        return rows

    async def _search_papers_direct(
        self,
        query: str,
        search_field: str = "主题",
        db_scope: str = "总库",
        source_type: str = "all",
        journal: str = None,
        start_year: int = None,
        end_year: int = None,
        sort_by: str = "relevance",
        start_index: int = 0,
        limit: int = 10,
    ) -> Dict:
        if not self._cookie_header_for_host("kns.cnki.net"):
            raise RuntimeError("CNKI storage-state cookies are not available.")
        if not self._is_total_db_scope(db_scope):
            raise RuntimeError("CNKI direct request does not yet map non-total db_scope safely.")

        field_spec = self._field_spec(search_field)
        if not field_spec:
            raise RuntimeError(f"CNKI direct request does not support search_field={search_field!r}.")
        field_code, field_title = field_spec

        limit = max(int(limit or 10), 0)
        start_index = max(int(start_index or 0), 0)
        if limit == 0:
            return {"total_results": "0", "papers": []}

        timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
            starter_url = self._direct_search_url(query, field_code)
            status, starter_final, _, body, _ = await self._direct_request(session, "GET", starter_url)
            starter_html = body.decode("utf-8", errors="ignore")
            if status >= 400 or self._looks_like_security_or_login(starter_final, starter_html):
                raise RuntimeError("CNKI direct starter request reached security verification or login.")

            date_range = self._date_range_filter(start_year, end_year)
            local_year_filter_active = (start_year is not None or end_year is not None) and not date_range
            page_num = 1 if local_year_filter_active else (start_index // 20) + 1
            offset_in_first_page = 0 if local_year_filter_active else start_index % 20
            max_pages_to_scan = max(50, ((start_index + limit) // 20) + 10) if local_year_filter_active else max(2, (limit // 20) + 3)

            results = []
            total_count = "未知"
            matched_for_offset = 0
            turnpage = None

            if date_range or page_num > 1:
                seed_payload = self._build_direct_search_payload(query, field_code, field_title, 1)
                status, final_url, _, body, _ = await self._direct_request(
                    session,
                    "POST",
                    "https://kns.cnki.net/kns8s/brief/grid",
                    referer=starter_final,
                    data=seed_payload,
                    ajax=True,
                )
                seed_html = body.decode("utf-8", errors="ignore")
                if status >= 400 or self._looks_like_security_or_login(final_url, seed_html):
                    raise RuntimeError("CNKI direct seed grid request reached security verification or login.")
                turnpage = self._extract_turnpage(seed_html)
                if not turnpage:
                    raise RuntimeError("CNKI direct seed grid did not expose hidTurnPage for filtered/paged search.")

            from scraper_utils import venue_matches

            for scanned in range(max_pages_to_scan):
                is_page_turn = page_num > 1 or scanned > 0
                request_search_from = 4 if is_page_turn else (2 if date_range else 1)
                request_bool_search = not is_page_turn
                payload = self._build_direct_search_payload(
                    query,
                    field_code,
                    field_title,
                    page_num,
                    date_range=date_range,
                    turnpage=turnpage,
                    search_from=request_search_from,
                    bool_search=request_bool_search,
                    sort_field="PT" if is_page_turn else "",
                    sort_type="desc" if is_page_turn else "",
                )
                status, final_url, _, body, _ = await self._direct_request(
                    session,
                    "POST",
                    "https://kns.cnki.net/kns8s/brief/grid",
                    referer=starter_final,
                    data=payload,
                    ajax=True,
                )
                html = body.decode("utf-8", errors="ignore")
                if status >= 400 or self._looks_like_security_or_login(final_url, html):
                    raise RuntimeError("CNKI direct grid request reached security verification or login.")
                if not turnpage:
                    turnpage = self._extract_turnpage(html) or turnpage

                soup = bs4.BeautifulSoup(html, "html.parser")
                if scanned == 0:
                    total_count = self._parse_total_count(soup)

                rows = self._parse_search_rows(html)
                if not rows:
                    break

                start_at = offset_in_first_page if scanned == 0 and not local_year_filter_active else 0
                for item in rows[start_at:]:
                    paper_year = self._extract_year(item.get("date", ""))
                    if not self._year_allowed(paper_year, start_year=start_year, end_year=end_year):
                        continue
                    if local_year_filter_active and matched_for_offset < start_index:
                        matched_for_offset += 1
                        continue
                    if journal and item.get("venue_name") and not venue_matches(journal, item["venue_name"]):
                        continue
                    if not self._source_type_allowed(source_type, item.get("db_type", ""), item.get("source", "")):
                        continue

                    results.append(item)
                    if local_year_filter_active:
                        matched_for_offset += 1
                    if len(results) >= limit:
                        return {"total_results": total_count, "papers": results}

                page_num += 1

            return {"total_results": total_count, "papers": results}

    async def _get_paper_details_direct(self, detail_url: str) -> Dict[str, str]:
        if not self._cookie_header_for_host("kns.cnki.net"):
            raise RuntimeError("CNKI storage-state cookies are not available.")

        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=25)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
            status, final_url, _, body, _ = await self._direct_request(
                session,
                "GET",
                detail_url,
                referer="https://kns.cnki.net/kns8s/defaultresult/index",
            )
        html = body.decode("utf-8", errors="ignore")
        if status >= 400 or self._looks_like_security_or_login(final_url, html):
            raise RuntimeError("CNKI direct detail request reached security verification or login.")

        soup = bs4.BeautifulSoup(html, "html.parser")
        abstract_elem = soup.select_one("span#ChDivSummary")
        abstract = abstract_elem.text.strip() if abstract_elem else "No abstract found."

        keywords_elems = soup.select("p.keywords a")
        keywords = [k.text.strip().strip(";") for k in keywords_elems] if keywords_elems else []

        doi = ""
        for p in soup.select("ul.docinfo li, div.docinfo p"):
            text = p.get_text(" ", strip=True)
            match = re.search(r"DOI\s*[:：]\s*(.+)", text, flags=re.I)
            if match:
                doi = match.group(1).strip()
                break

        return {
            "url": detail_url,
            "abstract": abstract,
            "keywords": keywords,
            "doi": doi,
        }

    async def _download_paper_direct(self, detail_url: str, output_dir: str) -> Optional[str]:
        if not self._cookie_header_for_host("kns.cnki.net"):
            return None
        await self._refresh_cnki_institution_login_direct()

        timeout = aiohttp.ClientTimeout(total=90, connect=15, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
            status, final_url, _, body, _ = await self._direct_request(
                session,
                "GET",
                detail_url,
                referer="https://kns.cnki.net/kns8s/defaultresult/index",
            )
            html = body.decode("utf-8", errors="ignore")
            if status >= 400 or self._looks_like_security_or_login(final_url, html):
                return None

            soup = bs4.BeautifulSoup(html, "html.parser")
            title = "cnki_paper"
            title_elem = soup.select_one("h1, .wx-tit h1")
            if title_elem:
                title = title_elem.get_text(" ", strip=True)
            elif soup.title:
                title = soup.title.get_text(" ", strip=True).replace(" - 中国知网", "").strip() or title
            default_filename = self._safe_filename(title)
            if not default_filename.lower().endswith(".pdf"):
                default_filename += ".pdf"

            pdf_elem = soup.select_one("a#pdfDown")
            if not pdf_elem or not pdf_elem.get("href"):
                return None
            pdf_url = urllib.parse.urljoin(final_url, pdf_elem["href"])

            status, pdf_final_url, headers, pdf_body, _ = await self._direct_request(
                session,
                "GET",
                pdf_url,
                referer=final_url,
                max_redirects=12,
            )

        content_type = headers.get("Content-Type", "").lower()
        if not (pdf_body.startswith(b"%PDF") or "application/pdf" in content_type):
            text = pdf_body[:4000].decode("utf-8", errors="ignore")
            if "login.cnki.net" in pdf_final_url or "中国知网-登录" in text or "账号登录" in text:
                self._log("CNKI direct aiohttp PDF request reached login; falling back to browser profile.")
            elif "安全验证" in text or "captcha" in pdf_final_url.lower():
                self._log("CNKI direct aiohttp PDF request reached security verification; falling back to browser profile.")
            return None

        filename = self._filename_from_content_disposition(headers.get("Content-Disposition", "")) or default_filename
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        file_path = os.path.join(output_dir, self._safe_filename(filename))
        with open(file_path, "wb") as f:
            f.write(pdf_body)
        return file_path

    async def search_papers(self, query: str, search_field: str = "主题", db_scope: str = "总库", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        """
        search_papers with pagination, precise filtering, and search field support.
        """
        try:
            return await self._search_papers_direct(
                query=query,
                search_field=search_field,
                db_scope=db_scope,
                source_type=source_type,
                journal=journal,
                start_year=start_year,
                end_year=end_year,
                sort_by=sort_by,
                start_index=start_index,
                limit=limit,
            )
        except Exception as e:
            self._log(f"CNKI direct search failed, falling back to browser flow: {e}")

        if not self.page:
            await self._ensure_browser()

        async def retry_current_search():
            return await self.search_papers(
                query=query,
                search_field=search_field,
                db_scope=db_scope,
                source_type=source_type,
                journal=journal,
                start_year=start_year,
                end_year=end_year,
                sort_by=sort_by,
                start_index=start_index,
                limit=limit,
            )
             
        await self._goto("https://www.cnki.net/")
        try:
            recovered = await self._recover_from_security_if_needed(retry_current_search)
        except RuntimeError as e:
            return {"total_results": "0", "papers": [], "error": str(e)}
        if recovered is not None:
            return recovered
         
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
                self._log(f"Error selecting search field: {e}")
         
        # Fill search input
        search_box = self.page.locator("textarea.search-input, #txt_SearchText, input.search-input, #txt_search, textarea[name='txt_SearchText']")
        if await search_box.count() > 0:
            await search_box.first.fill(query)
            search_btn = self.page.locator(
                "div.search-btn:has-text('检索'), input.search-btn, button:has-text('检索'), a:has-text('检索')"
            ).filter(visible=True).first
            await search_btn.click()
        else:
            search_box = self.page.get_by_role("textbox", name="中文文献、外文文献")
            if await search_box.count() > 0:
                await search_box.fill(query)
                search_btn = self.page.get_by_text("检索", exact=True)
                await search_btn.click()

        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        try:
            recovered = await self._recover_from_security_if_needed(retry_current_search)
        except RuntimeError as e:
            return {"total_results": "0", "papers": [], "error": str(e)}
        if recovered is not None:
            return recovered
             
        # Wait for results page
        try:
            await self.page.wait_for_selector("table.result-table-list tbody tr", timeout=15000)
        except Exception:
            return {"total_results": "0", "papers": []}

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

        if start_year is not None and end_year is not None and start_year == end_year:
            await self._apply_exact_year_filter(start_year)

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
            self._log("Extract count error:", e)
        
        rows = soup.select("table.result-table-list tbody tr")
         
        results = []
        collected = 0
        matched_for_offset = 0
        year_filter_active = start_year is not None or end_year is not None

        target_start_page = 1 if year_filter_active else (start_index // 20) + 1
        offset_in_first_page = 0 if year_filter_active else start_index % 20

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
                self._log(f"Pagination error jumping to page {target_start_page}: {e}")
         
        # Now collect items spanning across pages as necessary
        pages_scanned = 0
        max_pages_to_scan = max(50, ((start_index + limit) // 20) + 10) if year_filter_active else max(2, (limit // 20) + 3)
        while collected < limit and pages_scanned < max_pages_to_scan:
            pages_scanned += 1
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
                title = self._clean_result_text(title_elem.text if title_elem else "N/A")
                detail_link = title_elem.get("href") if title_elem else ""
                if detail_link and detail_link.startswith("/"):
                    detail_link = "https://kns.cnki.net" + detail_link
                    
                author_elem = row.select_one("td.author")
                author = self._clean_result_text(author_elem.text if author_elem else "N/A", compact=True)

                source_elem = row.select_one("td.source")
                source = self._clean_result_text(source_elem.text if source_elem else "N/A", compact=True)
                # On CNKI's result table, td.source already holds the journal / publication
                # name in clear text, so it doubles as venue_name. We expose it under both keys
                # for cross-platform consistency.
                venue_name = source if source != "N/A" else ""

                date_elem = row.select_one("td.date")
                date = self._clean_result_text(date_elem.text if date_elem else "N/A")

                paper_year = self._extract_year(date)
                if not self._year_allowed(paper_year, start_year=start_year, end_year=end_year):
                    continue

                if year_filter_active and matched_for_offset < start_index:
                    matched_for_offset += 1
                    continue

                # journal filter: CNKI's UI facet for 文献来源 is hidden behind a JS-only panel
                # that is fragile to drive; doing a client-side substring match against the
                # already-extracted source column is both reliable and order-preserving.
                from scraper_utils import venue_matches
                if journal and venue_name and not venue_matches(journal, venue_name):
                    continue

                data_elem = row.select_one("td.data")
                db_type = data_elem.text.strip() if data_elem else "N/A"

                uid = hashlib.md5(detail_link.encode()).hexdigest()[:8]

                results.append({
                    "id": uid,
                    "title": title,
                    "author": author,
                    "source": source,
                    "venue_name": venue_name,
                    "date": date,
                    "db_type": db_type,
                    "detail_link": detail_link,
                })
                collected += 1
                if year_filter_active:
                    matched_for_offset += 1
                 
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
        try:
            return await self._get_paper_details_direct(detail_url)
        except Exception as e:
            self._log(f"CNKI direct detail failed, falling back to browser flow: {e}")

        if not self.page:
            await self.initialize()

        from scraper_utils import goto_with_retry
        await goto_with_retry(self.page, detail_url, wait_until="domcontentloaded")
        
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
        os.makedirs(output_dir, exist_ok=True)
        try:
            direct_pdf = await self._download_paper_direct(detail_url, output_dir)
            if direct_pdf:
                return direct_pdf
        except Exception as e:
            self._log(f"CNKI direct PDF download failed, falling back to browser flow: {e}")

        if not self.page:
            await self._ensure_browser()
             
        await self._goto(detail_url)
        await asyncio.sleep(2)

        if await self._is_security_page():
            if not self.is_headful:
                await self.close()
                await self._ensure_browser(force_headful=True)
                await self._goto(detail_url)
            if not await self._wait_for_manual_security_clear(timeout=90):
                return "Error: CNKI security verification was not solved within 90 seconds."
         
        try:
            default_filename = await self._build_default_pdf_name()
            try:
                await self.page.wait_for_function(
                    """() => Array.from(document.querySelectorAll('#pdfDown, a'))
                        .some(a => (a.innerText || '').includes('PDF下载') && a.href && !a.href.startsWith('javascript'))""",
                    timeout=10000,
                )
            except Exception:
                pass

            pdf_btn = self.page.locator("a#pdfDown, a:has-text('PDF下载')").filter(visible=True).first
            if await pdf_btn.count() == 0:
                pdf_btn = self.page.locator("a#DownLoadParts, a:has-text('整本下载')").filter(visible=True).first
                 
            if await pdf_btn.count() > 0:
                href = await pdf_btn.get_attribute("href")
                download_url = None
                if href:
                    download_url = urllib.parse.urljoin(self.page.url, href)
                    direct_result = await self._download_via_request(download_url, output_dir, default_filename)
                    if direct_result:
                        return direct_result

                download_wait_seconds = 5
                download_task = asyncio.create_task(self.page.wait_for_event("download", timeout=download_wait_seconds * 1000))
                popup_task = asyncio.create_task(self.context.wait_for_event("page", timeout=download_wait_seconds * 1000))

                await pdf_btn.click()

                file_path = None
                popup = None
                for _ in range(download_wait_seconds):
                    if download_task and download_task.done():
                        try:
                            download = download_task.result()
                            file_path = await self._save_download(download, output_dir, default_filename)
                            break
                        except Exception as e:
                            self._log(f"CNKI main-page download event failed, waiting for popup fallback: {e}")
                            download_task = None

                    if popup_task and popup_task.done() and popup is None:
                        try:
                            popup = popup_task.result()
                        except Exception as e:
                            self._log(f"CNKI popup did not appear, waiting for main download fallback: {e}")
                            popup_task = None
                            popup = None
                            await asyncio.sleep(1)
                            continue
                        try:
                            await popup.wait_for_load_state("domcontentloaded", timeout=15000)
                        except Exception:
                            pass

                        if await self._is_security_page(popup):
                            return "Error: CNKI security verification is required before PDF download."

                        try:
                            popup_text = await popup.locator("body").inner_text(timeout=5000)
                        except Exception:
                            popup_text = ""
                        if "账号登录" in popup_text or "中国知网-登录" in await popup.title():
                            return "Error: CNKI PDF download requires login or institutional access. Please log in once in the CNKI browser profile, then retry."

                        try:
                            download = await popup.wait_for_event("download", timeout=20000)
                            file_path = await self._save_download(download, output_dir, default_filename)
                            break
                        except Exception:
                            pass

                    await asyncio.sleep(1)

                for task in [download_task, popup_task]:
                    if not task:
                        continue
                    if task.done():
                        try:
                            task.result()
                        except Exception:
                            pass
                    else:
                        task.cancel()

                if not file_path:
                    if download_url:
                        direct_result = await self._download_via_request(download_url, output_dir, default_filename)
                        if direct_result:
                            return direct_result
                    return f"Error downloading CNKI PDF: no download event or PDF response was produced within {download_wait_seconds} seconds."

                if not self._is_valid_pdf(file_path):
                    return f"Error: downloaded CNKI payload is not a valid PDF: {file_path}"
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
