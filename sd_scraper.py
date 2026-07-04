import asyncio
import base64
import functools
import json
import sys
from bs4 import BeautifulSoup
import os
import hashlib
import pymupdf4llm
from typing import Dict, List
import urllib.parse
import re
import aiohttp
from mcp_logging import safe_stderr_print
from runtime_config import allow_headful_fallback_for, ensure_runtime_environment, manual_verification_timeout_seconds, profile_path
from scraper_utils import remember_downloaded_pdf, reuse_downloaded_pdf

print = safe_stderr_print
ensure_runtime_environment()

class ScienceDirectScraper:
    def __init__(self):
        self.context = None
        self.page = None
        self.is_headful = False
        self.camoufox_cm = None
        self.allow_headful_fallback = allow_headful_fallback_for("SD")
        self.manual_verification_timeout = manual_verification_timeout_seconds()
        self._pdf_cache = {}

    def _force_pdf_download_handler(self, profile_dir: str) -> None:
        """Make Firefox save PDFs instead of opening the internal viewer.

        Persistent Firefox profiles remember MIME handling in handlers.json. If
        application/pdf is set to action=3 (handle internally), Playwright sees
        the PDF navigation but response.body() is often evicted after the viewer
        consumes the stream. action=0 is saveToDisk, which reliably emits the
        download event we already capture.
        """
        try:
            path = os.path.join(profile_dir, "handlers.json")
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
                except Exception:
                    data = {}
            data.setdefault("defaultHandlersVersion", {})
            data.setdefault("mimeTypes", {})
            data.setdefault("schemes", {})
            data["mimeTypes"]["application/pdf"] = {
                "action": 0,
                "extensions": ["pdf"],
            }
            data.setdefault("isDownloadsImprovementsAlreadyMigrated", False)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            print(f"[SD] Could not force PDF download handler: {e}")

    async def _ensure_browser(self, force_headful=False):
        if not self.context:
            print(f"Initializing ScienceDirect Persistent Browser Context (Headless: {not force_headful})...")
            from scraper_utils import pooled_profile, load_or_create_fingerprint
            profile_dir, self._profile_ephemeral = pooled_profile(".sd_profile", "SD")
            self._profile_dir = profile_dir
            _shared_fp = load_or_create_fingerprint("SD")
            self._force_pdf_download_handler(profile_dir)

            # parent.lock / .parentlock are Firefox-style locks left behind by crashed
            # Camoufox sessions; without removing them the next launch exits silently.
            for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
                lfile = os.path.join(profile_dir, lock_name)
                if os.path.exists(lfile):
                    try: os.remove(lfile)
                    except: pass

            from camoufox.async_api import AsyncCamoufox

            # Pin os=windows so the fingerprint hash stays stable across MCP restarts -- Cloudflare's
            # cf_clearance is bound to that hash, so randomizing OS every launch invalidates it and
            # forces a fresh manual challenge each session.
            # Do NOT block_images: SD rides DataDome which flags the missing-image-requests pattern
            # (Camoufox warns about this explicitly).
            # firefox_user_prefs disables the built-in PDF viewer so downloads land as download events.
            self.is_headful = force_headful
            _cam_kw = dict(
                headless=not force_headful,
                user_data_dir=profile_dir,
                persistent_context=True,
                os="windows",
                humanize=True,
                geoip=True,
                accept_downloads=True,
                firefox_user_prefs={
                    "pdfjs.disabled": True,
                    "browser.download.alwaysOpenPanel": False,
                    "browser.download.manager.showWhenStarting": False,
                    "browser.helperApps.neverAsk.saveToDisk": (
                        "application/pdf,application/octet-stream,binary/octet-stream,"
                        "application/force-download"
                    ),
                    "browser.helperApps.alwaysAsk.force": False,
                },
            )
            if _shared_fp is not None:
                _cam_kw["fingerprint"] = _shared_fp
                _cam_kw["i_know_what_im_doing"] = True
            self.camoufox_cm = AsyncCamoufox(**_cam_kw)
            self.context = await self.camoufox_cm.__aenter__()
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

            from scraper_utils import apply_browser_cookies, capture_browser_cookies
            await apply_browser_cookies(self.context, "SD")

            print("Navigating to SD (Resolving protections)...")
            await self.page.goto("https://www.sciencedirect.com")
            
            # Smart wait for DataDome
            cf_blocked = True
            for _ in range(15):
                try:
                    title = await self.page.title()
                    html = await self.page.content()
                    if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "请稍候" not in title and "Cloudflare" not in title and "Just a moment" not in title and "DataDome" not in html:
                        cf_blocked = False
                        break
                except Exception:
                    pass # Navigation in progress
                await asyncio.sleep(1)
                
            if cf_blocked:
                if not force_headful:
                    if not self.allow_headful_fallback:
                        print("[Anti-Bot] SD CAPTCHA blocked in headless mode; headful fallback is disabled.")
                        return
                    print("[Anti-Bot] SD CAPTCHA blocked in headless! Relaunching headful for manual check...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    return
                else:
                    print(f">>> PLEASE WAIT OR SOLVE SD CAPTCHA IN BROWSER WINDOW. Waiting up to {self.manual_verification_timeout}s... <<<")
                    solved = False
                    for _ in range(self.manual_verification_timeout):
                        try:
                            title = await self.page.title()
                            html = await self.page.content()
                            if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "请稍候" not in title and "Cloudflare" not in title and "Just a moment" not in title and "DataDome" not in html:
                                print("[Anti-Bot] SD CAPTCHA passed! Proceeding...")
                                solved = True
                                cf_blocked = False
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
            
            # Try to accept cookies banner if it exists
            try:
                btn = self.page.locator('button#onetrust-accept-btn-handler')
                if await btn.count() > 0:
                    await btn.first.click()
                    print("Accepted SD cookies.")
                    await asyncio.sleep(1)
            except Exception:
                pass
                
            if not cf_blocked:
                await capture_browser_cookies(self.context, "SD")
            print("ScienceDirect context initialized successfully.")

    async def warmup_auth(self, timeout_seconds: int = None) -> Dict:
        """Open ScienceDirect headful, wait for human verification, then persist cookies."""
        if timeout_seconds is not None:
            self.manual_verification_timeout = max(30, int(timeout_seconds))
        self.allow_headful_fallback = True
        if self.context:
            await self.close()
        started = asyncio.get_event_loop().time()
        await self._ensure_browser(force_headful=True)
        title = ""
        html = ""
        try:
            title = await self.page.title()
            html = await self.page.content()
        except Exception:
            pass
        ok = bool(self.page) and not self._is_blocked_html(f"{title}\n{html}")
        try:
            from scraper_utils import capture_browser_cookies
            await capture_browser_cookies(
                self.context,
                "SD",
                note="manual warmup verified" if ok else "manual warmup attempted",
            )
        except Exception:
            pass
        return {
            "platform": "SD",
            "ok": ok,
            "seconds": round(asyncio.get_event_loop().time() - started, 3),
            "title": title,
            "url": self.page.url if self.page else "",
        }

    async def initialize(self):
        await self._ensure_browser()

    async def close(self):
        if getattr(self, "context", None):
            try:
                from scraper_utils import capture_browser_cookies
                await capture_browser_cookies(self.context, "SD")
            except Exception:
                pass
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            try:
                await self.camoufox_cm.__aexit__(None, None, None)
            except Exception as e:
                print(f"ScienceDirect context close failed: {e}")
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None
        if getattr(self, "_profile_dir", None):
            from scraper_utils import cleanup_pooled_profile
            cleanup_pooled_profile(self._profile_dir, getattr(self, "_profile_ephemeral", False))
            self._profile_dir = None

    def _is_blocked_html(self, html: str, status: int = None) -> bool:
        text = (html or "").lower()
        if status in (403, 429):
            return True
        datadome_challenge = "datadome" in text and any(
            marker in text
            for marker in (
                "are you a robot",
                "captcha",
                "verify",
                "please wait",
                "请稍候",
            )
        )
        if datadome_challenge:
            return True
        return any(
            marker in text
            for marker in (
                "are you a robot",
                "captcha-delivery",
                "geo.captcha",
                "just a moment",
                "cf-browser-verification",
                "please wait",
                "请稍候",
                "egy pillanat",
            )
        )

    def _default_headers(self, *, referer: str = None, accept: str = "text/html,application/xhtml+xml") -> dict:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    async def _cookie_header_for_url(self, url: str) -> str:
        pairs = []
        try:
            cookies = await self.context.cookies(url) if self.context else []
        except Exception:
            cookies = []
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value is not None:
                pairs.append(f"{name}={value}")
        if not pairs:
            pairs = [f"{name}={value}" for name, value in self._profile_cookie_dict_for_url(url).items()]
        return "; ".join(pairs)

    def _profile_cookie_dict_for_url(self, url: str) -> dict:
        """Read ScienceDirect cookies from the project-local Firefox profile.

        This lets request-first paths carry verification cookies before we pay
        the cost of launching Camoufox. The database is opened read-only and
        immutable, so this does not mutate the browser profile.
        """
        try:
            import sqlite3
            import time
            from pathlib import Path

            parsed = urllib.parse.urlparse(url)
            req_host = (parsed.hostname or "").lower()
            if not req_host:
                return {}
            db_path = Path(profile_path(".sd_profile")) / "cookies.sqlite"
            if not db_path.exists():
                return {}
            uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
            now = int(time.time())
            out = {}
            con = sqlite3.connect(uri, uri=True)
            try:
                rows = con.execute(
                    """
                    SELECT host, name, value, path, expiry, isSecure
                    FROM moz_cookies
                    WHERE (expiry = 0 OR expiry > ?)
                    """,
                    (now,),
                ).fetchall()
            finally:
                con.close()
            scheme = (parsed.scheme or "https").lower()
            path = parsed.path or "/"
            for host, name, value, cookie_path, _expiry, is_secure in rows:
                if not name or value is None:
                    continue
                cookie_host = str(host or "").lstrip(".").lower()
                if not cookie_host:
                    continue
                if req_host != cookie_host and not req_host.endswith("." + cookie_host):
                    continue
                if is_secure and scheme != "https":
                    continue
                cookie_path = cookie_path or "/"
                if not path.startswith(cookie_path):
                    continue
                out[str(name)] = str(value)
            return out
        except Exception as e:
            print(f"[SD] Could not read profile cookies for direct request: {e}")
            return {}

    async def _cookie_dict_for_url(self, url: str = "") -> dict:
        cookie_dict = {}
        try:
            cookies = await self.context.cookies(url) if self.context else []
        except Exception:
            cookies = []
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value is not None:
                cookie_dict[name] = value
        if not cookie_dict:
            cookie_dict.update(self._profile_cookie_dict_for_url(url))
        return cookie_dict

    async def _direct_get_bytes(self, url: str, *, referer: str = None, accept: str = "*/*") -> bytes:
        headers = self._default_headers(referer=referer, accept=accept)
        cookie_header = await self._cookie_header_for_url(url)
        if cookie_header:
            headers["Cookie"] = cookie_header
        timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.get(url, headers=headers, ssl=False, allow_redirects=True) as resp:
                body = await resp.read()
                content_type = (resp.headers.get("content-type") or "").lower()
                if resp.status >= 400:
                    raise RuntimeError(f"SD direct request failed: HTTP {resp.status}")
                if "text/html" in content_type:
                    text = body.decode(resp.charset or "utf-8", errors="ignore")
                    if self._is_blocked_html(text, resp.status):
                        raise RuntimeError(f"SD direct request returned verification HTML (HTTP {resp.status})")
                return body

    async def _direct_get_text(self, url: str, *, referer: str = None) -> str:
        body = await self._direct_get_bytes(
            url,
            referer=referer,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        text = body.decode("utf-8", errors="ignore")
        if self._is_blocked_html(text):
            raise RuntimeError("SD direct request returned verification HTML")
        return text

    async def _curl_cffi_get_text(
        self,
        url: str,
        *,
        referer: str = None,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    ) -> str:
        try:
            from curl_cffi import requests as curl_requests
        except Exception as e:
            raise RuntimeError(f"curl_cffi is not available: {e}") from e

        ua = ""
        if self.page:
            try:
                ua = await self.page.evaluate("navigator.userAgent")
            except Exception:
                ua = ""
        cookie_dict = await self._cookie_dict_for_url(url)
        headers = self._default_headers(referer=referer, accept=accept)
        if ua:
            headers["User-Agent"] = ua

        last_error = None

        def _get_once(impersonate: str):
            return curl_requests.get(
                url,
                headers=headers,
                cookies=cookie_dict,
                impersonate=impersonate,
                timeout=45,
            )

        for impersonate in ("chrome136", "chrome124", "chrome120"):
            try:
                response = await asyncio.to_thread(_get_once, impersonate)
                text = response.text or ""
                print(
                    f"[SD] curl_cffi fetch ({impersonate}) "
                    f"status={response.status_code} size={len(text)}"
                )
                if self._is_blocked_html(text, response.status_code):
                    last_error = RuntimeError(
                        f"SD curl_cffi request returned verification HTML "
                        f"(HTTP {response.status_code})"
                    )
                    continue
                if response.status_code >= 400:
                    last_error = RuntimeError(f"SD curl_cffi request failed: HTTP {response.status_code}")
                    continue
                return text
            except Exception as e:
                last_error = e
                print(f"[SD] curl_cffi fetch ({impersonate}) failed: {e}")
        raise RuntimeError(str(last_error) if last_error else "SD curl_cffi request failed")

    async def _ensure_sciencedirect_origin(self):
        await self._ensure_browser()
        try:
            current = self.page.url or ""
        except Exception:
            current = ""
        host = urllib.parse.urlparse(current).hostname or ""
        if host.endswith("sciencedirect.com"):
            return
        try:
            await self.page.goto("https://www.sciencedirect.com", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            # The following in-page fetch will produce the more useful failure.
            pass

    async def _page_fetch_text(self, url: str, *, accept: str = "text/html,application/xhtml+xml") -> str:
        await self._ensure_sciencedirect_origin()
        result = await self.page.evaluate(
            """async ({url, accept}) => {
                const response = await fetch(url, {
                    credentials: 'include',
                    cache: 'reload',
                    headers: {'Accept': accept}
                });
                return {
                    ok: response.ok,
                    status: response.status,
                    contentType: response.headers.get('content-type') || '',
                    text: await response.text()
                };
            }""",
            {"url": url, "accept": accept},
        )
        text = result.get("text") or ""
        if self._is_blocked_html(text, result.get("status")):
            raise RuntimeError(f"SD page fetch returned verification HTML (HTTP {result.get('status')})")
        if not result.get("ok"):
            raise RuntimeError(f"SD page fetch failed: HTTP {result.get('status')}")
        return text

    async def _page_fetch_bytes(self, url: str, *, accept: str = "*/*") -> bytes:
        await self._ensure_sciencedirect_origin()
        result = await self.page.evaluate(
            """async ({url, accept}) => {
                const response = await fetch(url, {
                    credentials: 'include',
                    cache: 'reload',
                    headers: {'Accept': accept}
                });
                const blob = await response.blob();
                const bodyBase64 = await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve((reader.result || '').split(',')[1] || '');
                    reader.onerror = reject;
                    reader.readAsDataURL(blob);
                });
                return {
                    ok: response.ok,
                    status: response.status,
                    contentType: response.headers.get('content-type') || '',
                    bodyBase64
                };
            }""",
            {"url": url, "accept": accept},
        )
        body = base64.b64decode(result.get("bodyBase64") or "")
        content_type = (result.get("contentType") or "").lower()
        if "text/html" in content_type:
            text = body.decode("utf-8", errors="ignore")
            if self._is_blocked_html(text, result.get("status")):
                raise RuntimeError(f"SD page fetch returned verification HTML (HTTP {result.get('status')})")
        if not result.get("ok"):
            raise RuntimeError(f"SD page fetch failed: HTTP {result.get('status')}")
        return body

    async def _fetch_html_request_first(self, url: str, *, referer: str = "https://www.sciencedirect.com/") -> str:
        try:
            html = await self._curl_cffi_get_text(url, referer=referer)
            print("[SD] Fetched HTML via curl_cffi direct HTTP.")
            return html
        except Exception as e:
            print(f"[SD] curl_cffi direct HTTP failed ({e}); trying aiohttp.")

        try:
            html = await self._direct_get_text(url, referer=referer)
            print("[SD] Fetched HTML via pure direct HTTP.")
            return html
        except Exception as e:
            print(f"[SD] Pure direct HTTP failed ({e}); trying verified browser fetch.")

        await self._ensure_browser()
        try:
            html = await self._curl_cffi_get_text(url, referer=referer)
            print("[SD] Fetched HTML via curl_cffi verified-cookie HTTP.")
            return html
        except Exception as e:
            print(f"[SD] curl_cffi verified-cookie HTTP failed ({e}); trying aiohttp.")

        try:
            html = await self._direct_get_text(url, referer=referer)
            print("[SD] Fetched HTML via verified-cookie direct HTTP.")
            return html
        except Exception as e:
            print(f"[SD] Verified-cookie direct HTTP failed ({e}); trying page-context fetch.")

        html = await self._page_fetch_text(url)
        print("[SD] Fetched HTML via verified page-context fetch.")
        return html

    def _extract_search_token(self, html: str) -> str:
        match = re.search(r'"searchToken"\s*:\s*"([^"]+)"', html or "")
        return match.group(1) if match else ""

    def _build_search_api_url(self, search_url: str, token: str) -> str:
        parsed = urllib.parse.urlparse(search_url)
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        params["t"] = token
        params["hostname"] = "www.sciencedirect.com"
        return "https://www.sciencedirect.com/search/api?" + urllib.parse.urlencode(params)

    async def _fetch_search_api_json(self, search_url: str) -> dict:
        html = await self._fetch_html_request_first(search_url)
        token = self._extract_search_token(html)
        if not token:
            raise RuntimeError("SD search shell did not contain searchToken")
        api_url = self._build_search_api_url(search_url, token)
        try:
            body = await self._curl_cffi_get_text(
                api_url,
                referer=search_url,
                accept="application/json,*/*",
            )
            print("[SD] Fetched search API JSON via curl_cffi HTTP.")
        except Exception as e:
            print(f"[SD] curl_cffi search API failed ({e}); trying page-context fetch.")
            body = await self._page_fetch_text(api_url, accept="application/json,*/*")
        try:
            data = json.loads(body)
        except Exception as e:
            raise RuntimeError(f"SD search API returned non-JSON payload: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError("SD search API returned unexpected payload")
        return data

    def _build_search_url(
        self,
        query: str,
        search_field: str,
        source_type: str,
        journal: str,
        start_year: int,
        end_year: int,
        sort_by: str,
        start_index: int,
    ) -> str:
        offset = start_index
        field_map = {
            "全部": "qs",
            "主题": "qs",
            "篇名": "title",
            "摘要": "qs",
            "作者": "authors",
        }
        field = field_map.get(search_field, search_field if search_field in ["qs", "title", "authors"] else "qs")
        params = {field: query, "offset": str(offset)}
        if journal:
            params["pub"] = journal
        if sort_by == "date_desc":
            params["sortBy"] = "date"
        if start_year and end_year:
            params["date"] = f"{start_year}-{end_year}"
        elif start_year:
            params["date"] = f"{start_year}-2026"
        elif end_year:
            params["date"] = f"1900-{end_year}"
        if (source_type or "").lower() not in ["all", "全部", ""]:
            params["articleTypes"] = source_type
        return f"https://www.sciencedirect.com/search?{urllib.parse.urlencode(params)}"

    def _strip_inline_html(self, value: str) -> str:
        if not value:
            return ""
        return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)

    def _parse_search_html(self, html: str, *, limit: int, journal: str = None) -> Dict:
        soup = BeautifulSoup(html or "", "html.parser")
        total_str = "未知"
        total_node = soup.select_one(
            ".search-body-results-text, h1.search-body-results-text, "
            "span.search-body-results-text, h1[data-testid='srp-page-title']"
        )
        if total_node:
            numbers = re.findall(r"[\d,]+", total_node.get_text(" ", strip=True))
            valid_nums = [n.replace(",", "") for n in numbers if n.replace(",", "").isdigit()]
            if valid_nums:
                total_str = str(max(int(n) for n in valid_nums))

        papers = []
        from scraper_utils import venue_matches
        for item in soup.select("li.ResultItem"):
            if len(papers) >= limit:
                break
            title_node = item.select_one(".result-list-title-link")
            title = title_node.get_text(" ", strip=True) if title_node else "N/A"
            link = title_node.get("href", "") if title_node else ""
            if link and link.startswith("/"):
                link = urllib.parse.urljoin("https://www.sciencedirect.com", link)

            authors = [a.get_text(" ", strip=True) for a in item.select(".author")]
            author_str = ", ".join([a for a in authors if a]) or "N/A"

            venue_node = item.select_one("span.srctitle-date-fields a.subtype-srctitle-link")
            venue_name = venue_node.get_text(" ", strip=True) if venue_node else ""
            if journal and venue_name and not venue_matches(journal, venue_name):
                continue

            date = "N/A"
            date_nodes = item.select("span.srctitle-date-fields > span")
            if len(date_nodes) > 1:
                date = date_nodes[1].get_text(" ", strip=True)
            elif date_nodes:
                date = date_nodes[-1].get_text(" ", strip=True)

            type_node = item.select_one(".article-type")
            doc_type = type_node.get_text(" ", strip=True) if type_node else "Article"
            uid = hashlib.md5(link.encode()).hexdigest()[:8]
            papers.append({
                "id": uid,
                "title": title.strip(),
                "author": author_str.strip(),
                "source": venue_name or "ScienceDirect",
                "venue_name": venue_name,
                "date": date.strip(),
                "db_type": doc_type.strip(),
                "detail_link": link,
            })
        return {"total_results": total_str, "papers": papers}

    def _parse_search_api_json(self, data: dict, *, limit: int, journal: str = None) -> Dict:
        from scraper_utils import venue_matches

        total_results = str(data.get("resultsFound") or "0")
        papers = []
        for item in data.get("searchResults") or []:
            if len(papers) >= limit:
                break
            title = self._strip_inline_html(item.get("title") or item.get("titleXocs") or "N/A")
            link = item.get("link") or ""
            if link and link.startswith("/"):
                link = urllib.parse.urljoin("https://www.sciencedirect.com", link)
            if not link and item.get("pii"):
                link = f"https://www.sciencedirect.com/science/article/pii/{item.get('pii')}"

            author_names = []
            for author in item.get("authors") or []:
                if isinstance(author, dict) and author.get("name"):
                    author_names.append(author["name"].strip())
                elif isinstance(author, str):
                    author_names.append(author.strip())
            author_str = ", ".join([name for name in author_names if name]) or "N/A"

            venue_name = (item.get("sourceTitle") or "").strip()
            if journal and venue_name and not venue_matches(journal, venue_name):
                continue

            date = (
                item.get("publicationDateDisplay")
                or item.get("availableOnlineDate")
                or item.get("sortDate")
                or "N/A"
            )
            doc_type = item.get("articleTypeDisplayName") or item.get("articleType") or "Article"
            pdf_info = item.get("pdf") or {}
            pdf_url = pdf_info.get("downloadLink") or ""
            if pdf_url and pdf_url.startswith("/"):
                pdf_url = urllib.parse.urljoin("https://www.sciencedirect.com", pdf_url)
            link_key = link or item.get("pii") or item.get("doi") or title
            uid = hashlib.md5(link_key.encode()).hexdigest()[:8]
            papers.append({
                "id": uid,
                "title": title.strip(),
                "author": author_str.strip(),
                "source": venue_name or "ScienceDirect",
                "venue_name": venue_name,
                "date": str(date).strip(),
                "db_type": str(doc_type).strip(),
                "detail_link": link,
                "pdf_url": pdf_url,
                "pdf_filename": (pdf_info.get("filename") or "").strip(),
            })
        return {"total_results": total_results, "papers": papers}

    def _parse_detail_html(self, html: str, detail_url: str) -> Dict[str, str]:
        soup = BeautifulSoup(html or "", "html.parser")
        abstract_div = soup.select_one(".abstract.author, #abstracts, #aep-abstract-id")
        abstract = abstract_div.get_text(" ", strip=True) if abstract_div else "No abstract provided."

        keywords = []
        for tag in soup.select(".keyword, .keywords-section .keyword"):
            kw = tag.get_text(" ", strip=True)
            if kw and kw not in keywords:
                keywords.append(kw)

        doi = ""
        meta_doi = soup.select_one('meta[name="citation_doi"], meta[name="dc.Identifier"]')
        if meta_doi and meta_doi.get("content"):
            doi = meta_doi["content"].replace("doi:", "").strip()
        if not doi:
            doi_match = re.search(r"(?:doi\.org/|doi:)(10\.[^\\s\"<>]+)", html or "", flags=re.I)
            doi = doi_match.group(1).strip() if doi_match else ""

        return {
            "url": detail_url,
            "abstract": abstract.replace("\n", " "),
            "keywords": keywords,
            "doi": doi,
        }

    def _pii_from_detail_url(self, detail_url: str) -> str:
        match = re.search(r"/pii/([A-Za-z0-9]+)", detail_url or "")
        return match.group(1) if match else ""

    def _extract_pdf_url_from_html(self, html: str, detail_url: str) -> str:
        pii = self._pii_from_detail_url(detail_url)
        if not pii:
            return ""
        soup = BeautifulSoup(html or "", "html.parser")
        for link in soup.select("a[href]"):
            href = link.get("href") or ""
            if "/pdfft" in href and pii in href:
                return urllib.parse.urljoin("https://www.sciencedirect.com", href)

        pattern = (
            r"https://www\.sciencedirect\.com/science/article/pii/"
            + re.escape(pii)
            + r"/pdfft[^\"'<\s]*"
        )
        match = re.search(pattern, html or "", flags=re.I)
        if match:
            return match.group(0).replace("&amp;", "&")

        # Some pages do not render the md5-signed link until client JS runs.
        # This canonical /pdfft entry can still redirect to the signed asset;
        # if it returns viewer/challenge HTML the caller falls back to the
        # existing rendered-page flow.
        return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true"

    def _build_pdf_entry_url(self, detail_url: str, pdf_url: str = "") -> str:
        if pdf_url:
            return urllib.parse.urljoin("https://www.sciencedirect.com", pdf_url)
        pii = self._pii_from_detail_url(detail_url)
        if not pii:
            return ""
        return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?pid=1-s2.0-{pii}-main.pdf"

    async def _curl_cffi_pdf_get(self, url: str, referer: str = "", request_headers: dict = None) -> bytes:
        """Fetch a signed SD asset URL with browser-grade TLS impersonation.

        Firefox can return the PDF as a top-level navigation response whose body
        is already consumed before Playwright exposes it ("evicted"). A normal
        APIRequestContext refetch then gets a Cloudflare HTML page. curl_cffi
        with the live browser cookies preserves the post-verification request
        shape closely enough to retrieve the same signed PDF bytes.
        """
        try:
            from curl_cffi import requests as curl_requests
        except Exception as e:
            print(f"[SD] curl_cffi not available for PDF refetch: {e}")
            return b""

        request_headers = request_headers or {}
        ua = (
            request_headers.get("user-agent")
            or request_headers.get("User-Agent")
            or ""
        )
        if not ua and self.page:
            try:
                ua = await self.page.evaluate("navigator.userAgent")
            except Exception:
                ua = ""
        referer = (
            referer
            or request_headers.get("referer")
            or request_headers.get("Referer")
            or "https://www.sciencedirect.com/"
        )
        cookie_dict = await self._cookie_dict_for_url(url)
        headers = {
            "Accept": "application/pdf,*/*",
            "Referer": referer,
        }
        if ua:
            headers["User-Agent"] = ua

        def _get_once(impersonate: str):
            return curl_requests.get(
                url,
                headers=headers,
                cookies=cookie_dict,
                impersonate=impersonate,
                timeout=45,
            )

        for impersonate in ("chrome136", "chrome124", "chrome120"):
            try:
                response = await asyncio.to_thread(_get_once, impersonate)
                body = response.content or b""
                ctype = response.headers.get("content-type", "")
                print(
                    f"[SD] curl_cffi PDF refetch ({impersonate}) "
                    f"status={response.status_code} ct={ctype!r} size={len(body)}"
                )
                if response.status_code == 200 and body.startswith(b"%PDF"):
                    return body
            except Exception as e:
                print(f"[SD] curl_cffi PDF refetch ({impersonate}) failed: {e}")
        return b""

    async def _download_paper_fast_path(self, detail_url: str, output_dir: str) -> str:
        pii = self._pii_from_detail_url(detail_url)
        if not pii:
            return ""
        try:
            pdf_url = self._build_pdf_entry_url(detail_url)
            if not pdf_url:
                return ""
            print(f"[SD] Fast PDF candidate: {pdf_url}")
            body = await self._page_fetch_bytes(pdf_url, accept="application/pdf,*/*")
            if body.startswith(b"%PDF"):
                file_path = os.path.join(output_dir, f"sd_{pii}.pdf")
                with open(file_path, "wb") as f:
                    f.write(body)
                print(f"[SD] Fast PDF fetch succeeded ({len(body)} bytes).")
                return file_path
            head = body[:4096].lower()
            if b"datadome" in head or b"are you a robot" in head or b"just a moment" in head:
                print("[SD] Fast PDF fetch returned verification HTML; falling back.")
            else:
                print(f"[SD] Fast PDF fetch returned non-PDF payload ({len(body)} bytes); falling back.")
        except Exception as e:
            print(f"[SD] Fast PDF path failed ({e}); falling back.")
        return ""

    async def _download_pdf_entry_impl(
        self,
        detail_url: str,
        output_dir: str,
        _pdf_sniff: dict,
        safe_name_for_dl: list,
        pdf_url: str = "",
    ) -> str:
        pii = self._pii_from_detail_url(detail_url)
        if not pii:
            return ""
        safe_name_for_dl[0] = pii
        entry_url = self._build_pdf_entry_url(detail_url, pdf_url)
        if not entry_url:
            return ""

        file_path = os.path.join(output_dir, f"sd_{pii}.pdf")
        print(f"[SD] Direct PDF-entry navigation: {entry_url}")

        try:
            await self.page.goto(entry_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"SD direct PDF-entry goto raised (response may already have fired): {e}")

        for _ in range(40):
            if _pdf_sniff["body"]:
                break
            try:
                landed_url = self.page.url or ""
                if "/science/article/abs/pii/" in landed_url:
                    return (
                        "Error: ScienceDirect redirected the PDF entry to the abstract/preview page "
                        "(likely no PDF entitlement or no full-text PDF available for this item).\n"
                        f"PDF entry URL: {entry_url}\n"
                        f"Landed URL: {landed_url}"
                    )
                title = await self.page.title()
                if "Just a moment" in title or "Cloudflare" in title:
                    _pdf_sniff["turnstile"] = True
                    break
            except Exception:
                pass
            if _pdf_sniff["turnstile"] or _pdf_sniff["datadome_403"]:
                break
            await asyncio.sleep(0.5)
        if _pdf_sniff["body"]:
            if _pdf_sniff.get("saved_path"):
                import shutil
                if os.path.abspath(_pdf_sniff["saved_path"]) != os.path.abspath(file_path):
                    shutil.copyfile(_pdf_sniff["saved_path"], file_path)
                print(f"SD direct PDF-entry download hit -> {file_path}")
            else:
                with open(file_path, "wb") as f:
                    f.write(_pdf_sniff["body"])
                print(f"SD direct PDF-entry sniffer hit ({len(_pdf_sniff['body'])}B)")
            return file_path

        needs_manual = _pdf_sniff["turnstile"] or _pdf_sniff["datadome_403"]
        if needs_manual and not getattr(self, "is_headful", False):
            if not self.allow_headful_fallback:
                return (
                    "Error: ScienceDirect PDF host is gated by Cloudflare Turnstile "
                    "(separate from the detail-page DataDome). Enable "
                    "allow_headful_fallback (with SD in allow_headful_fallback_platforms) "
                    "so a browser window can pop for manual verification.\n"
                    f"PDF entry URL: {entry_url}"
                )
            print("[Anti-Bot] Cloudflare Turnstile detected on SD PDF host. Relaunching headful for manual solve...")
            await self.close()
            await self._ensure_browser(force_headful=True)
            self.is_headful = True
            return await self.download_paper(detail_url, output_dir)

        if getattr(self, "is_headful", False):
            print(">>> Please pass the Cloudflare Turnstile / DataDome challenge in the browser window. Waiting up to 120s... <<<")
            for _ in range(240):
                if _pdf_sniff["body"]:
                    break
                await asyncio.sleep(0.5)
            if _pdf_sniff["body"]:
                if _pdf_sniff.get("saved_path"):
                    import shutil
                    if os.path.abspath(_pdf_sniff["saved_path"]) != os.path.abspath(file_path):
                        shutil.copyfile(_pdf_sniff["saved_path"], file_path)
                    print(f"SD direct PDF-entry download hit after manual solve -> {file_path}")
                else:
                    with open(file_path, "wb") as f:
                        f.write(_pdf_sniff["body"])
                    print(f"SD direct PDF-entry sniffer hit after manual solve ({len(_pdf_sniff['body'])}B)")
                return file_path

        return ""

    async def search_papers(self, query: str, search_field: str = "qs", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        q_url = self._build_search_url(
            query,
            search_field,
            source_type,
            journal,
            start_year,
            end_year,
            sort_by,
            start_index,
        )

        print(f"Fetching SD search HTML: {q_url}")
        try:
            data = await self._fetch_search_api_json(q_url)
            return self._parse_search_api_json(data, limit=limit, journal=journal)
        except Exception as e:
            print(f"[SD] Search API path failed ({e}); trying shell HTML parser.")

        try:
            html = await self._fetch_html_request_first(q_url)
            parsed = self._parse_search_html(html, limit=limit, journal=journal)
            if parsed.get("papers"):
                return parsed
            print("[SD] Shell HTML parser found no result items; falling back to browser-rendered search.")
        except Exception as e:
            print(f"[SD] Shell HTML parser failed ({e}); falling back to browser-rendered search.")

        await self._ensure_browser()
        print(f"Navigating to SD search fallback: {q_url}")
        
        for attempt in range(2):
            await self.page.goto(q_url, wait_until="domcontentloaded")
            
            # Smart wait for DataDome/CF
            captcha_detected = False
            for _ in range(15):
                try:
                    title = await self.page.title()
                    html = await self.page.content()
                    if "Are you a robot" in title or "Are you a robot" in html or "Egy pillanat" in title or "DataDome" in html or "请稍候" in title or "Just a moment" in title:
                        captcha_detected = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
                
            if captcha_detected:
                if attempt == 0:
                    if not self.allow_headful_fallback:
                        return {
                            "total_results": "0",
                            "papers": [],
                            "error": "ScienceDirect anti-bot verification blocked the headless MCP session. Add SD to allow_headful_fallback_platforms in mcp_runtime_config.json for manual browser verification.",
                        }
                    print("[Anti-Bot] CAPTCHA detected in headless mode! Relaunching in headful mode for user intervention...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    continue # Try again with headful browser
                else:
                    print(">>> PLEASE SOLVE THE CAPTCHA IN THE BROWSER WINDOW. Waiting up to 60 seconds... <<<")
                    solved = False
                    for _ in range(60):
                        try:
                            title = await self.page.title()
                            html = await self.page.content()
                            if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "DataDome" not in html and "请稍候" not in title and "Just a moment" not in title:
                                print("[Anti-Bot] CAPTCHA completely solved! Proceeding...")
                                solved = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                    if not solved:
                        print("Captcha was not solved in time.")
            else:
                break # Not blocked!
            
        # Standard Playwright automatic wait and selection
        try:
            await self.page.wait_for_selector("li.ResultItem", timeout=15000)
        except Exception:
            print("No items found. Current title is:", await self.page.title())
            html = await self.page.content()
            if "Are you a robot?" in html:
                print("STILL STUCK IN DATADOME! Manual intervention needed.")
                
        # Check Total Results
        total_str = "未知"
        total_loc = self.page.locator(".search-body-results-text, h1.search-body-results-text, span.search-body-results-text, h1[data-testid='srp-page-title']").first
        if await total_loc.count() > 0:
            txt = await total_loc.inner_text()
            numbers = re.findall(r'[\d,]+', txt)
            if numbers:
                valid_nums = [n.replace(',', '') for n in numbers]
                total_str = str(max(int(n) for n in valid_nums if n.isdigit()))
                
        papers = []
        items = await self.page.locator("li.ResultItem").all()
        
        collected = 0        
        for i, item in enumerate(items):
            if collected >= limit:
                break
                
            title_loc = item.locator(".result-list-title-link")
            title = await title_loc.inner_text() if await title_loc.count() > 0 else "N/A"
            link = await title_loc.get_attribute('href') if await title_loc.count() > 0 else ""
            if link and link.startswith("/"):
                link = "https://www.sciencedirect.com" + link
            
            author_locs = await item.locator(".author").all()
            author_str = ", ".join([await a.inner_text() for a in author_locs]) if author_locs else "N/A"
            
            # Current SD markup nests the venue title several spans deep, so a plain
            # nth-of-type selector matches multiple elements and trips Playwright strict mode.
            # The journal/proceeding link with class subtype-srctitle-link is unique per item.
            venue_loc = item.locator("span.srctitle-date-fields a.subtype-srctitle-link").first
            venue_name = (await venue_loc.inner_text()).strip() if await venue_loc.count() > 0 else ""

            # journal filter post-check: SD's &pub= URL param is fuzzy, drop cross-publication
            # hits when caller wanted a specific journal.
            from scraper_utils import venue_matches
            if journal and venue_name and not venue_matches(journal, venue_name):
                continue

            # Date sits as the second direct child <span> of .srctitle-date-fields (e.g. "March 2025").
            date_loc = item.locator("span.srctitle-date-fields > span").nth(1)
            if await date_loc.count() == 0:
                date_loc = item.locator("span.srctitle-date-fields > span").last
            date = await date_loc.inner_text() if await date_loc.count() > 0 else "N/A"

            doc_type = "Article"
            type_loc = item.locator(".article-type")
            if await type_loc.count() > 0:
                doc_type = await type_loc.inner_text()

            uid = hashlib.md5(link.encode()).hexdigest()[:8]

            papers.append({
                "id": uid,
                "title": title.strip(),
                "author": author_str.strip(),
                "source": venue_name or "ScienceDirect",
                "venue_name": venue_name,
                "date": date.strip(),
                "db_type": doc_type.strip(),
                "detail_link": link,
            })
            collected += 1
            
        return {
            "total_results": total_str,
            "papers": papers
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        try:
            html = await self._fetch_html_request_first(detail_url)
            return self._parse_detail_html(html, detail_url)
        except Exception as e:
            print(f"[SD] Request-first detail failed ({e}); falling back to browser-rendered detail.")

        await self._ensure_browser()
        from scraper_utils import goto_with_retry
        await goto_with_retry(self.page, detail_url, wait_until="domcontentloaded")
        
        for _ in range(15):
            try:
                title = await self.page.title()
                html = await self.page.content()
                if "Are you a robot" not in title and "Are you a robot" not in html and "Egy pillanat" not in title and "请稍候" not in title and "Cloudflare" not in title and "Just a moment" not in title and "DataDome" not in html:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
            
        await asyncio.sleep(2)
        
        html = await self.page.content()
        return self._parse_detail_html(html, detail_url)

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)
        pii_match = re.search(r"/pii/([A-Za-z0-9]+)", detail_url or "")
        pii = pii_match.group(1) if pii_match else ""
        if pii:
            cached = reuse_downloaded_pdf(self._pdf_cache, pii, output_dir, f"sd_{pii}.pdf")
            if cached:
                print(f"[SD] Reused in-process PDF cache for {pii}.")
                return cached

        fast_path = await self._download_paper_fast_path(detail_url, output_dir)
        if fast_path:
            if pii:
                remember_downloaded_pdf(self._pdf_cache, pii, fast_path)
            return fast_path

        await self._ensure_browser()

        # Global response sniffer. See block comment above.
        _pdf_sniff = {"body": None, "url": None, "turnstile": False, "datadome_403": False}
        _sniff_log_path = os.path.join(
            os.path.dirname(__file__), "scratch", f"sd_sniff_{int(__import__('time').time())}.log"
        )
        try:
            os.makedirs(os.path.dirname(_sniff_log_path), exist_ok=True)
            _sniff_log = open(_sniff_log_path, "w", encoding="utf-8")
        except Exception:
            _sniff_log = None

        def _sniff_write(line: str) -> None:
            try:
                if _sniff_log and not getattr(_sniff_log, "closed", True):
                    _sniff_log.write(line)
                    _sniff_log.flush()
            except Exception:
                pass

        _sniff_write(f"# SD sniffer log for download_paper({detail_url})\n")

        async def _sniff_response(response):
            try:
                url = response.url or ""
                ct = (response.headers.get("content-type") or "").lower()
                status = response.status
            except Exception as e:
                _sniff_write(f"[meta-err] {e}\n")
                return
            # Log every response (small log, helps diagnose what Firefox saw).
            _sniff_write(f"{status} ct={ct!r} url={url[:200]}\n")
            # Flag the two known anti-bot signatures so the outer flow can
            # decide to escalate to headful for manual solving.
            url_low_early = url.lower()
            if (
                "challenges.cloudflare.com/turnstile" in url_low_early
                or "cdn-cgi/challenge-platform" in url_low_early
                or "challenges.cloudflare.com/cdn-cgi" in url_low_early
                or (status in (403, 429) and "/pdfft" in url_low_early)
            ):
                _pdf_sniff["turnstile"] = True
            if status == 403 and "sciencedirectassets" in url_low_early and ".pdf" in url_low_early:
                _pdf_sniff["datadome_403"] = True
            if _pdf_sniff["body"] is not None:
                return
            url_low = url.lower()
            hit = (
                status == 200
                and (
                    "application/pdf" in ct
                    or ("sciencedirectassets" in url_low and ".pdf" in url_low)
                    or ("/pdfft" in url_low and status == 200 and ("pdf" in ct or "octet" in ct))
                )
            )
            if not hit:
                return
            body = None
            try:
                body = await response.body()
            except Exception as e:
                _sniff_write(f"  -> body() raised {type(e).__name__}: {e}\n")
                try:
                    req_headers = response.request.headers
                except Exception:
                    req_headers = {}
                try:
                    body = await self._curl_cffi_pdf_get(
                        url,
                        referer=detail_url,
                        request_headers=req_headers,
                    )
                    if body:
                        _sniff_write(f"  -> curl_cffi refetch size={len(body)}B head={body[:4]!r}\n")
                except Exception as e0:
                    _sniff_write(f"  -> curl_cffi refetch raised {type(e0).__name__}: {e0}\n")
                # Body is unreadable because Firefox already consumed it for
                # inline rendering (PDF.js) or a download stream. Refetch the
                # same URL via the browser context's request API -- cookies
                # carry over (including any just-solved Turnstile tokens), and
                # X-Amz signed URLs are valid for 5 minutes, so a second GET
                # within that window should return the same PDF bytes WITHOUT
                # any rendering eviction.
                if body is None or body[:4] != b"%PDF":
                    try:
                        refetch = await self.context.request.get(
                            url,
                            headers={"Referer": "https://www.sciencedirect.com/", "Accept": "application/pdf,*/*"},
                            timeout=30000,
                        )
                        refetch_body = await refetch.body()
                        _sniff_write(f"  -> refetch GET status={refetch.status} ct={refetch.headers.get('content-type','')!r} size={len(refetch_body)}B head={refetch_body[:4]!r}\n")
                        if refetch.status == 200 and refetch_body[:4] == b"%PDF":
                            body = refetch_body
                    except Exception as e2:
                        _sniff_write(f"  -> refetch GET raised {type(e2).__name__}: {e2}\n")
                if body is None or body[:4] != b"%PDF":
                    return
            head = body[:4] if body else b""
            _sniff_write(f"  -> body {len(body)}B head={head!r}\n")
            if body and head == b"%PDF":
                _pdf_sniff["body"] = body
                _pdf_sniff["url"] = url
                print(f"[SD][sniffer] captured {len(body)}B from {url[:80]}...")

        # Hook the sniffer on the main page AND on any new page that opens
        # mid-flow (Firefox sometimes opens the PDF.js viewer in a separate
        # tab; we need to observe responses from that tab too).
        self.page.on("response", _sniff_response)

        # Download hook: when Firefox treats the PDF as a stream-to-disk
        # download (after Turnstile is solved we observed status=200
        # application/pdf with body() raising "evicted" -- the body went
        # straight to a download), we capture the file via download.save_as.
        # Far more reliable than chasing response.body() for large PDFs.
        async def _on_download(download):
            try:
                sug = download.suggested_filename or ""
                target = os.path.join(output_dir, f"sd_{safe_name_for_dl[0]}.pdf") if safe_name_for_dl[0] else os.path.join(output_dir, sug or "sd_download.pdf")
                await download.save_as(target)
                _sniff_write(f"[download] saved as {target} (suggested={sug!r})\n")
                with open(target, "rb") as _f:
                    head = _f.read(4)
                if head == b"%PDF":
                    _pdf_sniff["body"] = b"<saved-via-download>"  # sentinel: file already on disk
                    _pdf_sniff["url"] = download.url
                    _pdf_sniff["saved_path"] = target
                    print(f"[SD][download] saved {os.path.getsize(target)}B -> {target}")
                else:
                    _sniff_write(f"[download] head={head!r} not %PDF, ignoring\n")
            except Exception as e:
                _sniff_write(f"[download] save_as raised: {e}\n")
        # The PII -> safe_name extraction happens inside _download_paper_impl;
        # we expose it to the closure via a list mutation. Default None for now.
        safe_name_for_dl = [None]
        self.page.on("download", _on_download)

        def _hook_new_page(new_p):
            try:
                new_p.on("response", _sniff_response)
                new_p.on("download", _on_download)
            except Exception:
                pass
        try:
            self.context.on("page", _hook_new_page)
        except Exception:
            pass

        try:
            result = await self._download_pdf_entry_impl(detail_url, output_dir, _pdf_sniff, safe_name_for_dl)
            if not result:
                result = await self._download_paper_impl(detail_url, output_dir, _pdf_sniff, safe_name_for_dl)
            _sniff_write(f"# result: {result[:200]}\n")
            # On failure, surface the log path so we can analyze.
            if isinstance(result, str) and result.startswith("Error: SD PDF download failed"):
                return result + f"\nSniffer log: {_sniff_log_path}"
            if pii and isinstance(result, str):
                remember_downloaded_pdf(self._pdf_cache, pii, result)
            return result
        finally:
            try:
                self.page.remove_listener("response", _sniff_response)
            except Exception:
                pass
            try:
                self.page.remove_listener("download", _on_download)
            except Exception:
                pass
            try:
                self.context.remove_listener("page", _hook_new_page)
            except Exception:
                pass
            try:
                if _sniff_log and not getattr(_sniff_log, "closed", True):
                    _sniff_log.close()
            except Exception:
                pass

    async def _download_paper_impl(self, detail_url: str, output_dir: str, _pdf_sniff: dict, safe_name_for_dl: list) -> str:
        # To get the valid PDF URL with the encrypted md5 signature, we MUST first go to the page and find the View PDF button.
        await self.page.goto(detail_url, wait_until="domcontentloaded")
        
        import re
        pii_match = re.search(r'/pii/([A-Z0-9]+)', detail_url)
        safe_name = pii_match.group(1) if pii_match else "unknown_pii"
        safe_name_for_dl[0] = safe_name  # expose to the download-event closure

        page_title = await self.page.title()
        print(f"Page Title: {page_title}")
        
        try:
            # Wait for client-side React/Vue components to render the download button
            try:
                await self.page.wait_for_selector(f'a[href*="/science/article/pii/{safe_name}/pdfft"]', timeout=15000)
            except Exception as e:
                print(f"Selector wait timeout, button might be delayed or unavailable: {e}")
                
            import re
            pii_match = re.search(r'/pii/([A-Z0-9]+)', detail_url)
            safe_name = pii_match.group(1) if pii_match else "unknown_pii"
            
            # Check for DataDome or wait for the PDF link to appear
            try:
                await self.page.wait_for_selector(f'a[href*="/pdfft"]', timeout=20000)
            except Exception:
                # Might be DataDome on the detail page!
                html = await self.page.content()
                title = await self.page.title()
                if "Are you a robot" in title or "Are you a robot" in html or "DataDome" in html or "请稍候" in title or "Just a moment" in title:
                    if not getattr(self, 'is_headful', False):
                        if not self.allow_headful_fallback:
                            return "Error: ScienceDirect verification blocked the detail page in headless mode. Add SD to allow_headful_fallback_platforms for manual verification."
                        print("[Anti-Bot] CAPTCHA detected on detail page! Relaunching in headful mode...")
                        await self.close()
                        await self._ensure_browser(force_headful=True)
                        self.is_headful = True
                        return await self.download_paper(detail_url, output_dir)
                    else:
                        print(">>> PLEASE SOLVE DATADOME CAPTCHA... Waiting up to 60 seconds... <<<")
                        for _ in range(60):
                            html = await self.page.content()
                            title = await self.page.title()
                            if "Are you a robot" not in title and "Are you a robot" not in html and "DataDome" not in html and "请稍候" not in title and "Just a moment" not in title:
                                break
                            await asyncio.sleep(1)
                        await self.page.wait_for_selector(f'a[href*="/pdfft"]', timeout=15000)

            # Dynamically extract the PDF link matching the current PII, bypassing text quirks like &nbsp;
            js_code_extract = """
            (targetPii) => {
                let btn = Array.from(document.querySelectorAll('a')).find(el => 
                    el.href && el.href.includes('/science/article/pii/' + targetPii + '/pdfft')
                );
                return btn ? btn.href : null;
            }
            """
            pdf_url = await self.page.evaluate(js_code_extract, safe_name)
            
            if not pdf_url:
                html = await self.page.content()
                # An aggressive heuristic that previously tried to classify
                # "paywall vs scraper breakage" produced false positives (it
                # treated standard SD sidebar CTAs like "Purchase PDF" as proof
                # of paywall even on articles whose PDF was actually
                # accessible). The honest behavior: dump the HTML, return a
                # neutral error pointing at the dump, and let the caller (or
                # future debugging) inspect it. Possible causes the dump will
                # disambiguate: (a) genuine paywall / no entitlement, (b) SD
                # page-structure change, (c) DataDome on the detail page that
                # slipped past earlier checks.
                try:
                    os.makedirs("scratch", exist_ok=True)
                    with open("scratch/dump_fail.html", "w", encoding="utf-8") as f:
                        f.write(html)
                except Exception:
                    pass
                print(f"Could not find a /{safe_name}/pdfft anchor on the detail page. HTML dumped to dump_fail.html")
                return (
                    f"Error: No /{safe_name}/pdfft anchor on the ScienceDirect "
                    "detail page (possible causes: paywall / no entitlement, "
                    "page-structure change, or anti-bot interstitial). "
                    "HTML dumped to scratch/dump_fail.html for inspection."
                )
                
            print(f"Discovered signed PDF URL: {pdf_url}")
            
            import re
            pii_match = re.search(r'/pii/([A-Z0-9]+)', detail_url)
            safe_name = pii_match.group(1) if pii_match else "unknown_pii"
            file_path = os.path.join(output_dir, f"sd_{safe_name}.pdf")

            # The PDF byte fetch MUST originate from inside the article page's
            # JS context. A context.request.get is silently DataDome-blocked
            # (returns 403 + ~830KB challenge HTML even with cookies), exactly
            # like the old fetch_ris bug. We already navigated to detail_url
            # above, so an in-page fetch is a same-origin in-app XHR carrying
            # the live DataDome cookies. Mirrors the proven ACM base64 path.
            direct_status = None
            direct_type = ""
            direct_size = 0
            try:
                fetched = await self.page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {credentials: 'include',
                            headers: {'Accept': 'application/pdf,*/*'}});
                        const status = r.status;
                        const ctype = (r.headers.get('content-type') || '').toLowerCase();
                        const blob = await r.blob();
                        const b64 = await new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve((reader.result || '').split(',')[1] || '');
                            reader.onerror = reject;
                            reader.readAsDataURL(blob);
                        });
                        return {status, ctype, b64};
                    }""",
                    pdf_url,
                )
                import base64 as _b64
                if isinstance(fetched, dict):
                    direct_body = _b64.b64decode(fetched.get("b64") or "")
                    direct_status = fetched.get("status")
                    direct_type = fetched.get("ctype") or ""
                else:
                    direct_body = b""
                direct_size = len(direct_body)
                if direct_status == 200 and (
                    direct_body.startswith(b"%PDF") or "application/pdf" in direct_type
                ):
                    with open(file_path, "wb") as f:
                        f.write(direct_body)
                    return file_path
                # Silent DataDome: 200 with text/html body, or 403 from the asset host.
                if b"datadome" in direct_body[:4096].lower() or b"are you a robot" in direct_body[:4096].lower():
                    return (
                        "Error: ScienceDirect served a DataDome challenge for the PDF stream "
                        f"(in-page fetch returned HTTP {direct_status}, {direct_size} bytes of "
                        f"content-type {direct_type or 'unknown'}). Re-run with manual verification "
                        f"enabled. PDF URL: {pdf_url}"
                    )
                print(
                    f"SD in-page PDF fetch did not return a PDF: status={direct_status}, content-type={direct_type}, body={direct_size}B"
                )
                # DIAGNOSTIC: dump the non-PDF body so we can analyze the
                # viewer page's actual structure (looking for embedded S3
                # URLs, download buttons, etc).
                try:
                    os.makedirs("scratch", exist_ok=True)
                    import time as _t
                    _vd = os.path.join("scratch", f"sd_viewer_{safe_name}_{int(_t.time())}.html")
                    with open(_vd, "wb") as _vf:
                        _vf.write(direct_body)
                    print(f"SD viewer HTML dumped -> {_vd}")
                except Exception as _e:
                    print(f"SD viewer dump failed: {_e}")
            except Exception as e:
                print(f"SD in-page PDF fetch failed, falling back to viewer flow: {e}")
            
            # Navigate to the EPDF viewer. The /pdfft entry triggers a redirect chain that
            # ultimately serves PDF bytes from pdf.sciencedirectassets.com (an S3 signed URL).
            # The trick: hook expect_response BEFORE navigating so we capture the PDF body in
            # flight -- this is more reliable than the legacy "click button, wait for download
            # event" path because (a) the download event doesn't always fire on Firefox with
            # pdfjs disabled and (b) the signed URL only appears in page.url after the click.
            def is_pdf_response(resp):
                ct = resp.headers.get("content-type", "").lower()
                if "application/pdf" in ct:
                    return True
                if "sciencedirectassets.com" in resp.url and ".pdf" in resp.url:
                    return True
                return False

            try:
                async with self.page.expect_response(is_pdf_response, timeout=45000) as pdf_resp_info:
                    try:
                        await self.page.goto(pdf_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        # Inline PDF responses can abort the goto with NS_BINDING_ABORTED on
                        # Firefox. The response we want has already fired by then.
                        print(f"SD viewer goto raised (likely the PDF response already fired): {e}")
                pdf_response = await pdf_resp_info.value
                pdf_body = await pdf_response.body()
                if pdf_response.status == 200 and pdf_body.startswith(b"%PDF"):
                    with open(file_path, "wb") as f:
                        f.write(pdf_body)
                    print(
                        f"SD asset-direct path succeeded "
                        f"({len(pdf_body)} bytes intercepted from {pdf_response.url[:80]}...)."
                    )
                    return file_path
                print(
                    f"SD intercepted PDF response was not a valid PDF: status={pdf_response.status}, "
                    f"url={pdf_response.url[:120]!r}, body={len(pdf_body)}B"
                )
            except Exception as e:
                print(f"SD response interception failed, falling back to viewer button flow: {e}")

            # Phase A: brief silent wait (Turnstile sometimes auto-solves
            # within a few seconds; no need to bother the user yet).
            for _ in range(20):  # 10s
                if _pdf_sniff["body"]:
                    break
                await asyncio.sleep(0.5)
            if _pdf_sniff["body"]:
                if _pdf_sniff.get("saved_path"):
                    # File already saved via download event; mirror to file_path.
                    import shutil
                    if os.path.abspath(_pdf_sniff["saved_path"]) != os.path.abspath(file_path):
                        shutil.copyfile(_pdf_sniff["saved_path"], file_path)
                    print(f"SD download hit during silent wait -> {file_path}")
                else:
                    with open(file_path, "wb") as f:
                        f.write(_pdf_sniff["body"])
                    print(f"SD sniffer hit during silent wait ({len(_pdf_sniff['body'])}B)")
                return file_path

            # Phase B: if Phase A timed out AND we saw Turnstile / S3 403
            # markers, the S3 PDF host is gated by Cloudflare Turnstile (a
            # different anti-bot from the DataDome on the detail page) and
            # this can ONLY be passed by a human clicking the Turnstile
            # widget. Escalate to headful so the user sees it.
            needs_manual = _pdf_sniff["turnstile"] or _pdf_sniff["datadome_403"]
            if needs_manual and not getattr(self, "is_headful", False):
                if not self.allow_headful_fallback:
                    return (
                        "Error: ScienceDirect PDF host is gated by Cloudflare Turnstile "
                        "(separate from the detail-page DataDome). Enable "
                        "allow_headful_fallback (with SD in allow_headful_fallback_platforms) "
                        "so a browser window can pop for manual verification.\n"
                        f"Signed PDF URL: {pdf_url}"
                    )
                print("[Anti-Bot] Cloudflare Turnstile detected on SD PDF host. Relaunching headful for manual solve...")
                await self.close()
                await self._ensure_browser(force_headful=True)
                self.is_headful = True
                return await self.download_paper(detail_url, output_dir)

            # Phase C: already headful or no captcha markers -- give user
            # generous time to solve Turnstile manually in the visible window.
            if getattr(self, "is_headful", False):
                print(">>> Please pass the Cloudflare Turnstile / DataDome challenge in the browser window. Waiting up to 120s... <<<")
                for _ in range(240):  # 120s
                    if _pdf_sniff["body"]:
                        break
                    await asyncio.sleep(0.5)
                if _pdf_sniff["body"]:
                    if _pdf_sniff.get("saved_path"):
                        import shutil
                        if os.path.abspath(_pdf_sniff["saved_path"]) != os.path.abspath(file_path):
                            shutil.copyfile(_pdf_sniff["saved_path"], file_path)
                        print(f"SD download hit after manual solve -> {file_path}")
                    else:
                        with open(file_path, "wb") as f:
                            f.write(_pdf_sniff["body"])
                        print(f"SD sniffer hit after manual solve ({len(_pdf_sniff['body'])}B)")
                    return file_path

            return (
                f"Error: SD PDF download failed (sniffer captured no PDF body).\n"
                f"Signed PDF URL: {pdf_url}\n"
                f"Turnstile detected: {_pdf_sniff['turnstile']}, S3-403 detected: {_pdf_sniff['datadome_403']}\n"
                f"Pages in context: {[(p.url or '')[:120] for p in (self.context.pages if self.context else [])]}\n"
                f"This means Cloudflare Turnstile on pdf.sciencedirectassets.com was not solved in time."
            )

        except Exception as e:
            return f"Error downloading PDF: {e}"

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not pdf_path or not os.path.exists(pdf_path) or "Error" in pdf_path or "Failed" in pdf_path:
            return f"Failed to download PDF: {pdf_path}"
            
        if not pdf_path.lower().endswith(".pdf"):
            return f"Downloaded file is not a PDF. Saved at: {pdf_path}"
            
        try:
            images_dir = os.path.join(output_dir, "images")
            os.makedirs(images_dir, exist_ok=True)
            
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

    async def fetch_ris(self, detail_url: str) -> str:
        """ScienceDirect: their public sdfe/arp/cite endpoint serves RIS.

        Direct context.request.get is silently blocked by DataDome
        (returns 403 + ~830KB challenge HTML even with cookies attached).
        Issuing the request from inside the page via fetch() makes it
        look like a normal in-app XHR and clears the challenge.
        """
        m = re.search(r"/pii/([A-Za-z0-9]+)", detail_url)
        if not m:
            return ""
        pii = m.group(1)
        await self._ensure_browser()
        # The in-page fetch must originate from a ScienceDirect article
        # so DataDome sees a coherent referrer + live cookies. If a prior
        # get_paper_details left us on this exact PII we reuse it; otherwise
        # navigate.
        try:
            current = self.page.url or ""
        except Exception:
            current = ""
        if pii not in current:
            try:
                await self.page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[SD] fetch_ris pre-nav failed: {e}")
                return ""
        url = (
            "https://www.sciencedirect.com/sdfe/arp/cite"
            f"?pii={pii}&format=application/x-research-info-systems&withabstract=true"
        )
        js = """async (u) => {
            const r = await fetch(u, {
                credentials: 'include',
                headers: {'Accept': 'application/x-research-info-systems,*/*'}
            });
            return {status: r.status, body: await r.text()};
        }"""
        try:
            out = await self.page.evaluate(js, url)
            status = out.get("status") if isinstance(out, dict) else None
            body = (out.get("body") or "") if isinstance(out, dict) else ""
            body = body.replace("\r\n", "\n").strip()
            if status == 200 and "TY  - " in body and "ER" in body:
                return body
            print(f"[SD] fetch_ris non-RIS response: status={status} len={len(body)}")
        except Exception as e:
            print(f"[SD] fetch_ris failed: {e}")
        return ""


scraper_instance = ScienceDirectScraper()
