from __future__ import annotations

import asyncio
import os
import re
import sqlite3
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


class AIAAScraper:
    BASE_URL = "https://arc.aiaa.org"
    PROFILE_BASE = ".aiaa_profile"
    # Camoufox in this workspace is Firefox 135. AIAA's cf_clearance is
    # fingerprint-bound: chrome136/firefox133/firefox147 all returned CF 403,
    # while firefox135 plus the browser-issued cookie returned search/details/PDF.
    DIRECT_IMPERSONATE = "firefox135"
    DIRECT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"

    def __init__(self):
        self.context = None
        self.page = None
        self.camoufox_cm = None
        self.allow_headful_fallback = allow_headful_fallback_for("AIAA")
        self.manual_verification_timeout = manual_verification_timeout_seconds()
        self._pdf_cache: dict[str, str] = {}

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _is_cloudflare_title(title: str) -> bool:
        text = (title or "").lower()
        return any(
            marker in text
            for marker in (
                "just a moment",
                "cloudflare",
                "checking your browser",
                "attention required",
            )
        )

    @staticmethod
    def _is_blocked_html(html: str, status: int = None) -> bool:
        text = (html or "").lower()
        return any(
            marker in text
            for marker in (
                "<title>just a moment",
                "cf-browser-verification",
                "challenges.cloudflare.com",
                "turnstile",
                "verify you are human",
                "checking if the site connection is secure",
            )
        )

    @staticmethod
    def _extract_doi(url_or_text: str) -> str:
        if not url_or_text:
            return ""
        decoded = urllib.parse.unquote(str(url_or_text))
        match = re.search(r"(10\.2514/[^?#\s\"'<>]+)", decoded, re.IGNORECASE)
        return match.group(1).rstrip(".") if match else ""

    @staticmethod
    def _safe_doi_stem(doi: str) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", doi or "").strip("._")
        return stem or "aiaa_paper"

    @staticmethod
    def _extract_year(text: str) -> int | None:
        match = re.search(r"\b(?:19|20)\d{2}\b", str(text or ""))
        return int(match.group(0)) if match else None

    @classmethod
    def _year_allowed(cls, text: str, start_year: int = None, end_year: int = None) -> bool:
        if start_year is None and end_year is None:
            return True
        year = cls._extract_year(text)
        if year is None:
            return False
        if start_year is not None and year < int(start_year):
            return False
        if end_year is not None and year > int(end_year):
            return False
        return True

    @staticmethod
    def _source_type_allowed(doi: str, source: str, source_type: str = "all") -> bool:
        wanted = str(source_type or "").strip().lower()
        if wanted in {"", "all"}:
            return True
        source_low = (source or "").lower()
        if wanted in {"conference", "conferences", "proceedings", "meeting"}:
            return doi.startswith("10.2514/6.") or any(
                token in source_low
                for token in ("conference", "forum", "meeting", "symposium", "exposition", "congress")
            )
        if wanted in {"journal", "journals", "article", "research-article"}:
            return doi.startswith("10.2514/1.") or "journal" in source_low
        if wanted in {"book", "chapter"}:
            return "book" in source_low or "chapter" in source_low
        return wanted in source_low

    @staticmethod
    def _cookie_matches(domain: str, path: str, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        req_path = parsed.path or "/"
        clean_domain = (domain or "").lstrip(".").lower()
        if clean_domain and not (host == clean_domain or host.endswith("." + clean_domain)):
            return False
        if path and not req_path.startswith(path):
            return False
        return True

    def _shared_cookie_dict_for_url(self, url: str) -> dict:
        out = {}
        try:
            from library import get_library

            lib = get_library()
            if not lib.enabled:
                return out
            state = lib.get_browser_state("AIAA") or {}
            now = int(time.time())
            for cookie in state.get("cookies") or []:
                if cookie.get("expires") and cookie.get("expires") > 0 and cookie.get("expires") < now:
                    continue
                if self._cookie_matches(cookie.get("domain", ""), cookie.get("path", "/"), url):
                    out[cookie.get("name")] = cookie.get("value", "")
        except Exception as e:
            print(f"[AIAA] Could not read shared browser cookies: {e}")
        return {k: v for k, v in out.items() if k}

    def _profile_cookie_dict_for_url(self, url: str) -> dict:
        out = {}
        db_path = Path(profile_path(self.PROFILE_BASE)) / "cookies.sqlite"
        if not db_path.exists():
            return out
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro&immutable=1", uri=True)
            try:
                rows = conn.execute(
                    "SELECT host, path, name, value, expiry FROM moz_cookies"
                ).fetchall()
            finally:
                conn.close()
            now = int(time.time())
            for domain, path, name, value, expiry in rows:
                if expiry and int(expiry) < now:
                    continue
                if self._cookie_matches(domain, path or "/", url):
                    out[name] = value
        except Exception as e:
            print(f"[AIAA] Could not read profile cookies for direct request: {e}")
        return out

    def _cookie_dict_for_url(self, url: str) -> dict:
        cookies = {}
        cookies.update(self._shared_cookie_dict_for_url(url))
        cookies.update(self._profile_cookie_dict_for_url(url))
        return cookies

    def _headers(self, *, referer: str = None, accept: str = None, xhr: bool = False) -> dict:
        headers = {
            "user-agent": self.DIRECT_UA,
            "accept": accept or "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
            "upgrade-insecure-requests": "1",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
        }
        if referer:
            headers["referer"] = referer
        if xhr:
            headers["x-requested-with"] = "XMLHttpRequest"
            headers["sec-fetch-dest"] = "empty"
            headers["sec-fetch-mode"] = "cors"
            headers.pop("sec-fetch-user", None)
            headers.pop("upgrade-insecure-requests", None)
        return headers

    async def _curl_request(
        self,
        method: str,
        url: str,
        *,
        referer: str = None,
        accept: str = None,
        data: dict = None,
        xhr: bool = False,
    ):
        from curl_cffi import requests as curl_requests

        def _run():
            headers = self._headers(referer=referer, accept=accept, xhr=xhr)
            cookies = self._cookie_dict_for_url(url)
            if method.upper() == "POST":
                headers["content-type"] = "application/x-www-form-urlencoded"
                return curl_requests.post(
                    url,
                    headers=headers,
                    cookies=cookies,
                    data=data or {},
                    timeout=60,
                    impersonate=self.DIRECT_IMPERSONATE,
                    allow_redirects=True,
                )
            return curl_requests.get(
                url,
                headers=headers,
                cookies=cookies,
                timeout=60,
                impersonate=self.DIRECT_IMPERSONATE,
                allow_redirects=True,
            )

        return await asyncio.to_thread(_run)

    async def _fetch_html_direct(self, url: str, *, referer: str = None, xhr: bool = False) -> str:
        response = await self._curl_request("GET", url, referer=referer, xhr=xhr)
        text = response.text or ""
        if self._is_blocked_html(text, response.status_code):
            raise RuntimeError(
                "AIAA Cloudflare verification blocked direct HTTP. Run warmup_platform_auth(platforms=\"AIAA\") "
                "or complete verification in the AIAA profile, then retry."
            )
        if response.status_code >= 400:
            raise RuntimeError(f"AIAA direct HTTP failed: HTTP {response.status_code}")
        return text

    async def _fetch_html_request_first(self, url: str, *, referer: str = None, xhr: bool = False) -> str:
        try:
            return await self._fetch_html_direct(url, referer=referer, xhr=xhr)
        except Exception as first_error:
            if not self.allow_headful_fallback:
                raise
            print(f"[AIAA] Direct HTTP needs verification ({first_error}); warming browser auth.")
            await self.warmup_auth()
            return await self._fetch_html_direct(url, referer=referer, xhr=xhr)

    async def _fetch_pdf_direct(self, url: str, *, referer: str = None) -> bytes:
        response = await self._curl_request(
            "GET",
            url,
            referer=referer,
            accept="application/pdf,application/octet-stream;q=0.9,text/html;q=0.8,*/*;q=0.7",
        )
        content = response.content or b""
        if content.startswith(b"%PDF"):
            return content
        text = ""
        try:
            text = response.text or ""
        except Exception:
            pass
        if self._is_blocked_html(text, response.status_code):
            raise RuntimeError(
                "AIAA Cloudflare verification blocked direct PDF request. Run warmup_platform_auth(platforms=\"AIAA\")."
            )
        if any(marker in (text or "").lower() for marker in ("purchase", "get access", "login", "institution")):
            raise RuntimeError("AIAA served an access page instead of PDF; the item may require entitlement.")
        raise RuntimeError(
            f"AIAA PDF endpoint did not return a PDF: HTTP {response.status_code}, "
            f"content-type={response.headers.get('content-type', '')}"
        )

    async def _ensure_browser(self, force_headful: bool = False):
        if self.context:
            return
        print(f"Initializing AIAA Persistent Browser Context (Headless: {not force_headful})...")
        from camoufox.async_api import AsyncCamoufox
        from scraper_utils import apply_browser_cookies, load_or_create_fingerprint, pooled_profile

        profile_dir, self._profile_ephemeral = pooled_profile(self.PROFILE_BASE, "AIAA")
        self._profile_dir = profile_dir
        for lock_name in ("lockfile", "SingletonLock", "parent.lock", ".parentlock"):
            lock_path = os.path.join(profile_dir, lock_name)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass

        fp = load_or_create_fingerprint("AIAA")
        kwargs = {
            "headless": not force_headful,
            "user_data_dir": profile_dir,
            "persistent_context": True,
            "os": "windows",
            "humanize": True,
            "geoip": True,
            "accept_downloads": True,
        }
        if fp is not None:
            kwargs["fingerprint"] = fp
            kwargs["i_know_what_im_doing"] = True

        self.camoufox_cm = AsyncCamoufox(**kwargs)
        self.context = await self.camoufox_cm.__aenter__()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await apply_browser_cookies(self.context, "AIAA")
        await self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=60000)

        blocked = True
        for _ in range(15):
            try:
                title = await self.page.title()
                html = await self.page.content()
                if not self._is_cloudflare_title(title) and not self._is_blocked_html(html):
                    blocked = False
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        if blocked and force_headful:
            print(
                f">>> PLEASE SOLVE AIAA CLOUDFLARE IN THE CAMOUFOX WINDOW. "
                f"Waiting up to {self.manual_verification_timeout}s... <<<"
            )
            for _ in range(self.manual_verification_timeout):
                try:
                    title = await self.page.title()
                    html = await self.page.content()
                    if not self._is_cloudflare_title(title) and not self._is_blocked_html(html):
                        blocked = False
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)

        if blocked and not force_headful:
            if not self.allow_headful_fallback:
                print("[AIAA] Cloudflare blocked headless warmup; headful fallback is disabled.")
            else:
                await self.close()
                await self._ensure_browser(force_headful=True)
                return

        if not blocked:
            try:
                from scraper_utils import capture_browser_cookies

                await capture_browser_cookies(
                    self.context,
                    "AIAA",
                    note="manual warmup verified" if force_headful else "session cookies",
                )
            except Exception:
                pass
        print("AIAA context initialized successfully.")

    async def warmup_auth(self, timeout_seconds: int = None) -> Dict:
        if timeout_seconds is not None:
            self.manual_verification_timeout = max(30, int(timeout_seconds))
        self.allow_headful_fallback = True
        if self.context:
            await self.close()
        started = time.perf_counter()
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
                "AIAA",
                note="manual warmup verified" if ok else "manual warmup attempted",
            )
        except Exception:
            pass
        return {
            "platform": "AIAA",
            "ok": ok,
            "seconds": round(time.perf_counter() - started, 3),
            "title": title,
            "url": self.page.url if self.page else "",
        }

    async def close(self):
        if self.context:
            try:
                from scraper_utils import capture_browser_cookies

                await capture_browser_cookies(self.context, "AIAA")
            except Exception:
                pass
        if self.camoufox_cm:
            try:
                await self.camoufox_cm.__aexit__(None, None, None)
            except Exception as e:
                print(f"AIAA context close failed: {e}")
            self.camoufox_cm = None
            self.context = None
            self.page = None
        elif self.context:
            await self.context.close()
            self.context = None
            self.page = None
        if getattr(self, "_profile_dir", None):
            from scraper_utils import cleanup_pooled_profile

            cleanup_pooled_profile(self._profile_dir, getattr(self, "_profile_ephemeral", False))
            self._profile_dir = None

    def _parse_search_html(
        self,
        html: str,
        *,
        limit: int,
        offset_in_first_page: int,
        source_type: str = "all",
        journal: str = None,
        start_year: int = None,
        end_year: int = None,
    ) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        total = "0"
        count_node = soup.select_one(".result__count, .search-result__title")
        if count_node:
            match = re.search(r"[\d,]+", count_node.get_text(" ", strip=True))
            if match:
                total = match.group(0).replace(",", "")

        papers: List[Dict] = []
        journal_clean = (journal or "").strip().lower()
        for item in soup.select("li.search-item"):
            doi = ""
            doi_input = item.select_one('input[name="doi"]')
            if doi_input and doi_input.get("value"):
                doi = doi_input["value"].strip()

            title_link = item.select_one("span.hlFld-Title h3.search-item__title a, h3.search-item__title a")
            if not title_link:
                continue
            title = self._clean_text(title_link.get_text(" ", strip=True))
            detail_link = urllib.parse.urljoin(self.BASE_URL, title_link.get("href") or "")
            if not doi:
                doi = self._extract_doi(detail_link)

            authors = [
                self._clean_text(node.get_text(" ", strip=True))
                for node in item.select(".meta__authors span.hlFld-ContribAuthor")
            ]
            authors = [a for a in authors if a]
            date = self._clean_text(" ".join(node.get_text(" ", strip=True) for node in item.select(".meta__details span")))
            serial = item.select_one("a.meta__serial")
            volume = item.select_one("a.meta__volume")
            source_parts = []
            if serial:
                source_parts.append(self._clean_text(serial.get_text(" ", strip=True)))
            if volume:
                source_parts.append(self._clean_text(volume.get_text(" ", strip=True)))
            source = ", ".join([p for p in source_parts if p])
            abstract_node = item.select_one(".hlFld-Abstract")
            abstract_snippet = self._clean_text(abstract_node.get_text(" ", strip=True)) if abstract_node else ""

            if journal_clean and journal_clean not in source.lower():
                continue
            if not self._year_allowed(date, start_year=start_year, end_year=end_year):
                continue
            if not self._source_type_allowed(doi, source, source_type=source_type):
                continue

            papers.append(
                {
                    "id": doi or detail_link,
                    "title": title,
                    "author": ", ".join(authors),
                    "authors": authors,
                    "source": source,
                    "venue_name": source,
                    "date": date,
                    "pub_date": date,
                    "abstract": abstract_snippet,
                    "doi": doi,
                    "detail_link": detail_link,
                    "pdf_url": urllib.parse.urljoin(self.BASE_URL, f"/doi/pdf/{doi}") if doi else "",
                    "db_type": "AIAA",
                }
            )

        if offset_in_first_page:
            papers = papers[offset_in_first_page:]
        return {"total_results": total, "papers": papers[: max(0, int(limit or 10))]}

    def _parse_detail_html(self, detail_url: str, html: str) -> Dict[str, object]:
        soup = BeautifulSoup(html, "html.parser")
        doi = self._extract_doi(detail_url)
        doi_meta = soup.find("meta", attrs={"name": "dc.Identifier"}) or soup.find("meta", attrs={"name": "publication_doi"})
        if doi_meta and doi_meta.get("content"):
            doi = self._extract_doi(doi_meta["content"]) or doi

        title = ""
        h1 = soup.select_one("h1.citation__title, h1")
        if h1:
            title = self._clean_text(h1.get_text(" ", strip=True))
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if not title and og_title and og_title.get("content"):
            title = self._clean_text(og_title["content"].split("|", 1)[0])

        abstract = ""
        for selector in (".NLM_abstract", ".hlFld-Abstract", 'div[class*="abstract"]'):
            node = soup.select_one(selector)
            if node:
                abstract = self._clean_text(node.get_text(" ", strip=True))
                abstract = re.sub(r"^Abstract:\s*", "", abstract, flags=re.IGNORECASE)
                break
        if not abstract:
            og_desc = soup.find("meta", attrs={"property": "og:description"})
            if og_desc and og_desc.get("content"):
                abstract = self._clean_text(og_desc["content"])

        authors = []
        for node in soup.select("ul.loa li a span:first-child"):
            name = self._clean_text(node.get_text(" ", strip=True))
            if name and name not in authors:
                authors.append(name)

        date = ""
        date_node = soup.select_one(".epub-section__date")
        if date_node:
            date = self._clean_text(date_node.get_text(" ", strip=True))

        keywords = []
        for node in soup.select('a[href*="keyword"], .article__keywords a, .hlFld-Keyword'):
            text = self._clean_text(node.get_text(" ", strip=True))
            if text and text.lower() not in {"keywords", "keyword"} and text not in keywords:
                keywords.append(text)

        pdf_link = soup.select_one('a[href*="/doi/pdf/"]')
        pdf_url = urllib.parse.urljoin(self.BASE_URL, pdf_link.get("href")) if pdf_link else ""
        if not pdf_url and doi:
            pdf_url = urllib.parse.urljoin(self.BASE_URL, f"/doi/pdf/{doi}")

        return {
            "url": detail_url,
            "title": title,
            "authors": authors,
            "pub_date": date,
            "abstract": abstract or "No abstract found",
            "keywords": keywords,
            "doi": doi,
            "pdf_url": pdf_url,
        }

    async def search_papers(
        self,
        query: str,
        search_field: str = "AllField",
        db_scope: str = "",
        source_type: str = "all",
        journal: str = None,
        start_year: int = None,
        end_year: int = None,
        sort_by: str = "relevance",
        start_index: int = 0,
        limit: int = 10,
    ) -> Dict:
        page_size = max(20, min(100, int(limit or 10) + int(start_index or 0)))
        start_page = int(start_index or 0) // page_size
        offset_in_first_page = int(start_index or 0) % page_size

        field_map = {
            "all": "AllField",
            "All": "AllField",
            "AllField": "AllField",
            "title": "Title",
            "Title": "Title",
            "abstract": "Abstract",
            "Abstract": "Abstract",
            "author": "Contrib",
            "authors": "Contrib",
            "Author": "Contrib",
            "Authors": "Contrib",
            "keyword": "Keyword",
            "keywords": "Keyword",
            "DOI": "AllField",
        }
        field = field_map.get(str(search_field or "AllField"), "AllField")
        params = [
            (field, query),
            ("startPage", str(start_page)),
            ("pageSize", str(page_size)),
        ]
        if start_year:
            params.append(("AfterYear", str(start_year)))
        if end_year:
            params.append(("BeforeYear", str(end_year)))
        if sort_by == "date_desc":
            params.append(("sortBy", "Earliest"))
        elif sort_by == "citations":
            params.append(("sortBy", "cited"))
        elif sort_by == "downloads":
            params.append(("sortBy", "downloaded"))
        if journal:
            params.append(("PubName", str(journal).strip()))

        search_url = f"{self.BASE_URL}/action/doSearch?{urllib.parse.urlencode(params)}"
        print(f"[AIAA] Direct search request: {search_url}")
        html = await self._fetch_html_request_first(search_url, referer=f"{self.BASE_URL}/")
        return self._parse_search_html(
            html,
            limit=limit,
            offset_in_first_page=offset_in_first_page,
            source_type=source_type,
            journal=journal,
            start_year=start_year,
            end_year=end_year,
        )

    async def get_paper_details(self, detail_url: str) -> Dict[str, object]:
        html = await self._fetch_html_request_first(detail_url, referer=f"{self.BASE_URL}/")
        return self._parse_detail_html(detail_url, html)

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        doi = self._extract_doi(detail_url)
        if not doi:
            try:
                details = await self.get_paper_details(detail_url)
                doi = str(details.get("doi") or "")
            except Exception:
                doi = ""
        if not doi:
            return "Error: Could not extract AIAA DOI from URL."

        from scraper_utils import remember_downloaded_pdf, reuse_downloaded_pdf

        safe_name = self._safe_doi_stem(doi)
        reused = reuse_downloaded_pdf(self._pdf_cache, doi, output_dir, filename=f"aiaa_{safe_name}.pdf")
        if reused:
            return reused

        pdf_url = urllib.parse.urljoin(self.BASE_URL, f"/doi/pdf/{doi}")
        try:
            pdf_bytes = await self._fetch_pdf_direct(pdf_url, referer=detail_url)
        except Exception as first_error:
            if not self.allow_headful_fallback:
                return f"Error: {first_error}"
            print(f"[AIAA] Direct PDF request needs verification ({first_error}); warming browser auth.")
            await self.warmup_auth()
            pdf_bytes = await self._fetch_pdf_direct(pdf_url, referer=detail_url)

        os.makedirs(output_dir, exist_ok=True)
        path = os.path.abspath(os.path.join(output_dir, f"aiaa_{safe_name}.pdf"))
        with open(path, "wb") as fh:
            fh.write(pdf_bytes)
        remember_downloaded_pdf(self._pdf_cache, doi, path)
        print(f"[AIAA] Downloaded PDF via direct HTTP: {path}")
        return path

    async def read_paper_content(self, detail_url: str, output_dir: str):
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not isinstance(pdf_path, str) or not os.path.exists(pdf_path):
            return pdf_path
        from pdf_utils import convert_pdf_to_markdown

        converted = await asyncio.to_thread(convert_pdf_to_markdown, pdf_path, output_dir)
        return converted.md_path, converted.preview

    async def fetch_ris(self, detail_url: str) -> str:
        doi = self._extract_doi(detail_url)
        if not doi:
            details = await self.get_paper_details(detail_url)
            doi = str(details.get("doi") or "")
        if not doi:
            return ""
        url = f"{self.BASE_URL}/action/downloadCitation"
        data = {
            "doi": doi,
            "downloadFileName": f"aiaa_{self._safe_doi_stem(doi.replace('10.2514/', ''))}",
            "include": "abs",
            "format": "ris",
            "submit": "Download article citation data",
        }
        response = await self._curl_request(
            "POST",
            url,
            referer=f"{self.BASE_URL}/action/showCitFormats?doi={urllib.parse.quote(doi, safe='')}",
            accept="text/plain,*/*;q=0.8",
            data=data,
        )
        text = response.text or ""
        if response.status_code == 200 and "TY  -" in text:
            return text
        return ""


scraper_instance = AIAAScraper()
