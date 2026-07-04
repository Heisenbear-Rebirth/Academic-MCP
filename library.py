"""Local academic library: MongoDB-backed metadata + canonical file repository.

Design summary:
- Identity per (platform, native_id). Native ID is extracted from the detail URL.
- All downloaded PDFs / generated Markdown live under {library_root}/{platform}/{safe_id}/.
- On cache miss, the scraper writes into the canonical dir; on hit, files are mirrored to the caller's
  requested output_dir so the existing MCP contract (returns paths under output_dir) is preserved.
- If MongoDB is unreachable, the library degrades to a no-op (transparent pass-through to scrapers).
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import pymongo
    from pymongo import MongoClient, ReturnDocument
    from pymongo.errors import PyMongoError
except Exception:  # pragma: no cover - import-time guard
    pymongo = None
    MongoClient = None
    ReturnDocument = None
    PyMongoError = Exception

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


def _aiaa_native_id(url: str) -> str:
    decoded = urllib.parse.unquote(url or "")
    match = re.search(r"(?:/doi/(?:abs/|full/|pdf/|epdf/|reader/)?)?(10\.2514/[^/?#\s\"'<>]+)", decoded, re.IGNORECASE)
    return match.group(1).rstrip(".") if match else ""


def _mdpi_native_id(url: str) -> str:
    decoded = urllib.parse.unquote(url or "")
    doi = re.search(r"(10\.3390/[A-Za-z0-9._;()/:-]+)", decoded, re.IGNORECASE)
    if doi:
        return doi.group(1).rstrip(").,;")
    parsed = urllib.parse.urlparse(decoded)
    path = (parsed.path or "").strip("/")
    path = re.sub(r"/(?:pdf|htm|xml)(?:/)?$", "", path)
    match = re.match(r"([0-9]{4}-[0-9]{3,4}X?/\d+/\d+/[^/?#]+)", path, re.IGNORECASE)
    return match.group(1) if match else ""


def _wos_native_id(url: str) -> str:
    # UT accession for any WoS collection (WOS:, MEDLINE:, CCC:, ...). Must stay
    # consistent with wos_scraper._wos_id_from_url so the cache key equals the UT.
    decoded = urllib.parse.unquote(url or "")
    match = re.search(r"/full-record/([^/?#]+)", decoded)
    if match:
        return match.group(1).strip()
    match = re.search(r"([A-Za-z]{2,}:[A-Za-z0-9._-]+)", decoded)
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
    "AIAA": _aiaa_native_id,
    "MDPI": _mdpi_native_id,
    "WOS": _wos_native_id,
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
# MongoDB connection management
# ---------------------------------------------------------------------------

class _LibraryUnavailable(Exception):
    pass


class Library:
    """MongoDB + filesystem academic cache.

    The first call to :meth:`ensure_ready` connects to MongoDB, verifies the
    indexes and the file root. If initialisation fails (no driver, no .env,
    Mongo down, etc.) the library is marked disabled and every method becomes a
    graceful no-op.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._disabled_reason: Optional[str] = None
        self._client = None
        self._db = None

    # -- Lifecycle ------------------------------------------------------

    def _load_mongo_config(self) -> Tuple[str, str]:
        """Resolve (connection_uri, database_name) from the environment.

        A full ``MONGODB_URI`` takes precedence; otherwise a URI is assembled
        from discrete host/port/user/password vars so simple local setups only
        need ``MONGODB_HOST``.
        """
        if not pymongo:
            raise _LibraryUnavailable("pymongo is not installed. Run `pip install -r requirements.txt`.")
        uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
        host = os.getenv("MONGODB_HOST") or os.getenv("MONGO_HOST")
        if not uri and not host:
            raise _LibraryUnavailable(
                "MONGODB_URI (or MONGODB_HOST) is not set. Copy .env.example to .env and fill in credentials."
            )
        if not uri:
            port = int(os.getenv("MONGODB_PORT", os.getenv("MONGO_PORT", "27017")))
            user = os.getenv("MONGODB_USER") or os.getenv("MONGO_USER")
            password = os.getenv("MONGODB_PASSWORD") or os.getenv("MONGO_PASSWORD")
            if user:
                cred = f"{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password or '')}@"
            else:
                cred = ""
            uri = f"mongodb://{cred}{host}:{port}"
        db_name = os.getenv("MONGODB_DATABASE") or os.getenv("MONGO_DATABASE") or "academic_mcp"
        return uri, db_name

    def _ensure_indexes(self) -> None:
        # Collections and documents are created lazily on first write; we only
        # need to pin the uniqueness + lookup indexes that back the DAO.
        # JSON-shaped fields (keywords, extra, results, filters, cookies,
        # fingerprint) are stored as native BSON documents/arrays -- no more
        # MEDIUMTEXT round-tripping.
        papers = self._db.papers
        papers.create_index([("platform", 1), ("native_id", 1)], unique=True, name="uniq_platform_native")
        papers.create_index([("id", 1)], name="paper_id")
        papers.create_index([("updated_at", -1)], name="paper_updated")
        searches = self._db.search_queries
        searches.create_index([("platform", 1), ("query_hash", 1)], unique=True, name="uniq_platform_hash")
        searches.create_index([("id", 1)], name="search_id")
        searches.create_index([("fetched_at", -1)], name="search_fetched")
        # browser_state is keyed by _id == platform, so it needs no extra index.

    def _next_id(self, name: str) -> int:
        """Emulate a SQL AUTO_INCREMENT sequence via an atomic counters doc.

        Used to keep the integer ``id`` the web UI relies on (paper export,
        search deletion) stable and human-friendly. Gaps are impossible because
        the counter is only advanced on genuine inserts.
        """
        doc = self._db.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["seq"])

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
                uri, db_name = self._load_mongo_config()
                client = MongoClient(uri, serverSelectionTimeoutMS=3000, tz_aware=False)
                client.admin.command("ping")  # fail fast if Mongo is unreachable
                self._client = client
                self._db = client[db_name]
                self._ensure_indexes()
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
            print("[Library] Ready. Mongo indexes verified, file root at:", library_root_path())
            return True

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
        decoded.pop("_id", None)
        # keywords/extra are normally native BSON now; the str/bytes path only
        # matters for values that were persisted as encoded JSON.
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
        row = self._db.papers.find_one(
            {"platform": (platform or "").upper(), "native_id": native_id}
        )
        return self._row_to_paper(row) if row else None

    _PAPER_COLS = (
        "title", "author", "source", "venue_name", "pub_date", "db_type", "doi",
        "detail_link", "abstract", "keywords", "pdf_path", "md_path",
        "images_dir", "extra", "ris_text",
    )

    def upsert_paper(self, platform: str, native_id: str, **fields: Any) -> None:
        if not self.enabled or not native_id:
            return
        platform = (platform or "").upper()
        # keywords/extra are stored as native BSON documents/arrays.
        set_fields = {
            col: fields[col]
            for col in self._PAPER_COLS
            if col in fields and fields[col] is not None
        }
        now = datetime.now()
        update = {
            "$set": {**set_fields, "updated_at": now},
            "$setOnInsert": {"platform": platform, "native_id": native_id, "created_at": now},
        }
        res = self._db.papers.update_one(
            {"platform": platform, "native_id": native_id}, update, upsert=True
        )
        if res.upserted_id is not None:
            try:
                self._db.papers.update_one(
                    {"_id": res.upserted_id}, {"$set": {"id": self._next_id("papers")}}
                )
            except PyMongoError:
                pass

    # -- Search cache -------------------------------------------------

    def get_search(self, platform: str, query_hash: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        row = self._db.search_queries.find_one(
            {"platform": (platform or "").upper(), "query_hash": query_hash},
            {"total_results": 1, "results": 1},
        )
        if not row:
            return None
        results = row.get("results")
        if isinstance(results, (bytes, bytearray)):
            results = results.decode("utf-8", errors="replace")
        if isinstance(results, str):  # defensive: encoded-JSON legacy payloads
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
        platform = (platform or "").upper()
        now = datetime.now()
        # filters/results are stored as native BSON, not JSON strings.
        update = {
            "$set": {
                "query_text": query_text,
                "filters": filters or {},
                "total_results": str(total_results) if total_results is not None else "0",
                "results": papers or [],
                "fetched_at": now,
            },
            "$setOnInsert": {"platform": platform, "query_hash": query_hash},
        }
        res = self._db.search_queries.update_one(
            {"platform": platform, "query_hash": query_hash}, update, upsert=True
        )
        if res.upserted_id is not None:
            try:
                self._db.search_queries.update_one(
                    {"_id": res.upserted_id}, {"$set": {"id": self._next_id("search_queries")}}
                )
            except PyMongoError:
                pass

    # -- Listing / management (for the web UI) -----------------------

    @staticmethod
    def _nonempty(field: str) -> Dict[str, Any]:
        """Aggregation expr: 1 when *field* is present and a non-empty string.

        ``$ifNull`` coalesces both a missing field and an explicit null to "",
        so the single ``$ne`` cleanly rejects absent/empty values (an aggregation
        ``$ne`` against a bare field reference does NOT reliably treat a missing
        field as null).
        """
        return {"$cond": [{"$ne": [{"$ifNull": [f"${field}", ""]}, ""]}, 1, 0]}

    def stats(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "platforms": [], "totals": {"papers": 0, "searches": 0}}
        pipeline = [
            {
                "$group": {
                    "_id": "$platform",
                    "c": {"$sum": 1},
                    "pdfs": {"$sum": self._nonempty("pdf_path")},
                    "mds": {"$sum": self._nonempty("md_path")},
                }
            },
            {"$sort": {"c": -1}},
        ]
        platforms = [
            {"platform": r["_id"], "c": r["c"], "pdfs": r["pdfs"], "mds": r["mds"]}
            for r in self._db.papers.aggregate(pipeline)
        ]
        papers_total = self._db.papers.count_documents({})
        searches_total = self._db.search_queries.count_documents({})
        return {
            "enabled": True,
            "platforms": platforms,
            "totals": {"papers": papers_total, "searches": searches_total},
        }

    # Recognised values for the ``state`` filter on :meth:`list_papers`. Each maps
    # to a list of Mongo query clauses ANDed into the overall filter. Note that a
    # Mongo ``$in: [None, ""]`` query also matches a *missing* field (treated as
    # null), and ``$nin`` correspondingly excludes it -- matching the SQL
    # NULL/'' semantics of the original schema.
    _STATE_CLAUSES = {
        "search_only": [{"abstract": {"$in": [None, ""]}}, {"pdf_path": {"$in": [None, ""]}}],
        "with_abstract": [{"abstract": {"$nin": [None, ""]}}],
        "with_pdf": [{"pdf_path": {"$nin": [None, ""]}}],
        "with_md": [{"md_path": {"$nin": [None, ""]}}],
        "with_ris": [{"ris_text": {"$nin": [None, ""]}}],
    }

    _LIST_PROJECTION = {
        "id": 1, "platform": 1, "native_id": 1, "title": 1, "author": 1,
        "pub_date": 1, "db_type": 1, "abstract": 1, "doi": 1, "venue_name": 1,
        "pdf_path": 1, "md_path": 1, "ris_text": 1, "detail_link": 1, "updated_at": 1,
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
        clauses: List[Dict[str, Any]] = []
        if platform:
            clauses.append({"platform": platform.upper()})
        if keyword:
            rx = {"$regex": re.escape(keyword), "$options": "i"}
            clauses.append({"$or": [{"title": rx}, {"author": rx}, {"native_id": rx}]})
        clauses.extend(self._STATE_CLAUSES.get((state or "").strip(), []))
        query: Dict[str, Any] = {"$and": clauses} if clauses else {}
        offset = max(0, (page - 1) * page_size)
        total = self._db.papers.count_documents(query)
        cursor = (
            self._db.papers.find(query, self._LIST_PROJECTION)
            .sort("updated_at", -1)
            .skip(offset)
            .limit(page_size)
        )
        rows = []
        for r in cursor:
            row = dict(r)
            row.pop("_id", None)
            # Flag columns make templating concise without leaking blob payloads.
            row["has_abstract"] = bool((row.get("abstract") or "").strip())
            row["has_ris"] = bool((row.pop("ris_text", None) or "").strip())
            rows.append(row)
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}

    def get_paper_ris(self, platform: str, native_id: str) -> Optional[str]:
        if not self.enabled or not native_id:
            return None
        row = self._db.papers.find_one(
            {"platform": (platform or "").upper(), "native_id": native_id},
            {"ris_text": 1},
        )
        return (row or {}).get("ris_text")

    def set_paper_ris(self, platform: str, native_id: str, ris_text: str) -> bool:
        if not self.enabled or not native_id or not ris_text:
            return False
        res = self._db.papers.update_one(
            {"platform": (platform or "").upper(), "native_id": native_id},
            {"$set": {"ris_text": ris_text, "updated_at": datetime.now()}},
        )
        return res.matched_count > 0

    def fetch_papers_for_export(self, paper_ids: List[int]) -> List[Dict[str, Any]]:
        """Pull full rows (including ris_text + abstract) for a batch of paper PKs."""
        if not self.enabled or not paper_ids:
            return []
        ids = [int(i) for i in paper_ids]
        cursor = self._db.papers.find({"id": {"$in": ids}}).sort(
            [("platform", 1), ("updated_at", -1)]
        )
        return [self._row_to_paper(r) for r in cursor]

    def list_searches(self, page: int = 1, page_size: int = 30) -> Dict[str, Any]:
        if not self.enabled:
            return {"rows": [], "total": 0, "page": page, "page_size": page_size}
        offset = max(0, (page - 1) * page_size)
        total = self._db.search_queries.count_documents({})
        projection = {
            "id": 1, "platform": 1, "query_hash": 1, "query_text": 1,
            "filters": 1, "total_results": 1, "fetched_at": 1,
        }
        cursor = (
            self._db.search_queries.find({}, projection)
            .sort("fetched_at", -1)
            .skip(offset)
            .limit(page_size)
        )
        rows = []
        for r in cursor:
            row = dict(r)
            row.pop("_id", None)
            filters = row.get("filters")
            if isinstance(filters, (bytes, bytearray)):
                filters = filters.decode("utf-8", errors="replace")
            if isinstance(filters, str):  # defensive: encoded-JSON legacy payloads
                try:
                    row["filters"] = json.loads(filters)
                except Exception:
                    pass
            rows.append(row)
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}

    def delete_paper(self, platform: str, native_id: str, *, remove_files: bool = False) -> bool:
        if not self.enabled or not native_id:
            return False
        res = self._db.papers.delete_one(
            {"platform": (platform or "").upper(), "native_id": native_id}
        )
        deleted = res.deleted_count > 0
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
        res = self._db.search_queries.delete_one({"id": int(search_id)})
        return res.deleted_count > 0

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
        row = self._db.browser_state.find_one({"_id": (platform or "").upper()})
        if not row:
            return None
        return {
            "platform": row.get("_id"),
            "fingerprint": self._decode_json_field(row.get("fingerprint"), None),
            "cookies": self._decode_json_field(row.get("cookies"), []),
            "user_agent": row.get("user_agent"),
            "verified_at": row.get("verified_at"),
            "updated_at": row.get("updated_at"),
            "note": row.get("note"),
        }

    def save_browser_fingerprint(self, platform: str, fingerprint: Dict[str, Any], user_agent: str = None) -> None:
        if not self.enabled:
            return
        set_doc: Dict[str, Any] = {"fingerprint": fingerprint, "updated_at": datetime.now()}
        if user_agent is not None:  # COALESCE: only overwrite when a new UA is supplied
            set_doc["user_agent"] = user_agent
        self._db.browser_state.update_one(
            {"_id": (platform or "").upper()}, {"$set": set_doc}, upsert=True
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
        now = datetime.now()
        set_doc: Dict[str, Any] = {"cookies": cookies or [], "updated_at": now}
        if user_agent is not None:  # COALESCE semantics
            set_doc["user_agent"] = user_agent
        if note is not None:
            set_doc["note"] = note
        if mark_verified:
            set_doc["verified_at"] = now
        self._db.browser_state.update_one(
            {"_id": (platform or "").upper()}, {"$set": set_doc}, upsert=True
        )

    def clear_browser_state(self, platform: str, *, keep_fingerprint: bool = True) -> bool:
        if not self.enabled:
            return False
        platform_u = (platform or "").upper()
        if keep_fingerprint:
            res = self._db.browser_state.update_one(
                {"_id": platform_u},
                {"$set": {"cookies": None, "verified_at": None,
                          "updated_at": datetime.now(), "note": "cookies cleared"}},
            )
            return res.matched_count > 0
        res = self._db.browser_state.delete_one({"_id": platform_u})
        return res.deleted_count > 0

    def list_browser_states(self) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        rows = []
        for r in self._db.browser_state.find({}).sort("_id", 1):
            rows.append({
                "platform": r.get("_id"),
                "user_agent": r.get("user_agent"),
                "verified_at": r.get("verified_at"),
                "updated_at": r.get("updated_at"),
                "note": r.get("note"),
                "has_fingerprint": bool(r.get("fingerprint")),
                "has_cookies": bool(r.get("cookies")),
            })
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
