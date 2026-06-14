import os
import asyncio
import functools
import sys
import json
from mcp_logging import safe_stderr_print
from typing import List, Dict, Optional
from playwright.async_api import async_playwright
import bs4
import pymupdf4llm
import re
import hashlib
import urllib.parse
import aiohttp

print = safe_stderr_print

class IEEEScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.http_session = None
    
    async def initialize(self, force_headful=False):
        import json
        self.playwright = None # Will not be used anymore
        
        from scraper_utils import pooled_profile, load_or_create_fingerprint
        # Canonical profile if free; ephemeral per-PID copy if another live MCP
        # server already owns it -> N clients can run concurrently.
        profile_dir, self._profile_ephemeral = pooled_profile(".ieee_profile", "IEEE")
        self._profile_dir = profile_dir
        _shared_fp = load_or_create_fingerprint("IEEE")
        # parent.lock / .parentlock are Firefox-style locks (Camoufox is Firefox-based);
        # lockfile / SingletonLock are Chromium-style. A crashed previous Camoufox session
        # leaves parent.lock behind, and the next launch silently exits if we don't clean it.
        for lock_name in ["lockfile", "SingletonLock", "parent.lock", ".parentlock"]:
            lfile = os.path.join(profile_dir, lock_name)
            if os.path.exists(lfile):
                try: os.remove(lfile)
                except: pass

        os.makedirs(os.path.join(profile_dir, "Default"), exist_ok=True)
        # Bypasses WAF completely; renders PDFs not internally, but drops straight to downloader
        prefs = {"plugins": {"always_open_pdf_externally": True}, "download": {"prompt_for_download": False}}
        with open(os.path.join(profile_dir, "Default", "Preferences"), "w") as f:
            json.dump(prefs, f)
            
        from camoufox.async_api import AsyncCamoufox
        # NOTE: do not enable block_images on IEEE — its Cloudflare profile flags the
        # missing-image-requests pattern. Speed gains come from sleep tightening only.
        _cam_kw = dict(
            headless=not force_headful,
            user_data_dir=profile_dir,
            persistent_context=True,
            os="windows",
            humanize=True,
            geoip=True,
        )
        if _shared_fp is not None:
            # Pinned fingerprint is intentional (cf_clearance is bound to it and
            # must stay identical across clients); silence Camoufox's advisory.
            _cam_kw["fingerprint"] = _shared_fp
            _cam_kw["i_know_what_im_doing"] = True
        self.camoufox_cm = AsyncCamoufox(**_cam_kw)
        self.context = await self.camoufox_cm.__aenter__()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        # Seed shared cf_clearance/DataDome cookies before any navigation so a
        # verification solved by another client carries over.
        from scraper_utils import apply_browser_cookies
        await apply_browser_cookies(self.context, "IEEE")

    async def close(self):
        if getattr(self, "http_session", None) and not self.http_session.closed:
            try:
                await self.http_session.close()
            except Exception:
                pass
            self.http_session = None
        if getattr(self, "context", None):
            try:
                from scraper_utils import capture_browser_cookies
                await capture_browser_cookies(self.context, "IEEE")
            except Exception:
                pass
        if hasattr(self, 'page') and self.page:
            try:
                await self.page.close()
                self.page = None
            except:
                pass
        if hasattr(self, 'camoufox_cm') and self.camoufox_cm:
            await self.camoufox_cm.__aexit__(None, None, None)
            self.camoufox_cm = None
            self.context = None
        elif getattr(self, 'context', None):
            await self.context.close()
            self.context = None
            self.playwright = None
        if getattr(self, "_profile_dir", None):
            from scraper_utils import cleanup_pooled_profile
            cleanup_pooled_profile(self._profile_dir, getattr(self, "_profile_ephemeral", False))
            self._profile_dir = None

    def _default_headers(self, *, referer: str = None, accept: str = "application/json, text/plain, */*") -> dict:
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

    async def _ensure_http_session(self) -> aiohttp.ClientSession:
        if self.http_session and not self.http_session.closed:
            return self.http_session
        timeout = aiohttp.ClientTimeout(total=180, connect=15, sock_read=150)
        self.http_session = aiohttp.ClientSession(timeout=timeout)
        return self.http_session

    def _has_ieee_session_cookies(self) -> bool:
        session = getattr(self, "http_session", None)
        if not session or session.closed:
            return False
        try:
            names = {cookie.key for cookie in session.cookie_jar}
        except Exception:
            return False
        return bool(names.intersection({"JSESSIONID", "WLSESSION", "xpluserinfo", "ERIGHTS", "ipList"}))

    def _strip_html(self, text: str) -> str:
        clean = bs4.BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
        return self._clean_text_markers(clean)

    def _clean_text_markers(self, text: str) -> str:
        return str(text or "").replace("[::", "").replace("::]", "")

    def _build_search_payload(
        self,
        query: str,
        search_field: str,
        source_type: str,
        journal: str,
        start_year: int,
        end_year: int,
        sort_by: str,
        page_number: int,
    ) -> dict:
        journal_clean = (journal or "").strip()
        if journal_clean:
            quoted = journal_clean.replace('"', '\\"')
            search_query = f'("Publication Title":"{quoted}") AND ({query})'
        else:
            search_query = query

        payload = {
            "newsearch": True,
            "queryText": search_query,
            "highlight": True,
            "returnFacets": ["ALL"],
            "returnType": "SEARCH",
            "matchPubs": True,
            "pageNumber": str(page_number),
        }
        if sort_by == "citations":
            payload["sortType"] = "paper-citations"
        elif sort_by == "date_desc":
            payload["sortType"] = "newest"

        if start_year and end_year:
            payload["ranges"] = [f"{start_year}_{end_year}_Year"]
        elif start_year:
            payload["ranges"] = [f"{start_year}_2026_Year"]
        elif end_year:
            payload["ranges"] = [f"1900_{end_year}_Year"]
        return payload

    def _record_matches_source_type(self, record: dict, source_type: str) -> bool:
        source = (source_type or "").strip().lower()
        if source in ("", "all", "全部"):
            return True
        haystack = " ".join(
            str(record.get(k) or "")
            for k in ("contentType", "displayContentType", "articleContentType", "docIdentifier")
        ).lower()
        aliases = {
            "conferences": ("conference",),
            "journals": ("journal", "magazine", "early access"),
            "magazines": ("magazine",),
            "books": ("book",),
            "early access articles": ("early access",),
            "standards": ("standard",),
            "courses": ("course",),
        }
        needles = aliases.get(source, (source,))
        return any(needle in haystack for needle in needles)

    def _parse_search_record(self, record: dict, journal: str = None) -> Optional[dict]:
        title = self._strip_html(record.get("articleTitle") or record.get("highlightedTitle") or "N/A")
        link = record.get("documentLink") or ""
        if link and link.startswith("/"):
            link = urllib.parse.urljoin("https://ieeexplore.ieee.org", link)
        arnumber = str(record.get("articleNumber") or record.get("arnumber") or "").strip()
        if not link and arnumber:
            link = f"https://ieeexplore.ieee.org/document/{arnumber}/"

        authors = []
        for author in record.get("authors") or []:
            if isinstance(author, dict):
                name = author.get("preferredName") or author.get("normalizedName")
                if name:
                    authors.append(str(name).strip())
            elif isinstance(author, str):
                authors.append(author.strip())
        author_str = ", ".join([a for a in authors if a]) or "N/A"

        venue_name = (
            record.get("displayPublicationTitle")
            or record.get("publicationTitle")
            or record.get("publication")
            or ""
        ).strip()
        venue_name = self._clean_text_markers(venue_name)
        from scraper_utils import venue_matches
        if journal and venue_name and not venue_matches(journal, venue_name):
            return None

        date = (
            record.get("publicationDate")
            or record.get("publicationYear")
            or record.get("dateOfInsertion")
            or "N/A"
        )
        doc_type = (
            record.get("displayContentType")
            or record.get("contentType")
            or record.get("articleContentType")
            or "N/A"
        )
        pdf_url = record.get("pdfLink") or ""
        if pdf_url and pdf_url.startswith("/"):
            pdf_url = urllib.parse.urljoin("https://ieeexplore.ieee.org", pdf_url)
        uid = hashlib.md5((link or arnumber or title).encode()).hexdigest()[:8]
        return {
            "id": uid,
            "title": title,
            "author": author_str,
            "source": venue_name or "IEEE",
            "venue_name": venue_name,
            "date": self._clean_text_markers(str(date).strip()),
            "db_type": self._clean_text_markers(str(doc_type).strip()),
            "detail_link": link,
            "pdf_url": pdf_url,
            "arnumber": arnumber,
        }

    async def _post_search_api(self, payload: dict) -> dict:
        url = "https://ieeexplore.ieee.org/rest/search"
        referer = "https://ieeexplore.ieee.org/search/searchresult.jsp"
        headers = self._default_headers(referer=referer, accept="application/json, text/plain, */*")
        headers.update({
            "Content-Type": "application/json",
            "Origin": "https://ieeexplore.ieee.org",
        })
        session = await self._ensure_http_session()
        async with session.post(url, json=payload, headers=headers, ssl=False) as resp:
            body = await resp.read()
            if resp.status != 200:
                raise RuntimeError(f"IEEE search API returned HTTP {resp.status}")
            try:
                return json.loads(body.decode("utf-8", errors="ignore"))
            except Exception as e:
                raise RuntimeError(f"IEEE search API returned non-JSON payload: {e}") from e

    async def _search_papers_direct(
        self,
        query: str,
        search_field: str,
        source_type: str,
        journal: str,
        start_year: int,
        end_year: int,
        sort_by: str,
        start_index: int,
        limit: int,
    ) -> Dict:
        page_size = 25
        page_number = (start_index // page_size) + 1
        offset = start_index % page_size
        papers = []
        total_results = "0"

        while len(papers) < limit and page_number <= ((start_index // page_size) + 6):
            payload = self._build_search_payload(
                query,
                search_field,
                source_type,
                journal,
                start_year,
                end_year,
                sort_by,
                page_number,
            )
            data = await self._post_search_api(payload)
            total_results = str(data.get("totalRecords") or data.get("totalResults") or total_results)
            records = data.get("records") or []
            if not records:
                break
            for record in records[offset:]:
                if len(papers) >= limit:
                    break
                if not self._record_matches_source_type(record, source_type):
                    continue
                parsed = self._parse_search_record(record, journal=journal)
                if parsed:
                    papers.append(parsed)
            offset = 0
            if len(records) < page_size:
                break
            page_number += 1
        return {"total_results": total_results, "papers": papers}

    def _parse_detail_metadata(self, html: str) -> dict:
        match = re.search(r'xplGlobal\.document\.metadata\s*=\s*(\{.*?\});', html or "", re.DOTALL)
        if not match:
            raise RuntimeError("IEEE detail page did not contain xplGlobal.document.metadata")
        return json.loads(match.group(1))

    def _metadata_to_details(self, meta: dict, detail_url: str) -> Dict[str, str]:
        abstract = "No abstract found."
        if meta.get("abstract"):
            abstract = self._strip_html(meta.get("abstract"))
        keywords = []
        for k_obj in meta.get("keywords") or []:
            if isinstance(k_obj, dict):
                keywords.extend([str(kw).strip() for kw in k_obj.get("kwd") or [] if kw])
        doi = meta.get("doi") or ""
        return {
            "url": detail_url,
            "abstract": abstract,
            "keywords": list(dict.fromkeys(keywords)),
            "doi": doi,
        }

    async def _fetch_detail_metadata_direct(self, detail_url: str) -> dict:
        headers = self._default_headers(
            referer="https://ieeexplore.ieee.org/search/searchresult.jsp",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        session = await self._ensure_http_session()
        async with session.get(detail_url, headers=headers, ssl=False) as resp:
            html = await resp.text(errors="ignore")
            if resp.status != 200:
                raise RuntimeError(f"IEEE detail direct GET returned HTTP {resp.status}")
        return self._parse_detail_metadata(html)

    async def _download_paper_direct(self, detail_url: str, output_dir: str, arnumber: str) -> str:
        headers = self._default_headers(
            referer="https://ieeexplore.ieee.org/search/searchresult.jsp",
            accept="text/html,application/xhtml+xml,application/pdf,*/*",
        )
        pdf_headers = self._default_headers(
            referer=detail_url,
            accept="application/pdf,text/html,*/*",
        )
        pdf_url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}"
        file_path = os.path.join(output_dir, f"ieee_{arnumber}.pdf")
        session = await self._ensure_http_session()

        async def warm_detail_session():
            async with session.get(detail_url, headers=headers, ssl=False) as resp:
                await resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"IEEE detail warmup returned HTTP {resp.status}")

        async def fetch_pdf():
            async with session.get(pdf_url, headers=pdf_headers, ssl=False, allow_redirects=True) as resp:
                body = await resp.read()
                content_type = (resp.headers.get("content-type") or "").lower()
                if resp.status == 200 and (body.startswith(b"%PDF") or "application/pdf" in content_type):
                    with open(file_path, "wb") as f:
                        f.write(body)
                    print(f"[IEEE] Direct PDF request succeeded ({len(body)} bytes).")
                    return file_path
                return resp.status, content_type, body

        warmed_now = False
        if not self._has_ieee_session_cookies():
            await warm_detail_session()
            warmed_now = True

        result = await fetch_pdf()
        if isinstance(result, str):
            return result

        if not warmed_now:
            # A search-warmed session is usually enough, but if IEEE returns a
            # transient HTML trap, refresh article-scoped cookies once and retry.
            await warm_detail_session()
            result = await fetch_pdf()
            if isinstance(result, str):
                return result

        status, content_type, body = result

        try:
            os.makedirs("scratch", exist_ok=True)
            import time as _t
            dump_path = os.path.join("scratch", f"ieee_direct_pdf_fail_{arnumber}_{int(_t.time())}.html")
            with open(dump_path, "wb") as f:
                f.write(body)
        except Exception:
            dump_path = "(dump failed)"
        raise RuntimeError(
            f"IEEE direct PDF returned HTTP {status}, content-type={content_type or '?'}, "
            f"size={len(body)}B; dumped to {dump_path}"
        )

    async def search_papers(self, query: str, search_field: str = "All", db_scope: str = "", source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None, sort_by: str = "relevance", start_index: int = 0, limit: int = 10) -> Dict:
        try:
            return await self._search_papers_direct(
                query,
                search_field,
                source_type,
                journal,
                start_year,
                end_year,
                sort_by,
                start_index,
                limit,
            )
        except Exception as e:
            print(f"[IEEE] Direct search API failed ({e}); falling back to browser search.")

        if not self.page:
            await self.initialize()

        import urllib.parse
        # IEEE Xplore advanced-query syntax: ("Publication Title":"...") forces results
        # to a single journal. Without this, the journal argument was silently ignored.
        journal_clean = (journal or "").strip()
        if journal_clean:
            quoted = journal_clean.replace('"', '\\"')
            search_query = f'("Publication Title":"{quoted}") AND ({query})'
        else:
            search_query = query
        encoded_query = urllib.parse.quote(search_query)

        sort_str = ""
        if sort_by == "citations":
            sort_str = "&sortType=paper-citations"
        elif sort_by == "date_desc":
            sort_str = "&sortType=newest"

        range_str = ""
        if start_year and end_year:
            range_str = f"&ranges={start_year}_{end_year}_Year"
        elif start_year:
            range_str = f"&ranges={start_year}_2026_Year"
        elif end_year:
            range_str = f"&ranges=1900_{end_year}_Year"

        base_url = f"https://ieeexplore.ieee.org/search/searchresult.jsp?newsearch=true&queryText={encoded_query}{sort_str}{range_str}"
        
        await self.page.goto(base_url, wait_until="networkidle")
        await asyncio.sleep(1)

        # Determine total results
        html = await self.page.content()
        soup = bs4.BeautifulSoup(html, 'html.parser')
        
        total_count = "未知"
        try:
            # IEEE typically has something like "Showing 1-25 of 1,234,567 results for"
            match = re.search(r'of\s+([\d,]+)\s+results?', soup.text, re.IGNORECASE)
            if match:
                total_count = match.group(1).replace(",", "")
            else:
                for pattern in [
                    r'"totalRecords"\s*:\s*"?([\d,]+)"?',
                    r'"totalResults"\s*:\s*"?([\d,]+)"?',
                    r'"recordsTotal"\s*:\s*"?([\d,]+)"?',
                    r'([\d,]+)\s+Results',
                ]:
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        total_count = match.group(1).replace(",", "")
                        break
            if total_count == "未知":
                # Try finding in specific classes
                count_elem = soup.select_one("span.strong, h1, span[class*='results-display']")
                if count_elem:
                    match = re.search(r'([\d,]+)\s+results', count_elem.text, re.IGNORECASE)
                    if match:
                        total_count = match.group(1).replace(",", "")
        except Exception as e:
            print("IEEE Extract count error:", e)

        # In IEEE, one page applies source_type by navigating Facets
        if source_type.lower() != "all" and source_type != "全部":
            try:
                # Find the label or specific text for the facet
                facet = self.page.locator(f"//label[contains(text(), '{source_type}')] | //div[contains(@class, 'facet-label') and contains(text(), '{source_type}')] | //input[@type='checkbox']/parent::*//*[contains(text(), '{source_type}')]").first
                if await facet.count() > 0:
                    await facet.scroll_into_view_if_needed()
                    await asyncio.sleep(0.5)
                    await facet.click(force=True)
                    
                    # Sometimes there is an Apply button, sometimes it auto-reloads.
                    apply_btn = self.page.locator("button:has-text('Apply')").first
                    if await apply_btn.count() > 0:
                        await apply_btn.click(force=True)
                    
                    # Wait for network and DOM update
                    await asyncio.sleep(4)
            except Exception as e:
                print(f"Error filtering IEEE source_type: {e}")

        # IEEE pagination works via passing pageNumber= (1-indexed based on page sizes of 25)
        # However, for simplicity and alignment with CNKI, we just simulate clicking Next or doing basic pagination.
        target_start_page = (start_index // 25) + 1
        offset_in_first_page = start_index % 25

        if target_start_page > 1:
            try:
                # Direct url manipulation may be safer for leaping pages in IEEE
                current_url = self.page.url
                if "pageNumber=" not in current_url:
                    current_url += f"&pageNumber={target_start_page}"
                else:
                    current_url = re.sub(r'pageNumber=\d+', f'pageNumber={target_start_page}', current_url)
                await self.page.goto(current_url, wait_until="networkidle")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"Pagination error jumping to page {target_start_page}: {e}")

        results = []
        collected = 0
        seen_links = set()
        
        while collected < limit:
            html = await self.page.content()
            soup = bs4.BeautifulSoup(html, "html.parser")
            
            # The items are usually inside div.List-results-items
            rows = soup.select("div.List-results-items, div.result-item")
            if not rows:
                break
                
            startIndexToProcess = offset_in_first_page if collected == 0 else 0
            
            for row in rows[startIndexToProcess:]:
                if collected >= limit:
                    break
                    
                title_elem = row.select_one("h2 a, h3 a")
                title = title_elem.text.strip() if title_elem else "N/A"
                detail_link = title_elem.get("href") if title_elem else ""
                if detail_link and detail_link.startswith("/"):
                    detail_link = "https://ieeexplore.ieee.org" + detail_link
                    
                if detail_link in seen_links:
                    continue
                seen_links.add(detail_link)
                    
                author_elem = row.select_one(".author, p.author")
                author = author_elem.text.strip() if author_elem else "N/A"
                author = author.replace("\n", "").replace("  ", "")

                # Venue link lives directly inside div.description (the first <a> before the
                # nested publisher-info-container). Journals point at /xpl/RecentIssue.jsp?punumber=
                # while conferences point at /xpl/conhome/<id>/proceeding.
                description = row.select_one("div.description")
                venue_name = ""
                if description:
                    venue_link = description.find(
                        "a",
                        href=lambda h: h and ("/xpl/RecentIssue" in h or "/xpl/conhome" in h or "punumber=" in h),
                    )
                    if venue_link and venue_link.text.strip():
                        venue_name = venue_link.text.strip()
                publisher_container = row.select_one("div.publisher-info-container")

                # Belt and suspenders: if the caller asked for a specific journal but IEEE
                # returned a paper from a different venue (happens when the query syntax falls
                # back to full-text matching), skip it client-side. venue_matches handles
                # punctuation / parenthetical noise like "Conference Paper" suffixes.
                from scraper_utils import venue_matches
                if journal_clean and venue_name and not venue_matches(journal_clean, venue_name):
                    continue

                source = publisher_container.text.strip() if publisher_container else "IEEE"

                # Year info
                year_elem = row.select_one("div.description, div.publisher-info-container")
                date = "N/A"
                if year_elem:
                    y_match = re.search(r'Year:\s*(\d{4})', year_elem.text)
                    if y_match:
                        date = y_match.group(1)

                db_type = "N/A"
                if year_elem:
                    t_match = re.search(r'\|\s*([^|]+)$', year_elem.text)
                    if t_match:
                        db_type = t_match.group(1).strip()

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
                
            if collected < limit:
                try:
                    next_btn = self.page.locator("a.next-btn, a.next, button:has-text('Next')").first
                    if await next_btn.count() > 0:
                        await next_btn.click()
                        await asyncio.sleep(4)
                    else:
                        break
                except Exception:
                    break
                    
        # We have results -> we are past Cloudflare; persist the good cookies
        # so concurrent / future clients skip the manual challenge.
        try:
            from scraper_utils import capture_browser_cookies
            await capture_browser_cookies(self.context, "IEEE")
        except Exception:
            pass

        return {
            "total_results": total_count,
            "papers": results
        }

    async def get_paper_details(self, detail_url: str) -> Dict[str, str]:
        try:
            meta = await self._fetch_detail_metadata_direct(detail_url)
            return self._metadata_to_details(meta, detail_url)
        except Exception as e:
            print(f"[IEEE] Direct detail GET failed ({e}); falling back to browser detail.")

        if not self.page:
            await self.initialize()

        from scraper_utils import goto_with_retry
        await goto_with_retry(self.page, detail_url, wait_until="networkidle")
        await asyncio.sleep(2)
        
        try:
            html = await self.page.content()
            abstract = "No abstract found."
            keywords = []
            doi = ""

            # IEEE detail page injects all metadata as JSON into a script tag
            # This is significantly more robust than parsing React DOM classes
            match = re.search(r'xplGlobal\.document\.metadata\s*=\s*(\{.*?\});', html, re.DOTALL)
            if match:
                try:
                    import json
                    meta = json.loads(match.group(1))
                    if "abstract" in meta:
                        abstract = bs4.BeautifulSoup(meta["abstract"], "html.parser").text.strip()
                    if "doi" in meta:
                        doi = meta["doi"]
                    if "keywords" in meta:
                        for k_obj in meta["keywords"]:
                            if "kwd" in k_obj:
                                keywords.extend(k_obj["kwd"])
                except Exception as e:
                    print("IEEE JSON Meta parse error:", e)

            # Fallback to visual DOM parsing if JSON fails
            if abstract == "No abstract found.":
                soup = bs4.BeautifulSoup(html, "html.parser")
                abstract_elem = soup.select_one("div.abstract-text, div.u-mb-1 div")
                if abstract_elem:
                    abstract = abstract_elem.text.strip()
                if abstract.startswith("Abstract:"):
                    abstract = abstract[9:].strip()
                doi_elem = soup.select_one("a[href^='https://doi.org/']")
                if doi_elem:
                    doi = doi_elem.text.strip()

            return {
                "url": detail_url,
                "abstract": abstract,
                "keywords": list(set(keywords)),
                "doi": doi
            }
        except Exception as e:
            return {"error": str(e)}

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)
        
        # We find the arnumber directly from the URL e.g. /document/11007439/
        match = re.search(r'document/(\d+)', detail_url)
        if not match:
            return "Could not find arnumber in detail_url."
        arnumber = match.group(1)

        try:
            return await self._download_paper_direct(detail_url, output_dir, arnumber)
        except Exception as e:
            print(f"[IEEE] Direct PDF request failed ({e}); falling back to browser download.")

        if not self.page:
            await self.initialize()
        
        # Use the direct PDF stream URL
        pdf_url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}"
        
        await self.page.goto(detail_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        file_path = os.path.join(output_dir, f"ieee_{arnumber}.pdf")
        
        # Action: A-tag emulation triggered 418 inside IEEE WAF since it's an untrusted DOM Event.
        # Action Update: We natively transition page via evaluate JS. The Preferences JSON triggers immediate DL.
        try:
            file_path = os.path.join(output_dir, f"ieee_{arnumber}.pdf")
            
            # Setup a listener to catch any response that looks like the PDF stream
            pdf_body = []
            async def handle_response(response):
                if response.status == 200 and ("application/pdf" in response.headers.get("content-type", "") or response.url.endswith(".pdf") or "getPDF.jsp" in response.url):
                    try:
                        body = await response.body()
                        if b"%PDF" in body[:20]:
                            pdf_body.append(body)
                            print(f"Intercepted direct PDF stream from {response.url[:60]}...")
                    except Exception:
                        pass
                        
            self.page.on("response", handle_response)
            
            download_event_task = asyncio.create_task(self.page.wait_for_event("download"))
            js_code = f"window.location.href = '{pdf_url}';"
            await self.page.evaluate(js_code)
                
            is_downloaded = False
            for _ in range(60):
                if download_event_task.done():
                    try:
                        download = download_event_task.result()
                        await download.save_as(file_path)
                        is_downloaded = True
                    except Exception as e:
                        print("Native download failed:", e)
                    break
                    
                if len(pdf_body) > 0:
                    with open(file_path, "wb") as f:
                        f.write(pdf_body[0])
                    is_downloaded = True
                    download_event_task.cancel()
                    break
                    
                await asyncio.sleep(1)
                
            self.page.remove_listener("response", handle_response)
            if not download_event_task.done():
                download_event_task.cancel()
                
            if not is_downloaded:
                # Classify WHY: a paywalled / no-entitlement document serves an
                # HTML access page from getPDF.jsp instead of a PDF stream; a
                # Cloudflare/WAF challenge serves its own interstitial. Capture
                # what we actually landed on so this stops being a blind timeout.
                try:
                    cur_url = self.page.url
                    title = await self.page.title()
                    html = await self.page.content()
                except Exception:
                    cur_url, title, html = "?", "?", ""
                low = html.lower()
                if any(s in title or s in html for s in ("Just a moment", "Attention Required", "Cloudflare")) or "cf-challenge" in low:
                    reason = "Cloudflare/WAF challenge page"
                elif any(s in html for s in ("Purchase", "Get Access", "subscribe", "Sign In to Continue", "institutional access")) and "%PDF" not in html:
                    reason = "paywalled / no entitlement (IEEE served an access page, not a PDF)"
                else:
                    reason = "no download event and no PDF stream (page structure may have changed)"
                try:
                    os.makedirs("scratch", exist_ok=True)
                    import time as _t
                    _dp = os.path.join("scratch", f"ieee_dl_fail_{arnumber}_{int(_t.time())}.html")
                    with open(_dp, "w", encoding="utf-8") as _f:
                        _f.write(f"<!-- arnumber={arnumber} pdf_url={pdf_url} landed={cur_url} title={title!r} reason={reason} -->\n")
                        _f.write(html)
                except Exception:
                    _dp = "(dump failed)"
                return (
                    f"Error downloading IEEE PDF (arnumber {arnumber}): {reason}. "
                    f"Landed on {cur_url[:120]!r} (title={title[:80]!r}). "
                    f"HTML dumped to {_dp}."
                )
                
            # Additional size verify
            if os.path.getsize(file_path) < 70000:
                return f"Error: Download generated successfully but size seems to be an HTML trap ({os.path.getsize(file_path)} bytes)."
                
            return file_path
        except Exception as e:
            return f"Error downloading IEEE PDF: {str(e)}"

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
        """Pull an IEEE-formatted RIS via their citation export REST endpoint.

        Returns the RIS body on success or empty string on any failure --
        the caller falls back to local synthesis. Uses the scraper's already
        warmed-up Playwright APIRequestContext so existing cookies / referer
        propagate naturally.
        """
        m = re.search(r"/document/(\d+)", detail_url)
        if not m:
            return ""
        arnumber = m.group(1)
        if not self.page:
            await self.initialize()
        endpoints = [
            (
                "https://ieeexplore.ieee.org/rest/search/citation/format",
                "json",
                {
                    "recordIds": [arnumber],
                    "download-format": "download-ris",
                    "citations-format": "citation-abstract",
                },
            ),
            (
                "https://ieeexplore.ieee.org/xpl/downloadCitations",
                "form",
                {
                    "recordIds": arnumber,
                    "download-format": "download-ris",
                    "citations-format": "citation-abstract",
                    "x": "1",
                },
            ),
        ]
        for url, kind, payload in endpoints:
            try:
                if kind == "json":
                    resp = await self.context.request.post(
                        url,
                        data=payload,
                        headers={"Referer": detail_url, "Origin": "https://ieeexplore.ieee.org"},
                        timeout=30000,
                    )
                else:
                    resp = await self.context.request.post(
                        url,
                        form=payload,
                        headers={"Referer": detail_url, "Origin": "https://ieeexplore.ieee.org"},
                        timeout=30000,
                    )
                if resp.status != 200:
                    continue
                body = await resp.text()
                # JSON endpoint wraps the RIS payload in {"data":"TY  - ..."}.
                if body.lstrip().startswith("{"):
                    import json as _json
                    try:
                        data = _json.loads(body).get("data", "")
                    except Exception:
                        data = ""
                    body = data or body
                if "TY  - " in body and "ER" in body:
                    return body.replace("\r\n", "\n").strip()
            except Exception as e:
                print(f"[IEEE] fetch_ris via {url[:60]} failed: {e}")
        return ""


scraper_instance = IEEEScraper()
