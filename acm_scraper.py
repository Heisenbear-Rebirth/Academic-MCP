import asyncio
import functools
import base64
from bs4 import BeautifulSoup
import os
import hashlib
import pymupdf4llm
import aiohttp
from typing import Dict, List
import urllib.parse
import re
import sys
from mcp_logging import safe_stderr_print
from runtime_config import (
    allow_headful_fallback_for,
    ensure_runtime_environment,
    manual_verification_timeout_seconds,
    project_path,
)

print = safe_stderr_print
ensure_runtime_environment()

class ACMScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.camoufox_cm = None
        self.allow_headful_fallback = allow_headful_fallback_for("ACM")
        self.manual_verification_timeout = manual_verification_timeout_seconds()

    @staticmethod
    def _is_cloudflare_title(title: str) -> bool:
        title = (title or "").lower()
        return any(
            marker in title
            for marker in [
                "just a moment",
                "un momento",
                "cloudflare",
                "checking your browser",
                "attention required",
            ]
        )
        
    def _context_is_alive(self) -> bool:
        """True when self.context exists and its underlying browser is still connected.
        A previous CF-induced timeout can leave context/page non-None but referring to a
        torn-down browser process; subsequent calls then trip TargetClosedError."""
        if not self.context:
            return False
        try:
            browser = self.context.browser
            if browser is not None and not browser.is_connected():
                return False
            page = self.page
            if page is None or page.is_closed():
                return False
        except Exception:
            return False
        return True

    async def _reset_state(self) -> None:
        """Drop references to a wedged browser/context so the next _ensure_browser rebuilds it."""
        try:
            if self.camoufox_cm:
                await self.camoufox_cm.__aexit__(None, None, None)
        except Exception:
            pass
        self.camoufox_cm = None
        self.context = None
        self.page = None

    async def _ensure_browser(self, force_headful=False):
        if self.context and not self._context_is_alive():
            print("ACM context detected as closed; rebuilding from scratch.")
            await self._reset_state()
        if not self.context:
            print(f"Initializing ACM Persistent Browser Context (Headless: {not force_headful})...")
            from scraper_utils import pooled_profile, load_or_create_fingerprint
            profile_dir, self._profile_ephemeral = pooled_profile(".acm_profile", "ACM")
            self._profile_dir = profile_dir
            _shared_fp = load_or_create_fingerprint("ACM")

            # Include Firefox-style parent.lock / .parentlock so a crashed prior Camoufox
            # process can't keep us from launching.
            for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
                lfile = os.path.join(profile_dir, lock_name)
                if os.path.exists(lfile):
                    try: os.remove(lfile)
                    except: pass
                    
            self.playwright = None # Will not be used anymore
            from camoufox.async_api import AsyncCamoufox
            
            # Using OSINT stealth browser to evade hard blocks.
            # Keep ACM Camoufox-only: ordinary Chromium is less useful against ACM/Cloudflare.
            # ACM rides Cloudflare Turnstile + DataDome; image blocking is detectable.
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

            from scraper_utils import apply_browser_cookies, capture_browser_cookies
            await apply_browser_cookies(self.context, "ACM")

            # Navigate to ACM to acquire initial cookies and bypass CF
            print("Navigating to ACM DL (Resolving protections)...")
            await self.page.goto("https://dl.acm.org", wait_until="domcontentloaded", timeout=45000)
            
            # Smart wait for Cloudflare
            cf_blocked = True
            for _ in range(15):
                try:
                    title = await self.page.title()
                    if not self._is_cloudflare_title(title):
                        cf_blocked = False
                        break
                except Exception:
                    pass # Navigation in progress
                await asyncio.sleep(1)
                
            if cf_blocked:
                if not force_headful:
                    if not self.allow_headful_fallback:
                        print("[Anti-Bot] ACM Cloudflare blocked headless mode; headful fallback is disabled.")
                        return
                    print("[Anti-Bot] ACM Cloudflare blocked in headless! Relaunching headful for manual check...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    return # Exit the current call as the recursive one handles the rest
                else:
                    print(f">>> PLEASE SOLVE ACM CLOUDFLARE IN THE CAMOUFOX WINDOW. Waiting up to {self.manual_verification_timeout}s... <<<")
                    solved = False
                    for _ in range(self.manual_verification_timeout):
                        try:
                            title = await self.page.title()
                            if not self._is_cloudflare_title(title):
                                print("[Anti-Bot] ACM Cloudflare passed! Proceeding...")
                                solved = True
                                cf_blocked = False
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                    if not solved:
                        print("ACM Cloudflare not solved in time!")
            
            # Click accept cookies banner
            try:
                # Cookiebot or similar consent managers
                btn = self.page.locator('button:has-text("Allow all cookies"), button:has-text("Accept")')
                if await btn.count() > 0:
                    await btn.first.click()
                    print("Accepted ACM cookies.")
                    await asyncio.sleep(1)
            except Exception as e:
                pass
                
            if not cf_blocked:
                await capture_browser_cookies(self.context, "ACM")
            print("ACM context initialized successfully.")

    async def warmup_auth(self, timeout_seconds: int = None) -> Dict:
        """Open ACM headful, wait for human verification, then persist cookies."""
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
        ok = bool(self.page) and not self._is_cloudflare_title(title) and not self._is_blocked_html(html)
        try:
            from scraper_utils import capture_browser_cookies
            await capture_browser_cookies(
                self.context,
                "ACM",
                note="manual warmup verified" if ok else "manual warmup attempted",
            )
        except Exception:
            pass
        return {
            "platform": "ACM",
            "ok": ok,
            "seconds": round(asyncio.get_event_loop().time() - started, 3),
            "title": title,
            "url": self.page.url if self.page else "",
        }

    async def _safe_page_content(self, retries: int = 8) -> str:
        last_error = None
        for attempt in range(retries):
            try:
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                return await self.page.content()
            except Exception as e:
                last_error = e
                if "navigating" not in str(e).lower() and attempt >= 2:
                    break
                await asyncio.sleep(1)
        raise last_error

    @staticmethod
    def _extract_year(text: str) -> int | None:
        if not text:
            return None
        match = re.search(r"\b(?:19|20)\d{2}\b", str(text))
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
    def _source_type_allowed(doc_type: str, source_type: str = "all") -> bool:
        source_type = str(source_type or "").strip().lower()
        if source_type in {"", "all", "全部"}:
            return True
        normalized = re.sub(r"\s+", "-", str(doc_type or "").strip().lower())
        aliases = {
            "research-article": {"research-article", "article", "journal-article", "proceedings-article"},
            "short-paper": {"short-paper", "short-paper", "proceedings-article"},
            "review-article": {"review-article", "review", "journal-article"},
            "tutorial": {"tutorial"},
            "opinion": {"opinion"},
        }
        allowed = aliases.get(source_type, {source_type})
        return any(token in normalized for token in allowed)

    @staticmethod
    def _is_blocked_html(text: str, status: int = None) -> bool:
        lower = (text or "")[:5000].lower()
        if status in {401, 403, 429, 503} and (
            "cloudflare" in lower
            or "just a moment" in lower
            or "checking your browser" in lower
            or "cf-browser-verification" in lower
        ):
            return True
        return "cf-browser-verification" in lower or "<title>just a moment" in lower

    async def _context_cookie_header(self) -> str:
        if not self.context:
            return ""
        try:
            cookies = await self.context.cookies("https://dl.acm.org")
        except Exception:
            return ""
        return "; ".join(
            f"{cookie.get('name')}={cookie.get('value')}"
            for cookie in cookies
            if cookie.get("name") and cookie.get("value")
        )

    async def _curl_cffi_pdf_get(self, url: str, *, referer: str = None) -> bytes:
        self._last_acm_pdf_probe = None
        try:
            from curl_cffi import requests as curl_requests
        except Exception as e:
            print(f"[ACM] curl_cffi not available for PDF fetch: {e}")
            return b""

        ua = ""
        if self.page:
            try:
                ua = await self.page.evaluate("navigator.userAgent")
            except Exception:
                ua = ""
        try:
            cookies = await self.context.cookies("https://dl.acm.org")
        except Exception:
            cookies = []
        cookie_dict = {
            c.get("name"): c.get("value")
            for c in cookies
            if c.get("name") and c.get("value")
        }
        headers = {
            "Accept": "application/pdf,application/octet-stream,*/*",
            "Referer": referer or "https://dl.acm.org/",
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

        for impersonate in ("chrome124", "chrome120", "chrome136"):
            try:
                response = await asyncio.to_thread(_get_once, impersonate)
                body = response.content or b""
                ctype = response.headers.get("content-type", "")
                self._last_acm_pdf_probe = {
                    "status": response.status_code,
                    "content_type": ctype,
                    "size": len(body),
                    "body_head": body[:8192],
                    "impersonate": impersonate,
                }
                print(
                    f"[ACM] curl_cffi PDF fetch ({impersonate}) "
                    f"status={response.status_code} ct={ctype!r} size={len(body)}"
                )
                if response.status_code == 200 and body.startswith(b"%PDF"):
                    return body
            except Exception as e:
                print(f"[ACM] curl_cffi PDF fetch ({impersonate}) failed: {e}")
        return b""

    async def _curl_cffi_get_text(self, url: str, *, referer: str = None) -> str:
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
        try:
            cookies = await self.context.cookies("https://dl.acm.org")
        except Exception:
            cookies = []
        cookie_dict = {
            c.get("name"): c.get("value")
            for c in cookies
            if c.get("name") and c.get("value")
        }
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer or "https://dl.acm.org/",
        }
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

        for impersonate in ("chrome124", "chrome120", "chrome136"):
            try:
                response = await asyncio.to_thread(_get_once, impersonate)
                text = response.text or ""
                print(
                    f"[ACM] curl_cffi HTML fetch ({impersonate}) "
                    f"status={response.status_code} size={len(text)}"
                )
                if self._is_blocked_html(text, response.status_code):
                    last_error = RuntimeError(
                        f"ACM curl_cffi HTML fetch blocked by verification "
                        f"(HTTP {response.status_code})"
                    )
                    continue
                if response.status_code >= 400:
                    last_error = RuntimeError(f"ACM curl_cffi HTML fetch failed: HTTP {response.status_code}")
                    continue
                return text
            except Exception as e:
                last_error = e
                print(f"[ACM] curl_cffi HTML fetch ({impersonate}) failed: {e}")
        raise RuntimeError(str(last_error) if last_error else "ACM curl_cffi HTML fetch failed")

    async def _detail_exposes_pdf_link(self, detail_url: str, doi: str) -> bool:
        html = await self._curl_cffi_get_text(detail_url, referer="https://dl.acm.org/")
        encoded_doi = urllib.parse.quote(doi, safe="")
        needles = (
            f"/doi/pdf/{doi}".lower(),
            f"/doi/pdf/{encoded_doi}".lower(),
            f"/doi/epdf/{doi}".lower(),
            f"/doi/epdf/{encoded_doi}".lower(),
            "view pdf",
            "download pdf",
        )
        low = html.lower()
        return any(needle in low for needle in needles)

    async def _direct_get_bytes(self, url: str, *, referer: str = None, accept: str = "*/*") -> bytes:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer or "https://dl.acm.org/",
        }
        cookie_header = await self._context_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header

        timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.get(url, headers=headers, ssl=False, allow_redirects=True) as resp:
                body = await resp.read()
                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type.lower():
                    text = body[:8000].decode(resp.charset or "utf-8", errors="replace")
                    if self._is_blocked_html(text, resp.status):
                        raise RuntimeError(f"ACM direct request blocked by verification (HTTP {resp.status})")
                if resp.status >= 400:
                    raise RuntimeError(f"ACM direct request failed: HTTP {resp.status}")
                return body

    async def _direct_get_text(self, url: str, *, referer: str = None) -> str:
        body = await self._direct_get_bytes(
            url,
            referer=referer,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        text = body.decode("utf-8", errors="replace")
        if self._is_blocked_html(text):
            raise RuntimeError("ACM direct request returned verification HTML")
        return text

    async def _context_request_bytes(self, url: str, *, referer: str = None, accept: str = "*/*") -> bytes:
        if not self.context:
            raise RuntimeError("ACM browser context is not initialized")
        headers = {
            "Accept": accept,
            "Referer": referer or "https://dl.acm.org/",
        }
        response = await self.context.request.get(url, headers=headers, timeout=60000)
        body = await response.body()
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type.lower():
            text = body[:8000].decode("utf-8", errors="replace")
            if self._is_blocked_html(text, response.status):
                raise RuntimeError(f"ACM context request blocked by verification (HTTP {response.status})")
        if response.status >= 400:
            raise RuntimeError(f"ACM context request failed: HTTP {response.status}")
        return body

    async def _context_request_text(self, url: str, *, referer: str = None) -> str:
        body = await self._context_request_bytes(
            url,
            referer=referer,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        text = body.decode("utf-8", errors="replace")
        if self._is_blocked_html(text):
            raise RuntimeError("ACM context request returned verification HTML")
        return text

    async def _page_fetch_text(self, url: str, *, accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") -> str:
        if not self.page:
            raise RuntimeError("ACM page is not initialized")
        result = await self.page.evaluate(
            """async ({url, accept}) => {
                const response = await fetch(url, {
                    credentials: 'include',
                    headers: { Accept: accept }
                });
                const text = await response.text();
                return {
                    status: response.status,
                    ok: response.ok,
                    contentType: response.headers.get('content-type') || '',
                    text
                };
            }""",
            {"url": url, "accept": accept},
        )
        text = result.get("text") or ""
        if self._is_blocked_html(text, result.get("status")):
            raise RuntimeError(f"ACM page fetch blocked by verification (HTTP {result.get('status')})")
        if not result.get("ok"):
            raise RuntimeError(f"ACM page fetch failed: HTTP {result.get('status')}")
        return text

    async def _page_fetch_bytes(self, url: str, *, accept: str = "*/*") -> bytes:
        if not self.page:
            raise RuntimeError("ACM page is not initialized")
        result = await self.page.evaluate(
            """async ({url, accept}) => {
                const response = await fetch(url, {
                    credentials: 'include',
                    headers: { Accept: accept }
                });
                const buffer = await response.arrayBuffer();
                let binary = '';
                const bytes = new Uint8Array(buffer);
                const chunkSize = 0x8000;
                for (let i = 0; i < bytes.length; i += chunkSize) {
                    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                }
                return {
                    status: response.status,
                    ok: response.ok,
                    contentType: response.headers.get('content-type') || '',
                    bodyBase64: btoa(binary)
                };
            }""",
            {"url": url, "accept": accept},
        )
        body = base64.b64decode(result.get("bodyBase64") or "")
        content_type = result.get("contentType") or ""
        if "text/html" in content_type.lower():
            text = body[:8000].decode("utf-8", errors="replace")
            if self._is_blocked_html(text, result.get("status")):
                raise RuntimeError(f"ACM page fetch blocked by verification (HTTP {result.get('status')})")
        if not result.get("ok"):
            raise RuntimeError(f"ACM page fetch failed: HTTP {result.get('status')}")
        return body

    def _parse_search_html(
        self,
        html: str,
        *,
        limit: int,
        offset_in_first_page: int,
        journal_clean: str,
        source_type: str,
        start_year: int = None,
        end_year: int = None,
    ) -> Dict:
        soup = BeautifulSoup(html, "html.parser")

        total_str = "未知"
        hits_elem = soup.select_one(".hitsLength, .result__count, span.limit, div.issue-heading")
        if hits_elem:
            txt = hits_elem.text.strip()
            numbers = re.findall(r"[\d,]+", txt)
            if numbers:
                valid_nums = [n.replace(",", "") for n in numbers]
                total_str = str(max(int(n) for n in valid_nums if n.isdigit()))

        papers = []
        items = soup.select(".issue-item")
        collected = 0
        from scraper_utils import normalize_acm_doi, venue_matches

        for i, item in enumerate(items):
            if i < offset_in_first_page:
                continue
            if collected >= limit:
                break

            title_tag = item.select_one("h5.issue-item__title a, h2.issue-item__title a, .hlFld-Title a")
            title = title_tag.text.strip() if title_tag else "N/A"
            link = "https://dl.acm.org" + title_tag["href"] if title_tag and title_tag.has_attr("href") else ""

            authors_tags = item.select(".author-name, .loa__author-name, a[href*='/profile/']")
            authors = [a.text.strip() for a in authors_tags] if authors_tags else []
            author_str = ", ".join(authors) if authors else "N/A"

            item_text = item.get_text(" ", strip=True)
            year = self._extract_year(item_text)
            if not self._year_allowed(year, start_year=start_year, end_year=end_year):
                continue
            date_tag = item.select_one(".dot-separator span")
            date = str(year) if year else (date_tag.text.strip() if date_tag else "N/A")
            if date.lower().startswith("pages"):
                date = "N/A"

            type_tag = item.select_one(".issue-heading")
            doc_type = type_tag.text.strip() if type_tag else "Article"
            if not self._source_type_allowed(doc_type, source_type=source_type):
                continue

            doi = normalize_acm_doi(link)
            venue_tag = item.select_one(".issue-item__detail a, .epub-section__title")
            venue_name = venue_tag.text.strip() if venue_tag else ""
            if journal_clean and venue_name and not venue_matches(journal_clean, venue_name):
                continue

            uid = hashlib.md5(link.encode()).hexdigest()[:8]
            papers.append({
                "id": uid,
                "title": title,
                "author": author_str,
                "source": venue_name or "ACM Digital Library",
                "venue_name": venue_name,
                "doi": doi,
                "date": date,
                "db_type": doc_type,
                "detail_link": link,
            })
            collected += 1

        return {"total_results": total_str, "papers": papers}

    def _parse_detail_html(self, detail_url: str, html: str) -> Dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        abstract_div = soup.select_one(".abstractSection, #abstract")
        abstract = abstract_div.text.strip() if abstract_div else "No abstract provided."
        abstract = re.sub(r"^\s*Abstract\s*", "", abstract)

        keywords = []
        kw_tags = soup.select(".core-concept, .loa__concept, .chapter-concept")
        for tag in kw_tags:
            keywords.append(tag.text.strip())

        from scraper_utils import normalize_acm_doi
        doi = normalize_acm_doi(detail_url)
        return {
            "url": detail_url,
            "abstract": abstract.replace("\n", " "),
            "keywords": keywords,
            "doi": doi,
        }

    async def initialize(self):
        await self._ensure_browser()

    async def close(self):
        if getattr(self, "context", None):
            try:
                from scraper_utils import capture_browser_cookies
                await capture_browser_cookies(self.context, "ACM")
            except Exception:
                pass
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            try:
                await self.camoufox_cm.__aexit__(None, None, None)
            except Exception as e:
                print(f"ACM context close failed: {e}")
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        if getattr(self, "_profile_dir", None):
            from scraper_utils import cleanup_pooled_profile
            cleanup_pooled_profile(self._profile_dir, getattr(self, "_profile_ephemeral", False))
            self._profile_dir = None

    async def search_papers(self, query: str, search_field: str = "AllField", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        page_size = 20
        start_page = start_index // page_size
        offset_in_first_page = start_index % page_size

        field_map = {
            "全部": "AllField",
            "主题": "AllField",
            "篇名": "Title",
            "摘要": "Abstract",
            "作者": "Author",
        }
        field = field_map.get(search_field, search_field if search_field in ["AllField", "Title", "Abstract", "Author"] else "AllField")

        journal_clean = (journal or "").strip()
        if journal_clean:
            quoted_journal = journal_clean.replace('"', '\\"')
            combined_q = f'"{quoted_journal}" AND ({query})'
        else:
            combined_q = query

        params = [
            (field, combined_q),
            ("startPage", str(start_page)),
            ("pageSize", str(page_size)),
        ]
        if start_year:
            params.append(("AfterYear", str(start_year)))
        if end_year:
            params.append(("BeforeYear", str(end_year)))
        if sort_by == "date_desc":
            params.append(("sortBy", "EpubDate_desc"))
        if journal_clean:
            params.append(("PubName", journal_clean))
        if source_type.lower() not in ["all", "全部", ""]:
            params.append(("ConceptID", source_type))

        q_url = f"https://dl.acm.org/action/doSearch?{urllib.parse.urlencode(params)}"
        print(f"Navigating to ACM search: {q_url}")

        direct_error = None
        try:
            html = await self._direct_get_text(q_url)
            print("[ACM] Fetched search HTML via direct HTTP.")
            return self._parse_search_html(
                html,
                limit=limit,
                offset_in_first_page=offset_in_first_page,
                journal_clean=journal_clean,
                source_type=source_type,
                start_year=start_year,
                end_year=end_year,
            )
        except Exception as e:
            direct_error = e
            print(f"[ACM] Pure direct HTTP search failed ({e}); acquiring browser verification cookies.")

        try:
            await self._ensure_browser()
            html = await self._curl_cffi_get_text(q_url, referer="https://dl.acm.org/")
            print("[ACM] Fetched search HTML via curl_cffi verified-cookie HTTP.")
            return self._parse_search_html(
                html,
                limit=limit,
                offset_in_first_page=offset_in_first_page,
                journal_clean=journal_clean,
                source_type=source_type,
                start_year=start_year,
                end_year=end_year,
            )
        except Exception as e:
            print(f"[ACM] curl_cffi verified-cookie search failed ({e}); trying aiohttp.")

        try:
            html = await self._direct_get_text(q_url)
            print("[ACM] Fetched search HTML via verified-cookie direct HTTP.")
            return self._parse_search_html(
                html,
                limit=limit,
                offset_in_first_page=offset_in_first_page,
                journal_clean=journal_clean,
                source_type=source_type,
                start_year=start_year,
                end_year=end_year,
            )
        except Exception as e:
            print(f"[ACM] Verified-cookie aiohttp search failed ({e}); trying context request.")

        try:
            html = await self._context_request_text(q_url)
            print("[ACM] Fetched search HTML via verified context request.")
            return self._parse_search_html(
                html,
                limit=limit,
                offset_in_first_page=offset_in_first_page,
                journal_clean=journal_clean,
                source_type=source_type,
                start_year=start_year,
                end_year=end_year,
            )
        except Exception as e:
            print(f"[ACM] Verified context request search failed ({e}); falling back to browser-rendered page.")

        try:
            html = await self._page_fetch_text(q_url)
            print("[ACM] Fetched search HTML via verified page fetch.")
            return self._parse_search_html(
                html,
                limit=limit,
                offset_in_first_page=offset_in_first_page,
                journal_clean=journal_clean,
                source_type=source_type,
                start_year=start_year,
                end_year=end_year,
            )
        except Exception as e:
            print(f"[ACM] Verified page fetch search failed ({e}); falling back to browser-rendered page.")

        print(f"[ACM] Falling back to browser-rendered search after direct failure: {direct_error}")
        for attempt in range(2):
            await self.page.goto(q_url, wait_until="domcontentloaded")

            cf_blocked = True
            for _ in range(15):
                try:
                    title = await self.page.title()
                    if not self._is_cloudflare_title(title):
                        cf_blocked = False
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)

            if cf_blocked:
                if attempt == 0:
                    if not self.allow_headful_fallback:
                        return {
                            "total_results": "0",
                            "papers": [],
                            "error": "ACM Cloudflare verification blocked the headless MCP session. Add ACM to allow_headful_fallback_platforms in mcp_runtime_config.json for manual browser verification.",
                        }
                    print("[Anti-Bot] ACM Cloudflare blocked in headless search! Relaunching headful...")
                    await self.close()
                    await self._ensure_browser(force_headful=True)
                    continue
                print(f">>> PLEASE SOLVE ACM CLOUDFLARE IN THE CAMOUFOX WINDOW. Waiting up to {self.manual_verification_timeout}s... <<<")
                solved = False
                for _ in range(self.manual_verification_timeout):
                    try:
                        title = await self.page.title()
                        if not self._is_cloudflare_title(title):
                            print("[Anti-Bot] ACM Cloudflare passed! Proceeding...")
                            solved = True
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                if not solved:
                    return {
                        "total_results": "0",
                        "papers": [],
                        "error": "ACM Cloudflare verification was not completed before the manual verification timeout.",
                    }
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                break
            break

        await asyncio.sleep(3)
        html = await self._safe_page_content()
        parsed = self._parse_search_html(
            html,
            limit=limit,
            offset_in_first_page=offset_in_first_page,
            journal_clean=journal_clean,
            source_type=source_type,
            start_year=start_year,
            end_year=end_year,
        )
        if not parsed["papers"]:
            print("No items found. Current title is:", await self.page.title())
            if "cf-browser-verification" in html or "Just a moment" in html or "Un momento" in html:
                print("Still stuck in Cloudflare...")
        return parsed

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        direct_error = None
        try:
            html = await self._direct_get_text(detail_url, referer="https://dl.acm.org/")
            print("[ACM] Fetched detail HTML via direct HTTP.")
            return self._parse_detail_html(detail_url, html)
        except Exception as e:
            direct_error = e
            print(f"[ACM] Pure direct HTTP detail failed ({e}); acquiring browser verification cookies.")

        await self._ensure_browser()
        try:
            html = await self._curl_cffi_get_text(detail_url, referer="https://dl.acm.org/")
            print("[ACM] Fetched detail HTML via curl_cffi verified-cookie HTTP.")
            return self._parse_detail_html(detail_url, html)
        except Exception as e:
            print(f"[ACM] curl_cffi verified-cookie detail failed ({e}); trying aiohttp.")

        try:
            html = await self._direct_get_text(detail_url, referer="https://dl.acm.org/")
            print("[ACM] Fetched detail HTML via verified-cookie direct HTTP.")
            return self._parse_detail_html(detail_url, html)
        except Exception as e:
            print(f"[ACM] Verified-cookie aiohttp detail failed ({e}); trying context request.")

        try:
            html = await self._context_request_text(detail_url, referer="https://dl.acm.org/")
            print("[ACM] Fetched detail HTML via verified context request.")
            return self._parse_detail_html(detail_url, html)
        except Exception as e:
            print(f"[ACM] Verified context request detail failed ({e}); falling back to browser-rendered page.")

        try:
            html = await self._page_fetch_text(detail_url)
            print("[ACM] Fetched detail HTML via verified page fetch.")
            return self._parse_detail_html(detail_url, html)
        except Exception as e:
            print(f"[ACM] Verified page fetch detail failed ({e}); falling back to browser-rendered page.")

        print(f"[ACM] Falling back to browser-rendered detail after direct failure: {direct_error}")
        from scraper_utils import goto_with_retry
        await goto_with_retry(self.page, detail_url, wait_until="domcontentloaded")

        for _ in range(15):
            try:
                title = await self.page.title()
                if not self._is_cloudflare_title(title):
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        await asyncio.sleep(2)
        html = await self.page.content()
        return self._parse_detail_html(detail_url, html)

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)

        from scraper_utils import normalize_acm_doi
        doi = normalize_acm_doi(detail_url)
        if not doi:
            return f"Could not parse ACM DOI from URL: {detail_url}"

        # ACM direct PDF link
        pdf_url = f"https://dl.acm.org/doi/pdf/{doi}"
        
        # Due to ACM's CF, normal urllib might fail. We use Playwright Request Context to download.
        # Alternatively, we can navigate to the pdf URL and wait for download event, but ACM's PDF url serves the stream.
        # So we can capture the response from a fetch evaluate inside page context, which carries the cookies.
        safe_name = doi.replace('/', '_').replace('.', '_')
        file_path = os.path.join(output_dir, f"acm_{safe_name}.pdf")

        direct_error = None
        try:
            body = await self._direct_get_bytes(
                pdf_url,
                referer=detail_url,
                accept="application/pdf,application/octet-stream,*/*",
            )
            if not body.startswith(b"%PDF"):
                raise RuntimeError("payload is not a PDF")
            with open(file_path, "wb") as f:
                f.write(body)
            print(f"[ACM] Downloaded PDF via direct HTTP: {file_path}")
            return file_path
        except Exception as e:
            direct_error = e
            print(f"[ACM] Pure direct HTTP PDF failed ({e}); acquiring browser verification cookies.")

        await self._ensure_browser()
        try:
            body = await self._direct_get_bytes(
                pdf_url,
                referer=detail_url,
                accept="application/pdf,application/octet-stream,*/*",
            )
            if not body.startswith(b"%PDF"):
                raise RuntimeError("payload is not a PDF")
            with open(file_path, "wb") as f:
                f.write(body)
            print(f"[ACM] Downloaded PDF via verified-cookie direct HTTP: {file_path}")
            return file_path
        except Exception as e:
            print(f"[ACM] Verified-cookie direct HTTP PDF failed ({e}); falling back to Playwright request.")

        print(f"[ACM] Falling back to Playwright PDF download after direct failure: {direct_error}")
        
        print(f"Fetching PDF via secure Playwright context: {pdf_url}")

        try:
            body = await self._context_request_bytes(
                pdf_url,
                referer=detail_url,
                accept="application/pdf,application/octet-stream,*/*",
            )
            if body.startswith(b"%PDF"):
                with open(file_path, "wb") as f:
                    f.write(body)
                return file_path
            stripped = body.lstrip()[:512].lower()
            if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html"):
                return (
                    "Error: ACM PDF unavailable or not exposed for this article; "
                    "the PDF endpoint returned the article HTML page instead of a PDF."
                )
            print("ACM context request PDF failed: payload is not a PDF")
        except Exception as e:
            print(f"ACM context request PDF failed: {e}")

        try:
            body = await self._curl_cffi_pdf_get(pdf_url, referer=detail_url)
            if body.startswith(b"%PDF"):
                with open(file_path, "wb") as f:
                    f.write(body)
                print(f"[ACM] Downloaded PDF via curl_cffi direct HTTP: {file_path}")
                return file_path
        except Exception as e:
            print(f"ACM curl_cffi PDF fetch failed: {e}")

        probe = getattr(self, "_last_acm_pdf_probe", None) or {}
        probe_head = probe.get("body_head") or b""
        probe_text = probe_head.decode("utf-8", errors="replace")
        probe_type = str(probe.get("content_type") or "").lower()
        probe_status = probe.get("status")
        if probe_status in {401, 403, 429, 503} and self._is_blocked_html(probe_text, probe_status):
            try:
                if not await self._detail_exposes_pdf_link(detail_url, doi):
                    return (
                        "Error: ACM PDF unavailable or not exposed for this article; "
                        "the detail page does not expose a PDF link."
                    )
            except Exception as e:
                print(f"ACM detail PDF-link probe failed: {e}")
            return (
                "Error: ACM PDF endpoint is blocked by Cloudflare verification "
                f"(HTTP {probe_status}). Re-run warmup_platform_auth for ACM."
            )
        if "text/html" in probe_type or probe_head.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            return (
                "Error: ACM PDF unavailable or not exposed for this article; "
                "the PDF endpoint returned HTML instead of a PDF."
            )
        if probe_status == 404:
            return "Error: ACM PDF unavailable or not found for this article."

        try:
            body = await self._page_fetch_bytes(
                pdf_url,
                accept="application/pdf,application/octet-stream,*/*",
            )
            if not body.startswith(b"%PDF"):
                raise RuntimeError("payload is not a PDF")
            with open(file_path, "wb") as f:
                f.write(body)
            print(f"[ACM] Downloaded PDF via verified page fetch: {file_path}")
        except Exception as e:
            return f"Error downloading ACM PDF: {e}"

        try:
            with open(file_path, "rb") as f:
                if f.read(4) != b"%PDF":
                    return f"Error downloading ACM PDF: payload is not a valid PDF: {file_path}"
        except Exception as e:
            return f"Error validating ACM PDF: {e}"
            
        return file_path

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not pdf_path or not os.path.exists(pdf_path) or "Error" in pdf_path:
            return f"Failed to download PDF: {pdf_path}"
            
        if not pdf_path.lower().endswith(".pdf"):
            return f"Downloaded file is not a PDF, conversion not supported. Saved at: {pdf_path}"
            
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
        """ACM Digital Library serves RIS via /action/downloadCitation."""
        from scraper_utils import normalize_acm_doi
        doi = normalize_acm_doi(detail_url)
        if not doi:
            return ""
        url = (
            "https://dl.acm.org/action/downloadCitation"
            f"?doi={urllib.parse.quote(doi, safe='')}"
            "&format=ris&include=abs&direct=true"
        )
        direct_error = None
        try:
            body = await self._direct_get_bytes(
                url,
                referer=detail_url,
                accept="application/x-research-info-systems,*/*",
            )
            text = body.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
            if "TY  - " in text and "ER" in text:
                return text
            raise RuntimeError("payload is not RIS")
        except Exception as e:
            direct_error = e
            print(f"[ACM] Pure direct HTTP RIS failed ({e}); acquiring browser verification cookies.")

        await self._ensure_browser()
        try:
            body = await self._direct_get_bytes(
                url,
                referer=detail_url,
                accept="application/x-research-info-systems,*/*",
            )
            text = body.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
            if "TY  - " in text and "ER" in text:
                return text
            raise RuntimeError("payload is not RIS")
        except Exception as e:
            print(f"[ACM] Verified-cookie direct HTTP RIS failed ({e}); falling back to Playwright request.")

        print(f"[ACM] Falling back to Playwright RIS fetch after direct failure: {direct_error}")
        try:
            body_bytes = await self._context_request_bytes(
                url,
                referer=detail_url,
                accept="application/x-research-info-systems,*/*",
            )
            body = body_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
            if "TY  - " in body and "ER" in body:
                return body
        except Exception as e:
            print(f"[ACM] fetch_ris failed: {e}")

        try:
            body_bytes = await self._page_fetch_bytes(
                url,
                accept="application/x-research-info-systems,*/*",
            )
            body = body_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
            if "TY  - " in body and "ER" in body:
                return body
        except Exception as e:
            print(f"[ACM] page_fetch_ris failed: {e}")
        return ""


scraper_instance = ACMScraper()
