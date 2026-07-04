import os
import asyncio
import functools
import sys
from mcp_logging import safe_stderr_print
from typing import List, Dict, Optional
from playwright.async_api import async_playwright
import bs4
import hashlib
import urllib.parse
import re
import json
import aiohttp

print = safe_stderr_print


# Google Scholar rotates several anti-bot interstitials. The one that was
# slipping through (captured in scratch/gs_norows_*.html) is a reCAPTCHA page
# built as <form id="gs_captcha_f"><h1>Please show you're not a robot</h1>
# <div id="gs_captcha_c">...iframe...</div></form> -- it carries NONE of the
# markers the old detector looked for (form#captcha-form / div.g-recaptcha /
# "One more step"), yet still echoes a gs_ab_md result count, so the scraper
# happily parsed "total=820000, papers=[]". This matches every known variant.
_GS_CAPTCHA_SELECTOR = (
    "form#captcha-form, form#gs_captcha_f, "
    "#gs_captcha_c, #gs_captcha_ccl, "
    "div.g-recaptcha, #g-recaptcha-response, "
    "h1:-soup-contains('One more step'), "
    "h1:-soup-contains(\"not a robot\")"
)
_GS_BLOCK_TEXT = (
    "Please show you're not a robot",
    "Please show you’re not a robot",
    "our systems have detected unusual traffic",
)


def _gs_is_blocked(soup, html: str) -> bool:
    if soup.select_one(_GS_CAPTCHA_SELECTOR):
        return True
    return any(t in html for t in _GS_BLOCK_TEXT)


class GoogleScholarScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
    
    async def initialize(self, force_headful=False):
        import random
        # Clean obsolete lockfiles to prevent context launch crashing
        from scraper_utils import pooled_profile, load_or_create_fingerprint
        profile_dir, self._profile_ephemeral = pooled_profile(".gs_profile", "GS")
        self._profile_dir = profile_dir
        _shared_fp = load_or_create_fingerprint("GS")
        # Firefox-style parent.lock cleanup -- Camoufox crashes leave these behind.
        for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
            lfile = os.path.join(profile_dir, lock_name)
            if os.path.exists(lfile):
                try: os.remove(lfile)
                except: pass

        self.playwright = None # Will not be used anymore
        from camoufox.async_api import AsyncCamoufox
        
        # Google Scholar aggressively shadow-blocks bots; do not block images here.
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
        from scraper_utils import apply_browser_cookies
        await apply_browser_cookies(self.context, "GS")

    async def close(self):
        if getattr(self, "context", None):
            try:
                from scraper_utils import capture_browser_cookies
                await capture_browser_cookies(self.context, "GS")
            except Exception:
                pass
        if self.page:
            await self.page.close()
            self.page = None
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            await self.camoufox_cm.__aexit__(None, None, None)
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None
        if getattr(self, "_profile_dir", None):
            from scraper_utils import cleanup_pooled_profile
            cleanup_pooled_profile(self._profile_dir, getattr(self, "_profile_ephemeral", False))
            self._profile_dir = None

    def _build_search_url(self, query: str, journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0) -> str:
        # start_index maps directly to GS's `start` param (0, 10, 20...).
        params = {"hl": "en", "start": str(start_index)}
        if journal:
            params["as_q"] = query
            params["as_publication"] = journal
        else:
            params["q"] = query
        if sort_by == "date_desc":
            params["scisbd"] = "1"
        if start_year:
            params["as_ylo"] = str(start_year)
        if end_year:
            params["as_yhi"] = str(end_year)
        return "https://scholar.google.com/scholar?" + urllib.parse.urlencode(params)

    def _default_headers(self, referer: str = "https://scholar.google.com/") -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": referer,
        }

    async def _fetch_search_html_direct(self, url: str) -> str:
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=25)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.get(url, headers=self._default_headers(), ssl=False, allow_redirects=True) as resp:
                html = await resp.text(errors="ignore")
                if resp.status not in (200, 203):
                    raise RuntimeError(f"GS direct GET returned HTTP {resp.status}")
                return html

    def _blocked_result(self, kind: str) -> Dict:
        if kind == "captcha":
            return {
                "total_results": "CAPTCHA (403)",
                "papers": [{
                    "id": "errCaptcha",
                    "title": "Google Scholar requested CAPTCHA but it was not solved.",
                    "author": "System",
                    "source": "GS Blocked",
                    "date": "N/A",
                    "db_type": "Error",
                    "detail_link": "N/A",
                }],
            }
        return {
            "total_results": "IP_BANNED",
            "papers": [{
                "id": "errIpBan",
                "title": "Google Scholar has HARD-BANNED this IP address. Please change your VPN/Proxy node or wait 24 hours.",
                "author": "System",
                "source": "GS Blocked",
                "date": "N/A",
                "db_type": "Error",
                "detail_link": "N/A",
            }],
        }

    @staticmethod
    def _parse_cited_by(row) -> Optional[int]:
        """The "Cited by N" link in the result footer (div.gs_fl)."""
        for a in row.select("div.gs_fl a, div.gs_flb a"):
            m = re.match(r"\s*Cited by\s+([\d,]+)", a.get_text(" ", strip=True), re.I)
            if m:
                return int(m.group(1).replace(",", ""))
        return None

    @staticmethod
    def _parse_pdf_link(row) -> tuple:
        """The free [PDF]/[HTML] full-text link GS shows in div.gs_ggs.

        Returns (url, label) e.g. ("https://arxiv.org/pdf/2108.09084", "PDF").
        """
        a = row.select_one("div.gs_ggs a")
        href = a.get("href", "") if a else ""
        if not href:
            return "", ""
        ctg = a.select_one(".gs_ctg2")
        label = (ctg.get_text(strip=True) if ctg else "").strip("[] ").upper() or "PDF"
        return href, label

    @classmethod
    def _parse_result_row(cls, row, journal: str = None):
        """Parse one GS result row -> (paper_dict, dedupe_key).

        Shared by the direct-HTTP and browser code paths so they can never
        drift. Returns (None, None) for non-result rows or ones filtered out
        by the ``journal`` constraint.
        """
        title_elem = row.select_one("h3.gs_rt a") or row.select_one("h3.gs_rt")
        if not title_elem:
            return None, None
        title = title_elem.get_text(strip=True)
        detail_link = title_elem.get("href", "") if title_elem.name == "a" else ""
        gs_cluster_id = row.get("data-cid") or ""
        if not gs_cluster_id:
            parent = row.find_parent("div", class_="gs_r")
            if parent is not None:
                gs_cluster_id = parent.get("data-cid") or ""

        author_pub_elem = row.select_one("div.gs_a")
        author_pub_str = author_pub_elem.get_text().replace('\xa0', ' ').strip() if author_pub_elem else "N/A"
        author, date, source, venue_name = "N/A", "N/A", "Google Scholar", ""
        # Format: "Author1, Author2 - Journal Name, 2023 - PublisherSite"
        if " - " in author_pub_str:
            parts = author_pub_str.split(" - ")
            author = parts[0]
            if len(parts) > 1:
                middle = parts[1]
                date_m = re.search(r'\b(19|20)\d{2}\b', middle)
                if date_m:
                    date = date_m.group()
                venue_name = re.sub(r',?\s*\b(19|20)\d{2}\b.*$', '', middle).strip().rstrip(',').strip()
                source = parts[-1].strip()
                if source.startswith("…") or source.startswith("..."):
                    source_guess = urllib.parse.urlparse(detail_link).netloc
                    source = source_guess or source
        else:
            author = author_pub_str

        from scraper_utils import venue_matches
        if journal and venue_name and not venue_matches(journal, venue_name):
            return None, None

        snippet_elem = row.select_one("div.gs_rs")
        snippet = snippet_elem.get_text().replace('\n', ' ').strip() if snippet_elem else ""
        pdf_url, pdf_label = cls._parse_pdf_link(row)
        cited_by = cls._parse_cited_by(row)
        uid = hashlib.md5((detail_link or gs_cluster_id or title).encode()).hexdigest()[:8]

        from scraper_utils import platform_hint_from_url
        paper = {
            "id": uid,
            "title": title,
            "author": author,
            "source": "GS: " + source,
            "venue_name": venue_name,
            "date": date,
            "db_type": "GS Aggregated",
            "detail_link": detail_link,
            "recommended_platform": platform_hint_from_url(detail_link),
            "pdf_url": pdf_url,          # free full-text link, if GS offered one
            "pdf_label": pdf_label,      # "PDF" / "HTML"
            "cited_by": cited_by,        # citation count, if present
            "_abstract": snippet,        # expose the snippet so it's cached + shown
            "_gs_snippet": snippet,
            "_gs_cluster_id": gs_cluster_id,
        }
        return paper, (detail_link or gs_cluster_id or title)

    def _parse_search_html(self, html: str, *, query: str, journal: str = None, limit: int = 10, page_url: str = "") -> Dict:
        soup = bs4.BeautifulSoup(html or "", "html.parser")
        if _gs_is_blocked(soup, html or ""):
            raise RuntimeError("GS search returned CAPTCHA")
        if "We're sorry" in (html or "") or "but your computer or network may be sending automated queries" in (html or ""):
            return self._blocked_result("ip_banned")

        total_match = soup.select_one("div#gs_ab_md")
        total_text = total_match.text if total_match else "未知"
        # Usually: "About 1,230,000 results (0.05 sec)" or "找到约 1,230,000 条结果".
        m = re.search(r'([\d,]+)\s*(?:results|条结果)', total_text)
        total_results = m.group(1).replace(',', '') if m else "未知"

        rows = soup.select("div.gs_r.gs_or.gs_scl")
        if not rows:
            rows = soup.select("div.gs_ri, div.gs_r")

        if not rows and total_results not in ("未知", "0"):
            try:
                import time as _t
                _dump_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch")
                os.makedirs(_dump_dir, exist_ok=True)
                _dump = os.path.join(_dump_dir, f"gs_norows_{int(_t.time())}.html")
                with open(_dump, "w", encoding="utf-8") as _f:
                    _f.write(f"<!-- query={query!r} total={total_results} url={page_url} -->\n")
                    _f.write(html or "")
                print(f"[GS][DIAG] total={total_results} but 0 rows; HTML dumped -> {_dump}")
            except Exception as _e:
                print(f"[GS][DIAG] dump failed: {_e}")

        results = []
        seen_links = set()
        for row in rows:
            if len(results) >= limit:
                break
            paper, dedupe_key = self._parse_result_row(row, journal)
            if paper is None or dedupe_key in seen_links:
                continue
            seen_links.add(dedupe_key)
            results.append(paper)

        return {
            "total_results": total_results,
            "papers": results,
        }

    async def _fetch_ris_direct_for_cluster(self, cluster_id: str) -> str:
        if not cluster_id:
            return ""
        cite_url = (
            f"https://scholar.google.com/scholar?q=info:{cluster_id}:scholar.google.com/"
            f"&output=cite&scirp=0&hl=en"
        )
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=25)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.get(
                cite_url,
                headers=self._default_headers(),
                ssl=False,
                allow_redirects=True,
            ) as resp:
                cite_html = await resp.text(errors="ignore")
                if resp.status != 200:
                    return ""
        cite_soup = bs4.BeautifulSoup(cite_html or "", "html.parser")
        if _gs_is_blocked(cite_soup, cite_html or ""):
            return ""
        unescaped = (cite_html or "").replace("&amp;", "&")
        match = re.search(
            r'href="(https?://[^"]*scholar\.googleusercontent\.com/scholar\.ris\?[^"]+)"',
            unescaped,
        )
        if not match:
            return ""
        ris_url = match.group(1)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.get(
                ris_url,
                headers={
                    **self._default_headers(),
                    "Accept": "application/x-research-info-systems,*/*",
                    "Referer": "https://scholar.google.com/",
                },
                ssl=False,
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return ""
                body = await resp.text(errors="ignore")
        body = (body or "").replace("\r\n", "\n").strip()
        if "TY  -" in body and "ER" in body:
            return body
        return ""

    async def search_papers(self, query: str, search_field: str = "all", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        import random
        direct_url = self._build_search_url(query, journal, start_year, end_year, sort_by, start_index)
        try:
            html = await self._fetch_search_html_direct(direct_url)
            parsed = self._parse_search_html(html, query=query, journal=journal, limit=limit, page_url=direct_url)
            if parsed.get("papers") or parsed.get("total_results") in ("0", "IP_BANNED"):
                return parsed
            print("[GS] Direct search returned no rows; falling back to browser search.")
        except Exception as e:
            print(f"[GS] Direct search failed ({e}); falling back to browser search.")

        if not self.page:
            await self.initialize()

        base_url = direct_url

        # Human-like delay strategy to avoid fast CAPTCHA blocks
        await asyncio.sleep(random.uniform(2.5, 6.0))

        try:
            await self.page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            # Simulate human reading / scrolling
            for _ in range(random.randint(2, 4)):
                await self.page.mouse.wheel(0, random.randint(200, 600))
                await asyncio.sleep(random.uniform(0.5, 1.5))
                
        except Exception as e:
            print(f"Error navigating to Google Scholar: {e}")
            return {"total_results": "0", "papers": []}
            
        html = await self.page.content()
        soup = bs4.BeautifulSoup(html, 'html.parser')
        
        # Check for captcha
        if _gs_is_blocked(soup, html):
            # We assume headless if no explicit tracking is used
            if not hasattr(self, 'is_headful'):
                print("[Anti-Bot] GS CAPTCHA detected! Relaunching headful for manual check...")
                await self.close()
                self.is_headful = True
                await self.initialize(force_headful=True)
                return await self.search_papers(query, search_field, db_scope, source_type, journal, start_year, end_year, sort_by, start_index, limit)
            else:
                print(">>> PLEASE SOLVE GS CAPTCHA IN BROWSER WINDOW. Waiting up to 60s... <<<")
                solved = False
                for _ in range(60):
                    html = await self.page.content()
                    soup = bs4.BeautifulSoup(html, 'html.parser')
                    if not _gs_is_blocked(soup, html):
                        solved = True
                        break
                    await asyncio.sleep(1)
                
                if not solved:
                    return {
                        "total_results": "CAPTCHA (403)",
                        "papers": [{
                            "id": "errCaptcha",
                            "title": "Google Scholar requested CAPTCHA but it was not solved.",
                            "author": "System",
                            "source": "GS Blocked",
                            "date": "N/A",
                            "db_type": "Error",
                            "detail_link": "N/A"
                        }]
                    }
                else: # refresh soup after solve
                    html = await self.page.content()
                    soup = bs4.BeautifulSoup(html, 'html.parser')
            
        if "We're sorry" in html or "but your computer or network may be sending automated queries" in html:
            return {
                "total_results": "IP_BANNED",
                "papers": [{
                    "id": "errIpBan",
                    "title": "Google Scholar has HARD-BANNED this IP address. Please change your VPN/Proxy node or wait 24 hours.",
                    "author": "System",
                    "source": "GS Blocked",
                    "date": "N/A",
                    "db_type": "Error",
                    "detail_link": "N/A"
                }]
            }

        total_match = soup.select_one("div#gs_ab_md")
        total_text = total_match.text if total_match else "未知"
        # usually looks like "About 1,230,000 results (0.05 sec)" or "找到约 1,230,000 条结果"
        m = re.search(r'([\d,]+)\s*(?:results|条结果)', total_text)
        total_results = m.group(1).replace(',', '') if m else "未知"
        
        results = []
        rows = soup.select("div.gs_ri, div.gs_r")

        # DIAGNOSTIC: GS sometimes returns a page where the result count
        # parses fine but zero result rows are present (a soft-block variant
        # that doesn't match our CAPTCHA selectors). Dump the exact HTML our
        # browser received so we can build a precise detector. Best-effort;
        # never let instrumentation break the scrape.
        if not rows and total_results not in ("未知", "0"):
            try:
                import time as _t
                _dump_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch")
                os.makedirs(_dump_dir, exist_ok=True)
                _dump = os.path.join(_dump_dir, f"gs_norows_{int(_t.time())}.html")
                with open(_dump, "w", encoding="utf-8") as _f:
                    _f.write(f"<!-- query={query!r} total={total_results} url={self.page.url} -->\n")
                    _f.write(html)
                print(f"[GS][DIAG] total={total_results} but 0 rows; HTML dumped -> {_dump}")
            except Exception as _e:
                print(f"[GS][DIAG] dump failed: {_e}")

        collected = 0
        seen_links = set()

        for row in rows:
            if collected >= limit:
                break
            paper, dedupe_key = self._parse_result_row(row, journal)
            if paper is None or dedupe_key in seen_links:
                continue
            seen_links.add(dedupe_key)
            results.append(paper)
            collected += 1

        return {
            "total_results": total_results,
            "papers": results
        }

    def _gs_native_id(self, detail_url: str) -> str:
        return hashlib.sha1((detail_url or "").encode("utf-8")).hexdigest()[:24]

    def _gs_cached_extra(self, detail_url: str) -> dict:
        """Stored `extra` (gs_cluster_id / gs_pdf_url) for a GS paper, or {}.

        Requires the library to be enabled and the paper to have been captured
        by a prior search_papers call.
        """
        if not detail_url or detail_url == "N/A":
            return {}
        from library import get_library
        lib = get_library()
        if not lib.enabled:
            return {}
        row = lib.get_paper("GS", self._gs_native_id(detail_url))
        if not row:
            return {}
        extra = row.get("extra")
        if isinstance(extra, (bytes, bytearray)):
            extra = extra.decode("utf-8", errors="replace")
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        return extra if isinstance(extra, dict) else {}

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        # GS is a router to the publisher and has no GS-hosted abstract page.
        # Surface the snippet captured at search time (cached as `abstract`)
        # plus any free PDF link; otherwise point at the native platform.
        abstract, pdf_url = "", ""
        from library import get_library
        lib = get_library()
        if lib.enabled and detail_url and detail_url != "N/A":
            row = lib.get_paper("GS", self._gs_native_id(detail_url))
            if row:
                abstract = (row.get("abstract") or "").strip()
                extra = row.get("extra")
                if isinstance(extra, (bytes, bytearray)):
                    extra = extra.decode("utf-8", errors="replace")
                if isinstance(extra, str):
                    try:
                        extra = json.loads(extra)
                    except Exception:
                        extra = {}
                if isinstance(extra, dict):
                    pdf_url = extra.get("gs_pdf_url") or ""
        return {
            "url": detail_url,
            "abstract": abstract or "Google Scholar is a router (no abstract page). Run search_papers first to capture the snippet, or open the source / PDF link.",
            "keywords": ["GoogleScholar"],
            "doi": "",
            "pdf_url": pdf_url,
            "note": "GS aggregates results. If pdf_url is present it is a free full text; otherwise use the native platform scraper on the source URL.",
        }

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        """Download GS's free [PDF] full-text link (arXiv / proceedings / OA
        journals) when a prior search captured one; else point at the native
        platform. Returns the saved file path or an explanatory message."""
        pdf_url = self._gs_cached_extra(detail_url).get("gs_pdf_url") or ""
        if not pdf_url:
            return ("No free PDF link was captured for this GS result "
                    "(run search_papers first; note not every result has one). "
                    "Use read_paper_content with the native platform (IEEE, SD, ...) on the source URL.")
        os.makedirs(output_dir, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=90, connect=15, sock_read=60)
        headers = {**self._default_headers(), "Accept": "application/pdf,*/*"}
        try:
            async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as session:
                async with session.get(pdf_url, headers=headers, ssl=False, allow_redirects=True) as resp:
                    if resp.status != 200:
                        return f"GS free-PDF fetch returned HTTP {resp.status}: {pdf_url}"
                    data = await resp.read()
        except Exception as e:
            return f"GS free-PDF download failed ({e}): {pdf_url}"
        if not data[:5].startswith(b"%PDF"):
            return f"The GS free link was not a direct PDF (likely an HTML landing page): {pdf_url}"
        path = os.path.join(output_dir, f"gs_{self._gs_native_id(detail_url)}.pdf")
        try:
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:
            return f"GS PDF save failed: {e}"
        return path

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not (isinstance(pdf_path, str) and os.path.exists(pdf_path)):
            return pdf_path  # explanatory message from download_paper
        try:
            import pymupdf4llm
            images_dir = os.path.join(output_dir, "images")
            os.makedirs(images_dir, exist_ok=True)
            md_text = pymupdf4llm.to_markdown(doc=pdf_path, write_images=True, image_path=images_dir)
            md_path = os.path.splitext(pdf_path)[0] + ".md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md_text)
            preview = (md_text or "")[:1000]
            return f"Markdown generation complete. Saved to: {md_path}\n\nPreview:\n{preview}..."
        except Exception as e:
            return f"Downloaded GS PDF to {pdf_path} but markdown conversion failed: {e}"

    async def fetch_ris(self, detail_url: str) -> str:
        """GS native RIS via the per-cluster cite popup.

        Precondition: the paper was found via a previous GS search that
        stored its `gs_cluster_id` in papers.extra. We look it up, ask GS
        for the cite popup -- a small HTML fragment listing download links
        for BibTeX / EndNote / RefMan(.ris) / RefWorks -- and follow the
        .ris link. The cite popup is fetched in-page (same-origin to
        scholar.google.com so any session / Turnstile cookies carry); the
        .ris file lives on scholar.googleusercontent.com which is
        cross-origin, so that step uses context.request.get instead of an
        in-page fetch (page-context CORS would reject it). Returns "" on
        any failure; the server falls back to ris_utils.synthesize_ris.
        """
        import hashlib as _hashlib
        if not detail_url or detail_url == "N/A":
            return ""
        # Native ID for GS rows is sha1(detail_link)[:24] -- see library._gs_native_id.
        native_id = _hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:24]
        from library import get_library
        lib = get_library()
        if not lib.enabled:
            return ""
        row = lib.get_paper("GS", native_id)
        if not row:
            return ""
        extra_raw = row.get("extra") or ""
        cluster_id = ""
        if isinstance(extra_raw, (bytes, bytearray)):
            extra_raw = extra_raw.decode("utf-8", errors="replace")
        if isinstance(extra_raw, str) and extra_raw.strip():
            try:
                cluster_id = (json.loads(extra_raw) or {}).get("gs_cluster_id") or ""
            except Exception:
                cluster_id = ""
        elif isinstance(extra_raw, dict):
            cluster_id = extra_raw.get("gs_cluster_id") or ""
        if not cluster_id:
            return ""

        try:
            direct_body = await self._fetch_ris_direct_for_cluster(cluster_id)
            if direct_body:
                return direct_body
        except Exception as e:
            print(f"[GS][fetch_ris] direct RIS fetch failed: {e}")

        if not self.page:
            try:
                await self.initialize()
            except Exception as e:
                print(f"[GS][fetch_ris] initialize failed: {e}")
                return ""

        try:
            cur = self.page.url or ""
        except Exception:
            cur = ""
        if "scholar.google" not in cur:
            try:
                await self.page.goto("https://scholar.google.com/", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[GS][fetch_ris] goto scholar.google.com failed: {e}")
                return ""

        cite_url = (
            f"https://scholar.google.com/scholar?q=info:{cluster_id}:scholar.google.com/"
            f"&output=cite&scirp=0&hl=en"
        )
        in_page_fetch_js = """async (u) => {
            const r = await fetch(u, {credentials:'include', headers:{'Accept':'text/html,application/x-research-info-systems,*/*'}});
            return {status: r.status, ctype: (r.headers.get('content-type') || '').toLowerCase(), body: await r.text()};
        }"""
        try:
            cite_resp = await self.page.evaluate(in_page_fetch_js, cite_url)
        except Exception as e:
            print(f"[GS][fetch_ris] cite popup fetch raised: {e}")
            return ""
        if not isinstance(cite_resp, dict) or cite_resp.get("status") != 200:
            return ""
        cite_html = cite_resp.get("body") or ""
        # The popup lists 4 anchors -- we want the .ris one (RefMan format).
        m = re.search(
            r'href="(https?://[^"]*scholar\.googleusercontent\.com/scholar\.ris\?[^"]+)"',
            cite_html,
        )
        if not m:
            # GS occasionally rewrites href entities -- unescape and retry on a more permissive match.
            unescaped = cite_html.replace("&amp;", "&")
            m = re.search(
                r'href="(https?://[^"]*scholar\.googleusercontent\.com/scholar\.ris\?[^"]+)"',
                unescaped,
            )
        if not m:
            return ""
        ris_url = m.group(1).replace("&amp;", "&")
        # .ris lives on scholar.googleusercontent.com -- cross-origin from
        # the scholar.google.com page that's serving cite popup, so an
        # in-page fetch() trips Firefox's CORS guard (NetworkError). Use the
        # context-level request API instead: it shares the browser cookies
        # but bypasses the page's same-origin policy. googleusercontent
        # isn't fingerprint-gated like DataDome/Cloudflare so the request
        # succeeds with just the GS session cookies.
        try:
            ris_resp_obj = await self.context.request.get(
                ris_url,
                headers={
                    "Referer": "https://scholar.google.com/",
                    "Accept": "application/x-research-info-systems,*/*",
                },
                timeout=20000,
            )
            if ris_resp_obj.status != 200:
                return ""
            ris_body_text = await ris_resp_obj.text()
        except Exception as e:
            print(f"[GS][fetch_ris] .ris fetch raised: {e}")
            return ""
        body = (ris_body_text or "").replace("\r\n", "\n").strip()
        if "TY  -" in body and "ER" in body:
            return body
        return ""

scraper_instance = GoogleScholarScraper()
