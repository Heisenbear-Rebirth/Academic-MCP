from __future__ import annotations

import asyncio
import datetime
import os
import re
import urllib.parse
from pathlib import Path
from typing import Dict, List

from bs4 import BeautifulSoup

from mcp_logging import safe_stderr_print
from runtime_config import ensure_runtime_environment

print = safe_stderr_print
ensure_runtime_environment()


class MDPIScraper:
    BASE_URL = "https://www.mdpi.com"
    DIRECT_IMPERSONATE = "firefox135"
    DIRECT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"

    def __init__(self):
        self._session = None
        self._lock = asyncio.Lock()
        self._pdf_cache: dict[str, str] = {}

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @classmethod
    def _native_id_from_url(cls, url: str) -> str:
        parsed = urllib.parse.urlparse(url or "")
        path = urllib.parse.unquote(parsed.path or "").strip("/")
        doi = cls._extract_doi(url)
        if doi:
            return doi
        match = re.match(r"([0-9]{4}-[0-9]{3,4}X?/\d+/\d+/[^/?#]+)", path, re.IGNORECASE)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_doi(text: str) -> str:
        match = re.search(r"(10\.3390/[A-Za-z0-9._;()/:-]+)", text or "", re.IGNORECASE)
        return match.group(1).rstrip(").,;") if match else ""

    @staticmethod
    def _safe_stem(value: str) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._")
        return stem[:120] or "mdpi_paper"

    @staticmethod
    def _akamai_interstitial(html: str) -> bool:
        text = html or ""
        return "/_sec/verify?provider=interstitial" in text and "bm-verify" in text

    @staticmethod
    def _akamai_payload(html: str) -> dict | None:
        i_match = re.search(r"var i = (\d+);", html or "")
        n_match = re.search(r'Number\("(\d+)" \+ "(\d+)"\)', html or "")
        token_match = re.search(r'"bm-verify"\s*:\s*"([^"]+)"', html or "")
        if not (i_match and n_match and token_match):
            return None
        return {
            "bm-verify": token_match.group(1),
            "pow": int(i_match.group(1)) + int(n_match.group(1) + n_match.group(2)),
        }

    @staticmethod
    def _headers(*, referer: str = None, accept: str = None) -> dict:
        headers = {
            "user-agent": MDPIScraper.DIRECT_UA,
            "accept": accept or "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
        }
        if referer:
            headers["referer"] = referer
        return headers

    def _get_session(self):
        if self._session is None:
            from curl_cffi import requests as curl_requests

            self._session = curl_requests.Session(impersonate=self.DIRECT_IMPERSONATE)
        return self._session

    async def _request(self, method: str, url: str, *, referer: str = None, accept: str = None, data: dict = None):
        async with self._lock:
            return await asyncio.to_thread(
                self._request_sync,
                method,
                url,
                referer,
                accept,
                data,
            )

    def _request_sync(self, method: str, url: str, referer: str = None, accept: str = None, data: dict = None):
        session = self._get_session()
        headers = self._headers(referer=referer, accept=accept)

        def perform():
            if method.upper() == "POST":
                return session.post(url, headers=headers, data=data or {}, timeout=60, allow_redirects=True)
            return session.get(url, headers=headers, timeout=60, allow_redirects=True)

        last_error = None
        for attempt in range(3):
            try:
                response = perform()
                break
            except Exception as e:
                last_error = e
                if attempt == 2:
                    raise
                print(f"[MDPI] direct request retry {attempt + 1}/2 after {type(e).__name__}: {e}")
        else:
            raise last_error

        text = ""
        try:
            text = response.text or ""
        except Exception:
            text = ""
        if self._akamai_interstitial(text):
            payload = self._akamai_payload(text)
            if not payload:
                raise RuntimeError("MDPI Akamai interstitial was returned but could not be parsed.")
            verify_url = "https://www.mdpi.com/_sec/verify?provider=interstitial"
            verify = session.post(
                verify_url,
                json=payload,
                headers=self._headers(referer=url, accept="application/json,*/*"),
                timeout=60,
                allow_redirects=True,
            )
            if verify.status_code >= 400:
                raise RuntimeError(f"MDPI Akamai verification failed: HTTP {verify.status_code}")
            if method.upper() == "POST":
                response = session.post(url, headers=headers, data=data or {}, timeout=60, allow_redirects=True)
            else:
                response = session.get(url, headers=headers, timeout=60, allow_redirects=True)
        return response

    async def _fetch_html(self, url: str, *, referer: str = None) -> str:
        response = await self._request("GET", url, referer=referer)
        text = response.text or ""
        if self._akamai_interstitial(text):
            raise RuntimeError("MDPI Akamai verification did not clear.")
        if response.status_code >= 400:
            raise RuntimeError(f"MDPI HTML request failed: HTTP {response.status_code}")
        return text

    @staticmethod
    def _meta_values(soup: BeautifulSoup, name: str) -> list[str]:
        values = []
        for tag in soup.find_all("meta", attrs={"name": name}):
            value = tag.get("content")
            if value:
                values.append(re.sub(r"\s+", " ", value).strip())
        return values

    @classmethod
    def _parse_detail_html(cls, detail_url: str, html: str) -> Dict[str, object]:
        soup = BeautifulSoup(html, "html.parser")
        title = (cls._meta_values(soup, "citation_title") or cls._meta_values(soup, "dc.title") or [""])[0]
        authors = cls._meta_values(soup, "citation_author") or cls._meta_values(soup, "dc.creator")
        doi = (cls._meta_values(soup, "citation_doi") or cls._meta_values(soup, "dc.identifier") or [""])[0]
        abstract = (cls._meta_values(soup, "dc.description") or cls._meta_values(soup, "description") or [""])[0]
        keywords = cls._meta_values(soup, "dc.subject")
        journal = (cls._meta_values(soup, "citation_journal_title") or [""])[0]
        pub_date = (
            cls._meta_values(soup, "citation_online_date")
            or cls._meta_values(soup, "citation_publication_date")
            or cls._meta_values(soup, "dc.date")
            or [""]
        )[0]
        pdf_url = (cls._meta_values(soup, "citation_pdf_url") or [""])[0]
        if pdf_url:
            pdf_url = urllib.parse.urljoin(cls.BASE_URL, pdf_url)
        else:
            pdf_url = urllib.parse.urljoin(cls.BASE_URL, cls._detail_path(detail_url) + "/pdf")
        return {
            "url": detail_url,
            "title": title,
            "authors": authors,
            "author": ", ".join(authors),
            "source": journal,
            "venue_name": journal,
            "pub_date": pub_date,
            "abstract": abstract or "No abstract found",
            "keywords": keywords,
            "doi": doi,
            "pdf_url": pdf_url,
        }

    @staticmethod
    def _detail_path(url: str) -> str:
        parsed = urllib.parse.urlparse(url or "")
        path = parsed.path or ""
        path = re.sub(r"/(?:pdf|htm|xml)(?:/)?$", "", path)
        return path.rstrip("/")

    @classmethod
    def _parse_search_item(cls, item, *, source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None) -> Dict | None:
        title_link = item.select_one("a.title-link")
        if not title_link:
            return None
        detail_link = urllib.parse.urljoin(cls.BASE_URL, title_link.get("href") or "")
        title = cls._clean_text(title_link.get_text(" ", strip=True))
        native_id = cls._native_id_from_url(detail_link) or detail_link

        authors = [
            cls._clean_text(node.get_text(" ", strip=True))
            for node in item.select(".authors strong")
        ]
        authors = [a for a in authors if a]
        meta_node = item.select_one(".color-grey-dark")
        meta_text = cls._clean_text(meta_node.get_text(" ", strip=True)) if meta_node else ""
        journal_name = ""
        if meta_node:
            journal_tag = meta_node.find("em")
            if journal_tag:
                journal_name = cls._clean_text(journal_tag.get_text(" ", strip=True))
        doi = cls._extract_doi(meta_text)
        date_match = re.search(r"-\s*([0-3]?\d\s+[A-Za-z]{3,9}\s+(?:19|20)\d{2})", meta_text)
        pub_date = date_match.group(1) if date_match else ""
        if not pub_date:
            year_match = re.search(r"\b(?:19|20)\d{2}\b", meta_text)
            pub_date = year_match.group(0) if year_match else ""

        article_type_node = item.select_one(".label.articletype")
        article_type = cls._clean_text(article_type_node.get_text(" ", strip=True)) if article_type_node else ""
        abstract_node = item.select_one(".abstract-full") or item.select_one(".abstract-cropped")
        abstract = cls._clean_text(abstract_node.get_text(" ", strip=True)) if abstract_node else ""
        abstract = re.sub(r"\s*Full article\s*$", "", abstract).strip()

        pdf_node = item.select_one("a.UD_Listings_ArticlePDF, a[href*='/pdf']")
        pdf_url = urllib.parse.urljoin(cls.BASE_URL, pdf_node.get("href")) if pdf_node else ""

        wanted_type = str(source_type or "").strip().lower()
        if wanted_type not in {"", "all"} and wanted_type not in article_type.lower():
            return None
        if journal and str(journal).strip().lower() not in journal_name.lower():
            return None
        year = None
        year_match = re.search(r"\b(?:19|20)\d{2}\b", pub_date or meta_text)
        if year_match:
            year = int(year_match.group(0))
        if start_year is not None and (year is None or year < int(start_year)):
            return None
        if end_year is not None and (year is None or year > int(end_year)):
            return None

        return {
            "id": native_id,
            "title": title,
            "author": ", ".join(authors),
            "authors": authors,
            "source": journal_name,
            "venue_name": journal_name,
            "date": pub_date,
            "pub_date": pub_date,
            "abstract": abstract,
            "doi": doi,
            "detail_link": detail_link,
            "pdf_url": pdf_url,
            "db_type": "MDPI",
            "extra": {
                "article_type": article_type,
                "meta": meta_text,
            },
        }

    @classmethod
    def _parse_search_html(cls, html: str, *, limit: int, offset: int, source_type: str = "all", journal: str = None, start_year: int = None, end_year: int = None) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        total = "0"
        text = soup.get_text(" ", strip=True)
        match = re.search(r"Search Results\s*\(([\d,]+)\)", text)
        if match:
            total = match.group(1).replace(",", "")

        papers: List[Dict] = []
        for item in soup.select(".article-item"):
            parsed = cls._parse_search_item(
                item,
                source_type=source_type,
                journal=journal,
                start_year=start_year,
                end_year=end_year,
            )
            if parsed:
                papers.append(parsed)
        if offset:
            papers = papers[offset:]
        return {"total_results": total, "papers": papers[: max(0, int(limit or 10))]}

    async def search_papers(
        self,
        query: str,
        search_field: str = "all",
        db_scope: str = "",
        source_type: str = "all",
        journal: str = None,
        start_year: int = None,
        end_year: int = None,
        sort_by: str = "relevance",
        start_index: int = 0,
        limit: int = 10,
    ) -> Dict:
        page_count = 10
        start_index = max(0, int(start_index or 0))
        page_no = start_index // page_count + 1
        offset = start_index % page_count
        sort_map = {
            "relevance": "relevance",
            "date_desc": "pubdate",
            "citations": "cited",
            "views": "viewed",
            "downloads": "viewed",
        }
        sort = sort_map.get(str(sort_by or "relevance"), "relevance")
        params = {
            "sort": sort,
            "page_no": str(page_no),
            "page_count": str(page_count),
            "year_from": str(start_year or 1996),
            "year_to": str(end_year or datetime.datetime.now().year),
            "view": "default",
        }
        field = str(search_field or "all").strip().lower()
        if field in {"author", "authors", "au"}:
            params["authors"] = query
            params["q"] = ""
        else:
            params["q"] = query
        if journal:
            params["journal"] = str(journal)

        url = f"{self.BASE_URL}/search?{urllib.parse.urlencode(params)}"
        print(f"[MDPI] Direct search request: {url}")
        html = await self._fetch_html(url, referer=self.BASE_URL)
        return self._parse_search_html(
            html,
            limit=limit,
            offset=offset,
            source_type=source_type,
            journal=journal,
            start_year=start_year,
            end_year=end_year,
        )

    async def get_paper_details(self, detail_url: str) -> Dict[str, object]:
        html = await self._fetch_html(detail_url, referer=self.BASE_URL)
        return self._parse_detail_html(detail_url, html)

    async def _pdf_url_for(self, detail_url: str) -> str:
        path = self._detail_path(detail_url)
        return urllib.parse.urljoin(self.BASE_URL, path + "/pdf")

    async def download_paper(self, detail_url: str, output_dir: str) -> str:
        native_id = self._native_id_from_url(detail_url)
        if not native_id:
            native_id = self._safe_stem(detail_url)
        from scraper_utils import remember_downloaded_pdf, reuse_downloaded_pdf

        filename = f"mdpi_{self._safe_stem(native_id)}.pdf"
        reused = reuse_downloaded_pdf(self._pdf_cache, native_id, output_dir, filename=filename)
        if reused:
            return reused

        pdf_url = await self._pdf_url_for(detail_url)
        response = await self._request(
            "GET",
            pdf_url,
            referer=detail_url,
            accept="application/pdf,application/octet-stream;q=0.9,text/html;q=0.8,*/*;q=0.7",
        )
        content = response.content or b""
        if not content.startswith(b"%PDF"):
            return (
                "Error: MDPI PDF endpoint did not return a PDF "
                f"(HTTP {response.status_code}, content-type={response.headers.get('content-type', '')})."
            )
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.abspath(os.path.join(output_dir, filename))
        with open(path, "wb") as fh:
            fh.write(content)
        remember_downloaded_pdf(self._pdf_cache, native_id, path)
        print(f"[MDPI] Downloaded PDF via direct HTTP: {path}")
        return path

    async def read_paper_content(self, detail_url: str, output_dir: str):
        pdf_path = await self.download_paper(detail_url, output_dir)
        if not isinstance(pdf_path, str) or not os.path.exists(pdf_path):
            return pdf_path
        from pdf_utils import convert_pdf_to_markdown

        converted = await asyncio.to_thread(convert_pdf_to_markdown, pdf_path, output_dir)
        return converted.md_path, converted.preview

    async def fetch_ris(self, detail_url: str) -> str:
        html = await self._fetch_html(detail_url, referer=self.BASE_URL)
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", attrs={"name": "export-ris"})
        if not form:
            return ""
        data = {}
        for inp in form.select("input[name]"):
            name = inp.get("name")
            value = inp.get("value") or ""
            if name:
                data[name] = value
        if not data.get("articles_ids[]"):
            return ""
        action = urllib.parse.urljoin(self.BASE_URL, form.get("action") or "/export")
        response = await self._request(
            "POST",
            action,
            referer=detail_url,
            accept="text/plain,*/*;q=0.8",
            data=data,
        )
        text = response.text or ""
        if response.status_code == 200 and "TY  -" in text:
            return text
        return ""

    async def close(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None


scraper_instance = MDPIScraper()
