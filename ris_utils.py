"""RIS citation synthesizer.

Produces a valid RIS record from the metadata we already store in the
``papers`` table. EndNote, Zotero, Mendeley, JabRef and Citavi all accept
this dialect.

The synthesizer is the universal fallback. Platforms with a stable
native RIS export endpoint (IEEE / ACM / SD) provide richer records via
their per-scraper ``fetch_ris`` methods; synthesis is used when those
fail or when no native exporter exists (ArXiv / GS / CNKI / patents).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


# --- Type-of-Reference (TY tag) mapping ------------------------------------
# RIS TY codes per the Reference Manager spec. We pick conservative defaults
# that EndNote handles cleanly. Conference vs journal disambiguation uses
# the ``db_type`` column when present.
_DEFAULT_TY = {
    "ARXIV": "UNPB",     # unpublished / preprint
    "IEEE": "JOUR",      # overridden to CONF below if db_type suggests it
    "ACM": "JOUR",       # ditto
    "SD": "JOUR",
    "CNKI": "JOUR",
    "GS": "JOUR",
    "PATYEE": "PAT",
    "DAWEI": "PAT",
    "DAWEISOFT": "PAT",
    "PAT_DAWEI": "PAT",
}

_CONFERENCE_HINTS = ("conference", "proceedings", "workshop", "symposium", "会议")
_THESIS_HINTS = ("thesis", "dissertation", "学位", "硕士", "博士")


def _ty_for(platform: str, db_type: str = "", venue_name: str = "") -> str:
    base = _DEFAULT_TY.get((platform or "").upper(), "JOUR")
    blob = f"{db_type} {venue_name}".lower()
    if any(h in blob for h in _CONFERENCE_HINTS):
        return "CONF"
    if any(h in blob for h in _THESIS_HINTS):
        return "THES"
    return base


# --- Field helpers ---------------------------------------------------------

_AUTHOR_SPLIT = re.compile(r"[;,]|(?<=[a-z])\s+and\s+(?=[A-Z])")


def _split_authors(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in _AUTHOR_SPLIT.split(str(raw)) if p and p.strip()]
    # Drop trailing affiliation-style noise (e.g. "(IEEE)", "et al.").
    cleaned = []
    for p in parts:
        p = re.sub(r"\s*\(.*?\)\s*$", "", p).strip(" .;,")
        if p and p.lower() not in {"et al", "et al.", "others"}:
            cleaned.append(p)
    return cleaned


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_MONTH_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b",
    re.IGNORECASE,
)
_MONTH_NUM = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _date_parts(raw: str) -> tuple[str, str]:
    """Return (PY_year_or_empty, DA_yyyy_mm_dd_or_empty)."""
    if not raw:
        return "", ""
    text = str(raw)
    y = _YEAR_RE.search(text)
    year = y.group(0) if y else ""
    iso = re.search(r"\b(19|20)\d{2}[-/\.](\d{1,2})[-/\.](\d{1,2})\b", text)
    if iso:
        return year or iso.group(0)[:4], f"{iso.group(0)[:4]}/{int(iso.group(2)):02d}/{int(iso.group(3)):02d}"
    m = _MONTH_RE.search(text)
    if m and year:
        mm = _MONTH_NUM[m.group(1)[:3].lower()]
        return year, f"{year}/{mm}/"
    return year, year and f"{year}///" or ""


def _normalize_keywords(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(k).strip() for k in value if str(k).strip()]
    if isinstance(value, str):
        return [k.strip() for k in re.split(r"[;,，；]+", value) if k.strip()]
    return []


def _line(tag: str, value: Optional[str]) -> Optional[str]:
    """Build a RIS line, or None if the value is empty.

    Non-AB tags are flattened to a single line (EndNote prefers this); AB
    keeps its internal whitespace so paragraph structure survives the round
    trip. The ``ER`` terminator is handled by the caller, not here -- it's
    a tag with no value and must always be present in the record."""
    if value is None:
        return None
    v = str(value)
    if tag != "AB":
        v = re.sub(r"\s+", " ", v).strip()
    else:
        v = v.strip()
    if not v:
        return None
    return f"{tag}  - {v}"


# --- Synthesizer -----------------------------------------------------------

def synthesize_ris(paper: Dict[str, Any]) -> str:
    """Build a RIS record from a row of the ``papers`` table.

    The input dict is the same shape returned by ``Library.get_paper`` /
    ``Library.list_papers`` (already JSON-decoded where applicable).
    """
    platform = (paper.get("platform") or "").upper()
    ty = _ty_for(platform, paper.get("db_type") or "", paper.get("venue_name") or "")
    out: List[str] = [f"TY  - {ty}"]

    def push(tag: str, value: Optional[str]) -> None:
        line = _line(tag, value)
        if line:
            out.append(line)

    push("TI", paper.get("title"))
    for a in _split_authors(paper.get("author") or ""):
        push("AU", a)
    push("JO", paper.get("venue_name") or paper.get("source"))

    py, da = _date_parts(paper.get("pub_date") or "")
    push("PY", py)
    push("DA", da)

    push("DO", paper.get("doi"))
    push("UR", paper.get("detail_link"))

    abstract = paper.get("abstract")
    if abstract and str(abstract).strip().lower() not in {"no abstract found", "no abstract provided."}:
        push("AB", abstract)

    for kw in _normalize_keywords(paper.get("keywords")):
        push("KW", kw)

    native_id = paper.get("native_id")
    if native_id:
        # AN (accession number) -- platform-specific id, helps round-tripping.
        push("AN", f"{platform}:{native_id}")

    # ER terminator: required, has no value, must NOT go through _line.
    out.append("ER  - ")
    out.append("")  # blank line separates records when concatenated
    return "\n".join(out)


def concatenate_ris(records: Iterable[str]) -> str:
    """Join multiple RIS records into one file. Each record is normalised to
    end with the canonical ``ER  - `` terminator (trailing space preserved
    per RIS spec) and a blank line separator, which all major reference
    managers (EndNote / Zotero / Mendeley / JabRef) consume cleanly."""
    pieces = []
    for r in records:
        if not r:
            continue
        s = r.rstrip()
        # Normalise the terminator to "ER  - " with trailing space.
        if s.endswith("ER  -"):
            s = s + " "
        elif not s.endswith("ER  - "):
            s = s + "\nER  - "
        pieces.append(s + "\n")
    return "\n".join(pieces)
