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

            title_elem = row.select_one("h3.gs_rt a") or row.select_one("h3.gs_rt")
            if not title_elem:
                continue

            title = title_elem.text.strip()
            detail_link = title_elem.get("href", "") if title_elem.name == "a" else ""
            gs_cluster_id = row.get("data-cid") or ""
            if not gs_cluster_id:
                parent = row.find_parent("div", class_="gs_r")
                if parent is not None:
                    gs_cluster_id = parent.get("data-cid") or ""

            dedupe_key = detail_link or gs_cluster_id or title
            if dedupe_key in seen_links:
                continue
            seen_links.add(dedupe_key)

            author_pub_elem = row.select_one("div.gs_a")
            author_pub_str = author_pub_elem.text.replace('\xa0', ' ').strip() if author_pub_elem else "N/A"

            author = "N/A"
            date = "N/A"
            source = "Google Scholar"
            venue_name = ""

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
                        source = source_guess if source_guess else source
            else:
                author = author_pub_str

            from scraper_utils import venue_matches
            if journal and venue_name and not venue_matches(journal, venue_name):
                continue

            snippet_elem = row.select_one("div.gs_rs")
            snippet = snippet_elem.text.replace('\n', ' ').strip() if snippet_elem else ""
            uid = hashlib.md5((detail_link or gs_cluster_id or title).encode()).hexdigest()[:8]

            from scraper_utils import platform_hint_from_url
            results.append({
                "id": uid,
                "title": title,
                "author": author,
                "source": "GS: " + source,
                "venue_name": venue_name,
                "date": date,
                "db_type": "GS Aggregated",
                "detail_link": detail_link,
                "recommended_platform": platform_hint_from_url(detail_link),
                "_gs_snippet": snippet,
                "_gs_cluster_id": gs_cluster_id,
            })

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
                
            title_elem = row.select_one("h3.gs_rt a") or row.select_one("h3.gs_rt")
            if not title_elem:
                continue
                
            title = title_elem.text.strip()
            detail_link = title_elem.get("href", "") if title_elem.name == "a" else ""
            
            if detail_link in seen_links and detail_link:
                continue
            seen_links.add(detail_link)
            
            author_pub_elem = row.select_one("div.gs_a")
            author_pub_str = author_pub_elem.text.replace('\xa0', ' ').strip() if author_pub_elem else "N/A"
            
            # Author pub string format: "Author1, Author2 - Journal Name, 2023 - PublisherSite"
            author = "N/A"
            date = "N/A"
            source = "Google Scholar"
            venue_name = ""

            if " - " in author_pub_str:
                parts = author_pub_str.split(" - ")
                author = parts[0]
                if len(parts) > 1:
                    middle = parts[1]
                    date_m = re.search(r'\b(19|20)\d{2}\b', middle)
                    if date_m:
                        date = date_m.group()
                    # Strip the year and trailing comma to leave just the venue/journal token.
                    venue_name = re.sub(r',?\s*\b(19|20)\d{2}\b.*$', '', middle).strip().rstrip(',').strip()
                    source = parts[-1].strip()
                    if source.startswith("…"):
                        source_guess = urllib.parse.urlparse(detail_link).netloc
                        source = source_guess if source_guess else source
            else:
                author = author_pub_str

            # Belt and suspenders on top of GS's &as_publication=, which is fuzzy.
            # GS routinely truncates venue names ("Advances in neural ...") -- use
            # the shared matcher so the trailing ellipsis is forgiven.
            from scraper_utils import venue_matches
            if journal and venue_name and not venue_matches(journal, venue_name):
                continue

            snippet_elem = row.select_one("div.gs_rs")
            snippet = snippet_elem.text.replace('\n', ' ').strip() if snippet_elem else ""

            uid = hashlib.md5(detail_link.encode()).hexdigest()[:8]

            # data-cid lives on <div class="gs_r gs_or gs_scl" data-cid="..."/>.
            # Our `rows` selector matched both gs_r (outer) and gs_ri (inner);
            # for gs_ri we walk up to the gs_r parent. This cluster_id is what
            # gs_scraper.fetch_ris() later uses to build the GS cite URL.
            gs_cluster_id = row.get("data-cid") or ""
            if not gs_cluster_id:
                parent = row.find_parent("div", class_="gs_r")
                if parent is not None:
                    gs_cluster_id = parent.get("data-cid") or ""

            from scraper_utils import platform_hint_from_url
            results.append({
                "id": uid,
                "title": title,
                "author": author,
                "source": "GS: " + source,
                "venue_name": venue_name,
                "date": date,
                "db_type": "GS Aggregated",
                "detail_link": detail_link,
                "recommended_platform": platform_hint_from_url(detail_link),
                "_gs_snippet": snippet,
                "_gs_cluster_id": gs_cluster_id,
            })
            collected += 1
            
        return {
            "total_results": total_results,
            "papers": results
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        # Google scholar direct links point to the publisher.
        # We just return advising the LLM to use the specific platform link directly.
        return {
            "url": detail_url,
            "abstract": "Google Scholar acts as a router. Please inspect the source URL. If it's an IEEE/ACM/SD/CNKI link, use their native MCP scraper or directly download using 'read_paper_content' with the specific platform.",
            "keywords": ["GoogleScholar", "Redirected"],
            "doi": "Fetch via Native Tool."
        }

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        return "Not supported via GS. Please use native read_paper_content with target platform (e.g., IEEE, SD) on this URL."

    async def read_paper_content(self, detail_url: str, output_dir: str) -> str:
        return "Not supported via GS. Please use native read_paper_content with target platform (e.g., IEEE, SD) on this URL."

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
