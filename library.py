"""Local academic library: MySQL-backed metadata + canonical file repository.

Design summary:
- Identity per (platform, native_id). Native ID is extracted from the detail URL.
- All downloaded PDFs / generated Markdown live under {library_root}/{platform}/{safe_id}/.
- On cache miss, the scraper writes into the canonical dir; on hit, files are mirrored to the caller's
  requested output_dir so the existing MCP contract (returns paths under output_dir) is preserved.
- If MySQL is unreachable, the library degrades to a no-op (transparent pass-through to scrapers).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import pymysql
    from pymysql.cursors import DictCursor
except Exception:  # pragma: no cover - import-time guard
    pymysql = None
    DictCursor = None

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

from mcp_logging import safe_stderr_print
from runtime_config import library_enabled, library_root_path, project_path

print = safe_stderr_print

if load_dotenv is not None:
    env_path = project_path(".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)


# ---------------------------------------------------------------------------
# Native-ID extraction
# ---------------------------------------------------------------------------

def _qs(url: str) -> Dict[str, str]:
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        return {k.lower(): v[0] for k, v in qs.items() if v}
    except Exception:
        return {}


def _cnki_native_id(url: str) -> str:
    params = _qs(url)
    dbcode = params.get("dbcode") or params.get("dbname") or ""
    filename = params.get("filename") or params.get("fn") or ""
    if dbcode and filename:
        return f"{dbcode}|{filename}"
    if filename:
        return filename
    return ""


def _ieee_native_id(url: str) -> str:
    match = re.search(r"/document/(\d+)", url)
    return match.group(1) if match else ""


def _arxiv_native_id(url: str) -> str:
    match = re.search(r"/abs/([^/?#]+)", url)
    return match.group(1) if match else ""


def _acm_native_id(url: str) -> str:
    # Delegate so bare-ID URLs (`/doi/3447928.3456707`) still resolve to the
    # canonical 10.1145/... native_id used as the cache primary key.
    from scraper_utils import normalize_acm_doi
    return normalize_acm_doi(url)


def _sd_native_id(url: str) -> str:
    match = re.search(r"/pii/([A-Za-z0-9]+)", url)
    return match.group(1) if match else ""


def _gs_native_id(url: str) -> str:
    # Google Scholar links are external. Fall back to URL hash.
    if not url or url == "N/A":
        return ""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]


def _patyee_native_id(url: str) -> str:
    params = _qs(url)
    return params.get("pn") or ""


def _dawei_native_id(url: str) -> str:
    params = _qs(url)
    return params.get("pnm") or params.get("an") or ""


_EXTRACTORS = {
    "CNKI": _cnki_native_id,
    "IEEE": _ieee_native_id,
    "ARXIV": _arxiv_native_id,
    "ACM": _acm_native_id,
    "SD": _sd_native_id,
    "GS": _gs_native_id,
    "PATYEE": _patyee_native_id,
    "DAWEI": _dawei_native_id,
    "DAWEISOFT": _dawei_native_id,
    "PAT_DAWEI": _dawei_native_id,
}


def extract_native_id(platform: str, url: str) -> str:
    if not url:
        return ""
    extractor = _EXTRACTORS.get((platform or "").upper())
    if not extractor:
        return ""
    try:
        return extractor(url) or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAFE_FS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(native_id: str) -> str:
    """Filesystem-safe slug for a native id. Long/unsafe ids fall back to hash."""
    if not native_id:
        return "_unknown"
    candidate = _SAFE_FS.sub("_", native_id).strip("._")
    if not candidate or len(candidate) > 64:
        digest = hashlib.md5(native_id.encode("utf-8")).hexdigest()
        return f"id_{digest[:16]}"
    return candidate


def _normalize_for_hash(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    return value


def compute_query_hash(platform: str, query: str, filters: Dict[str, Any]) -> str:
    payload = {
        "p": (platform or "").upper(),
        "q": (query or "").strip().lower(),
        "f": {k: _normalize_for_hash(v) for k, v in sorted(filters.items())},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


# Search caches are permanent by design, so a poisoned response (an anti-bot
# soft-block that still echoes a result count but ships zero rows, or an
# explicit CAPTCHA / IP-ban sentinel) must NOT be written -- otherwise every
# later call returns the fossilized empty result. A genuine zero-hit query is
# fine to cache: it reports total 0 (or a non-numeric "unknown") with no rows.
_BLOCK_TOTALS = {"CAPTCHA (403)", "IP_BANNED"}


def search_result_is_cacheable(results: Any) -> bool:
    if not isinstance(results, dict):
        return False
    papers = results.get("papers") or []
    total_raw = str(results.get("total_results", "")).strip()
    if total_raw in _BLOCK_TOTALS:
        return False
    if any(isinstance(p, dict) and str(p.get("id", "")).startswith("err") for p in papers):
        return False
    if papers:
        return True
    # No rows: only cacheable if the platform genuinely reported zero matches.
    digits = re.sub(r"[,\s]", "", total_raw)
    if digits.isdigit() and int(digits) > 0:
        # Count says there ARE matches but we parsed none -> soft block.
        return False
    return True


# ---------------------------------------------------------------------------
# MySQL connection management
# ---------------------------------------------------------------------------

class _LibraryUnavailable(Exception):
    pass


class Library:
    """MySQL + filesystem academic cache.

    The first call to :meth:`ensure_ready` initialises the schema and the file root.
    If initialisation fails (no driver, no .env, MySQL down, etc.) the library is
    marked disabled and every method becomes a graceful no-op.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._disabled_reason: Optional[str] = None
        self._conn_kwargs: Optional[Dict[str, Any]] = None

    # -- Lifecycle ------------------------------------------------------

    def _load_conn_kwargs(self) -> Dict[str, Any]:
        if not pymysql:
            raise _LibraryUnavailable("PyMySQL is not installed. Run `pip install -r requirements.txt`.")
        host = os.getenv("MYSQL_HOST")
        if not host:
            raise _LibraryUnavailable("MYSQL_HOST is not set. Copy .env.example to .env and fill in credentials.")
        return {
            "host": host,
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", "root"),
            "password": os.getenv("MYSQL_PASSWORD", ""),
            "database": os.getenv("MYSQL_DATABASE", "academic_mcp"),
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": DictCursor,
        }

    def _ensure_database(self) -> None:
        """Create the target database if it does not exist."""
        kwargs = dict(self._conn_kwargs)  # type: ignore[arg-type]
        db_name = kwargs.pop("database")
        conn = pymysql.connect(**kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        # Schema is intentionally portable down to MySQL 5.5: MEDIUMTEXT instead of JSON,
        # ASCII charset on indexed identifier columns (so the unique key stays inside
        # InnoDB's 767-byte prefix limit), and DATETIME columns filled via NOW() in DML.
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS papers (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        platform VARCHAR(32) NOT NULL,
                        native_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                        title TEXT,
                        author TEXT,
                        source TEXT,
                        venue_name VARCHAR(512),
                        pub_date VARCHAR(64),
                        db_type VARCHAR(64),
                        doi VARCHAR(255),
                        detail_link TEXT,
                        abstract MEDIUMTEXT,
                        keywords MEDIUMTEXT,
                        pdf_path VARCHAR(512),
                        md_path VARCHAR(512),
                        images_dir VARCHAR(512),
                        extra MEDIUMTEXT,
                        ris_text MEDIUMTEXT,
                        created_at DATETIME NULL,
                        updated_at DATETIME NULL,
                        UNIQUE KEY uniq_platform_native (platform, native_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                # Defensive migrations for installations that pre-date later columns.
                for ddl in (
                    "ALTER TABLE papers ADD COLUMN venue_name VARCHAR(512) AFTER source",
                    "ALTER TABLE papers ADD COLUMN ris_text MEDIUMTEXT AFTER extra",
                ):
                    try:
                        cur.execute(ddl)
                    except pymysql.err.OperationalError as e:
                        if e.args[0] != 1060:  # 1060 = Duplicate column name (already migrated)
                            raise
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_queries (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        platform VARCHAR(32) NOT NULL,
                        query_hash CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                        query_text TEXT,
                        filters MEDIUMTEXT,
                        total_results VARCHAR(64),
                        results MEDIUMTEXT,
                        fetched_at DATETIME NULL,
                        UNIQUE KEY uniq_platform_hash (platform, query_hash)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                # Shared browser auth state so N concurrent MCP clients can reuse one
                # manual verification: pinned Camoufox fingerprint + cf_clearance/DataDome
                # cookies live here, not in any single profile dir.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS browser_state (
                        platform VARCHAR(32) NOT NULL PRIMARY KEY,
                        fingerprint MEDIUMTEXT,
                        cookies MEDIUMTEXT,
                        user_agent VARCHAR(512),
                        verified_at DATETIME NULL,
                        updated_at DATETIME NULL,
                        note VARCHAR(255)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )

    def ensure_ready(self) -> bool:
        if self._ready or self._disabled_reason:
            return self._ready
        with self._lock:
            if self._ready or self._disabled_reason:
                return self._ready
            if not library_enabled():
                self._disabled_reason = "library_enabled is false in mcp_runtime_config.json"
                print(f"[Library] Disabled: {self._disabled_reason}")
                return False
            try:
                self._conn_kwargs = self._load_conn_kwargs()
                self._ensure_database()
                self._ensure_schema()
                library_root_path().mkdir(parents=True, exist_ok=True)
            except _LibraryUnavailable as e:
                self._disabled_reason = str(e)
                print(f"[Library] Disabled: {self._disabled_reason}")
                return False
            except Exception as e:
                self._disabled_reason = f"{type(e).__name__}: {e}"
                print(f"[Library] Disabled after init failure: {self._disabled_reason}")
                return False
            self._ready = True
            print("[Library] Ready. Schema verified, file root at:", library_root_path())
            return True

    def _connect(self):
        assert self._conn_kwargs is not None
        return pymysql.connect(**self._conn_kwargs)

    @property
    def enabled(self) -> bool:
        return self._ready and not self._disabled_reason

    # -- Filesystem ---------------------------------------------------

    def canonical_dir(self, platform: str, native_id: str) -> Path:
        root = library_root_path()
        return root / (platform or "").upper() / _safe_segment(native_id)

    @staticmethod
    def _mirror_file(src: Path, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if src.resolve() == dest.resolve():
            return dest
        shutil.copy2(src, dest)
        return dest

    @staticmethod
    def _mirror_tree(src_dir: Path, dest_dir: Path) -> None:
        if not src_dir.exists():
            return
        dest_dir.mkdir(parents=True, exist_ok=True)
        for entry in src_dir.iterdir():
            target = dest_dir / entry.name
            if entry.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)

    def mirror_pdf_to(self, canonical_pdf: Path, output_dir: str) -> str:
        out = Path(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        if Path(canonical_pdf).parent.resolve() == out:
            return str(canonical_pdf)
        mirrored = self._mirror_file(Path(canonical_pdf), out)
        return str(mirrored)

    def mirror_markdown_to(self, canonical_md: Path, output_dir: str) -> str:
        """Copy the markdown plus its sibling images/ folder to *output_dir*."""
        out = Path(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        canon_md = Path(canonical_md)
        if canon_md.parent.resolve() == out:
            return str(canon_md)
        mirrored = self._mirror_file(canon_md, out)
        images_src = canon_md.parent / "images"
        if images_src.exists():
            self._mirror_tree(images_src, out / "images")
        return str(mirrored)

    # -- Paper DAO ----------------------------------------------------

    @staticmethod
    def _row_to_paper(row: Dict[str, Any]) -> Dict[str, Any]:
        if not row:
            return {}
        decoded = dict(row)
        for json_field in ("keywords", "extra"):
            value = decoded.get(json_field)
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8", errors="replace")
            if isinstance(value, str):
                try:
                    decoded[json_field] = json.loads(value)
                except Exception:
                    pass
        return decoded

    def get_paper(self, platform: str, native_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled or not native_id:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM papers WHERE platform=%s AND native_id=%s LIMIT 1",
                    ((platform or "").upper(), native_id),
                )
                row = cur.fetchone()
        return self._row_to_paper(row) if row else None

    _PAPER_COLS = (
        "title", "author", "source", "venue_name", "pub_date", "db_type", "doi",
        "detail_link", "abstract", "keywords", "pdf_path", "md_path",
        "images_dir", "extra", "ris_text",
    )

    def upsert_paper(self, platform: str, native_id: str, **fields: Any) -> None:
        if not self.enabled or not native_id:
            return
        payload = {"platform": (platform or "").upper(), "native_id": native_id}
        for col in self._PAPER_COLS:
            if col in fields and fields[col] is not None:
                value = fields[col]
                if col in ("keywords", "extra") and not isinstance(value, str):
                    value = json.dumps(value, ensure_ascii=False)
                payload[col] = value
        cols = list(payload.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        updates = ", ".join(
            f"{c}=VALUES({c})" for c in cols if c not in ("platform", "native_id")
        )
        update_clause = f"{updates}, updated_at=NOW()" if updates else "updated_at=NOW()"
        sql = (
            f"INSERT INTO papers ({', '.join(cols)}, created_at, updated_at) "
            f"VALUES ({placeholders}, NOW(), NOW()) "
            f"ON DUPLICATE KEY UPDATE {update_clause}"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [payload[c] for c in cols])

    # -- Search cache -------------------------------------------------

    def get_search(self, platform: str, query_hash: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT total_results, results FROM search_queries "
                    "WHERE platform=%s AND query_hash=%s LIMIT 1",
                    ((platform or "").upper(), query_hash),
                )
                row = cur.fetchone()
        if not row:
            return None
        results = row.get("results")
        if isinstance(results, (bytes, bytearray)):
            results = results.decode("utf-8", errors="replace")
        if isinstance(results, str):
            try:
                results = json.loads(results)
            except Exception:
                results = []
        return {
            "total_results": row.get("total_results") or "0",
            "papers": results or [],
        }

    def save_search(
        self,
        platform: str,
        query_hash: str,
        query_text: str,
        filters: Dict[str, Any],
        total_results: Any,
        papers: List[Dict[str, Any]],
    ) -> None:
        if not self.enabled:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO search_queries
                        (platform, query_hash, query_text, filters, total_results, results, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON DUPLICATE KEY UPDATE
                        query_text=VALUES(query_text),
                        filters=VALUES(filters),
                        total_results=VALUES(total_results),
                        results=VALUES(results),
                        fetched_at=NOW()
                    """,
                    (
                        (platform or "").upper(),
                        query_hash,
                        query_text,
                        json.dumps(filters, ensure_ascii=False, sort_keys=True),
                        str(total_results) if total_results is not None else "0",
                        json.dumps(papers or [], ensure_ascii=False),
                    ),
                )

    # -- Listing / management (for the web UI) -----------------------

    def stats(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "platforms": [], "totals": {"papers": 0, "searches": 0}}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT platform, COUNT(*) AS c, "
                    "SUM(CASE WHEN pdf_path IS NOT NULL AND pdf_path<>'' THEN 1 ELSE 0 END) AS pdfs, "
                    "SUM(CASE WHEN md_path IS NOT NULL AND md_path<>'' THEN 1 ELSE 0 END) AS mds "
                    "FROM papers GROUP BY platform ORDER BY c DESC"
                )
                platforms = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*) AS c FROM papers")
                papers_total = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM search_queries")
                searches_total = cur.fetchone()["c"]
        return {
            "enabled": True,
            "platforms": platforms,
            "totals": {"papers": papers_total, "searches": searches_total},
        }

    # Recognised values for the ``state`` filter on :meth:`list_papers`.
    # Each maps to a SQL fragment that selects the matching papers.
    _STATE_FILTERS = {
        "search_only": "(abstract IS NULL OR abstract='') AND (pdf_path IS NULL OR pdf_path='')",
        "with_abstract": "(abstract IS NOT NULL AND abstract<>'')",
        "with_pdf": "(pdf_path IS NOT NULL AND pdf_path<>'')",
        "with_md": "(md_path IS NOT NULL AND md_path<>'')",
        "with_ris": "(ris_text IS NOT NULL AND ris_text<>'')",
    }

    def list_papers(
        self,
        platform: Optional[str] = None,
        keyword: Optional[str] = None,
        state: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"rows": [], "total": 0, "page": page, "page_size": page_size}
        where, params = [], []
        if platform:
            where.append("platform=%s")
            params.append(platform.upper())
        if keyword:
            where.append("(title LIKE %s OR author LIKE %s OR native_id LIKE %s)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        state_sql = self._STATE_FILTERS.get((state or "").strip())
        if state_sql:
            where.append(state_sql)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        offset = max(0, (page - 1) * page_size)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS c FROM papers {clause}", params)
                total = cur.fetchone()["c"]
                cur.execute(
                    f"SELECT id, platform, native_id, title, author, pub_date, db_type, "
                    f"abstract, doi, venue_name, pdf_path, md_path, ris_text, detail_link, "
                    f"updated_at FROM papers {clause} "
                    f"ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                    [*params, page_size, offset],
                )
                rows = []
                for r in cur.fetchall():
                    row = dict(r)
                    # Flag columns make templating concise without leaking blob payloads.
                    row["has_abstract"] = bool((row.get("abstract") or "").strip())
                    row["has_ris"] = bool((row.pop("ris_text", None) or "").strip())
                    rows.append(row)
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}

    def get_paper_ris(self, platform: str, native_id: str) -> Optional[str]:
        if not self.enabled or not native_id:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ris_text FROM papers WHERE platform=%s AND native_id=%s LIMIT 1",
                    ((platform or "").upper(), native_id),
                )
                row = cur.fetchone()
        return (row or {}).get("ris_text")

    def set_paper_ris(self, platform: str, native_id: str, ris_text: str) -> bool:
        if not self.enabled or not native_id or not ris_text:
            return False
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE papers SET ris_text=%s, updated_at=NOW() "
                    "WHERE platform=%s AND native_id=%s",
                    (ris_text, (platform or "").upper(), native_id),
                )
                return cur.rowcount > 0

    def fetch_papers_for_export(self, paper_ids: List[int]) -> List[Dict[str, Any]]:
        """Pull full rows (including ris_text + abstract) for a batch of paper PKs."""
        if not self.enabled or not paper_ids:
            return []
        placeholders = ",".join(["%s"] * len(paper_ids))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM papers WHERE id IN ({placeholders}) ORDER BY platform, updated_at DESC",
                    paper_ids,
                )
                rows = [self._row_to_paper(r) for r in cur.fetchall()]
        return rows

    def list_searches(self, page: int = 1, page_size: int = 30) -> Dict[str, Any]:
        if not self.enabled:
            return {"rows": [], "total": 0, "page": page, "page_size": page_size}
        offset = max(0, (page - 1) * page_size)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM search_queries")
                total = cur.fetchone()["c"]
                cur.execute(
                    "SELECT id, platform, query_hash, query_text, filters, total_results, fetched_at "
                    "FROM search_queries ORDER BY fetched_at DESC LIMIT %s OFFSET %s",
                    (page_size, offset),
                )
                rows = []
                for r in cur.fetchall():
                    row = dict(r)
                    filters = row.get("filters")
                    if isinstance(filters, (bytes, bytearray)):
                        filters = filters.decode("utf-8", errors="replace")
                    if isinstance(filters, str):
                        try:
                            row["filters"] = json.loads(filters)
                        except Exception:
                            pass
                    rows.append(row)
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}

    def delete_paper(self, platform: str, native_id: str, *, remove_files: bool = False) -> bool:
        if not self.enabled or not native_id:
            return False
        paper = self.get_paper(platform, native_id) or {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM papers WHERE platform=%s AND native_id=%s",
                    ((platform or "").upper(), native_id),
                )
                deleted = cur.rowcount > 0
        if deleted and remove_files:
            canon = self.canonical_dir(platform, native_id)
            try:
                if canon.exists():
                    shutil.rmtree(canon)
            except Exception as e:
                print(f"[Library] Failed to remove files for {platform}/{native_id}: {e}")
        return deleted

    def delete_search(self, search_id: int) -> bool:
        if not self.enabled:
            return False
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM search_queries WHERE id=%s", (int(search_id),))
                return cur.rowcount > 0

    # -- Shared browser auth state ------------------------------------

    @staticmethod
    def _decode_json_field(value: Any, default: Any):
        if value is None:
            return default
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return default
        return value

    def get_browser_state(self, platform: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT platform, fingerprint, cookies, user_agent, verified_at, "
                    "updated_at, note FROM browser_state WHERE platform=%s LIMIT 1",
                    ((platform or "").upper(),),
                )
                row = cur.fetchone()
        if not row:
            return None
        row = dict(row)
        row["fingerprint"] = self._decode_json_field(row.get("fingerprint"), None)
        row["cookies"] = self._decode_json_field(row.get("cookies"), [])
        return row

    def save_browser_fingerprint(self, platform: str, fingerprint: Dict[str, Any], user_agent: str = None) -> None:
        if not self.enabled:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO browser_state (platform, fingerprint, user_agent, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON DUPLICATE KEY UPDATE
                        fingerprint=VALUES(fingerprint),
                        user_agent=COALESCE(VALUES(user_agent), user_agent),
                        updated_at=NOW()
                    """,
                    (
                        (platform or "").upper(),
                        json.dumps(fingerprint, ensure_ascii=False),
                        user_agent,
                    ),
                )

    def save_browser_cookies(
        self,
        platform: str,
        cookies: List[Dict[str, Any]],
        *,
        mark_verified: bool = False,
        user_agent: str = None,
        note: str = None,
    ) -> None:
        if not self.enabled:
            return
        verified_clause = "verified_at=NOW()," if mark_verified else ""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO browser_state (platform, cookies, user_agent, {'verified_at,' if mark_verified else ''} updated_at, note)
                    VALUES (%s, %s, %s, {'NOW(),' if mark_verified else ''} NOW(), %s)
                    ON DUPLICATE KEY UPDATE
                        cookies=VALUES(cookies),
                        user_agent=COALESCE(VALUES(user_agent), user_agent),
                        {verified_clause}
                        updated_at=NOW(),
                        note=COALESCE(VALUES(note), note)
                    """,
                    (
                        (platform or "").upper(),
                        json.dumps(cookies or [], ensure_ascii=False),
                        user_agent,
                        note,
                    ),
                )

    def clear_browser_state(self, platform: str, *, keep_fingerprint: bool = True) -> bool:
        if not self.enabled:
            return False
        with self._connect() as conn:
            with conn.cursor() as cur:
                if keep_fingerprint:
                    cur.execute(
                        "UPDATE browser_state SET cookies=NULL, verified_at=NULL, "
                        "updated_at=NOW(), note='cookies cleared' WHERE platform=%s",
                        ((platform or "").upper(),),
                    )
                else:
                    cur.execute(
                        "DELETE FROM browser_state WHERE platform=%s",
                        ((platform or "").upper(),),
                    )
                return cur.rowcount > 0

    def list_browser_states(self) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT platform, user_agent, verified_at, updated_at, note, "
                    "CHAR_LENGTH(COALESCE(cookies,'')) AS cookies_len, "
                    "CHAR_LENGTH(COALESCE(fingerprint,'')) AS fp_len "
                    "FROM browser_state ORDER BY platform"
                )
                rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["has_fingerprint"] = bool(r.pop("fp_len", 0))
            r["has_cookies"] = bool(r.pop("cookies_len", 0))
        return rows

    # -- High-level helpers (async-friendly) --------------------------

    async def search_or_fetch(
        self,
        platform: str,
        query: str,
        filters: Dict[str, Any],
        fetch_coro_factory,
    ) -> Tuple[Dict[str, Any], bool]:
        """Return (results_dict, cache_hit). On miss, *fetch_coro_factory()* is awaited
        to produce the fresh results, which are then persisted.
        """
        if not self.enabled:
            results = await fetch_coro_factory()
            return results, False

        query_hash = compute_query_hash(platform, query, filters)
        cached = await asyncio.to_thread(self.get_search, platform, query_hash)
        if cached:
            return cached, True

        results = await fetch_coro_factory()
        # A soft-blocked / CAPTCHA'd response must never poison the permanent
        # cache. Return it to the caller so they see the failure, but don't
        # persist it -- the next call will retry the live fetch.
        if not search_result_is_cacheable(results):
            print(f"[Library] {platform} result not cacheable (likely soft-block); skipping persist.")
            return results, False
        # Persist search cache + each paper as a side effect.
        try:
            await asyncio.to_thread(
                self.save_search,
                platform,
                query_hash,
                query,
                filters,
                results.get("total_results") if isinstance(results, dict) else None,
                results.get("papers") if isinstance(results, dict) else [],
            )
            for paper in (results.get("papers") or []) if isinstance(results, dict) else []:
                native_id = extract_native_id(platform, paper.get("detail_link") or "")
                if not native_id:
                    continue
                # Forward per-platform "extra" hints (e.g. GS cluster_id needed
                # later by gs_scraper.fetch_ris). The upsert layer JSON-encodes
                # dicts for the extra column; passing None / empty dict skips.
                extra = None
                gs_cid = paper.get("_gs_cluster_id")
                if gs_cid:
                    extra = {"gs_cluster_id": gs_cid}
                await asyncio.to_thread(
                    self.upsert_paper,
                    platform,
                    native_id,
                    title=paper.get("title"),
                    author=paper.get("author"),
                    source=paper.get("source"),
                    venue_name=paper.get("venue_name"),
                    pub_date=paper.get("date"),
                    db_type=paper.get("db_type"),
                    doi=paper.get("doi"),
                    detail_link=paper.get("detail_link"),
                    abstract=paper.get("_abstract"),
                    extra=extra,
                )
        except Exception as e:
            print(f"[Library] Failed to persist search cache: {e}")
        return results, False


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_library_singleton: Optional[Library] = None
_singleton_lock = threading.Lock()


def get_library() -> Library:
    global _library_singleton
    if _library_singleton is None:
        with _singleton_lock:
            if _library_singleton is None:
                _library_singleton = Library()
    _library_singleton.ensure_ready()
    return _library_singleton
