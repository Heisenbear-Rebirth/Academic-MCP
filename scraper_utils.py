"""Shared helpers for the per-platform scrapers.

Keep this module dependency-light (only stdlib + playwright). Each helper is
self-contained so individual scrapers can adopt them piecemeal.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Iterable

from mcp_logging import safe_stderr_print

_print = safe_stderr_print


# ---------------------------------------------------------------------------
# Venue / journal name matching
# ---------------------------------------------------------------------------

# Trailing ellipsis markers that Google Scholar / some other sites insert when
# the venue name is too long to render. We strip them so the substring check
# below treats the abbreviated text as a prefix of the real name.
_TRAIL_ELLIPSIS_RE = re.compile(r"[…\.\s]+$")
# Generic punctuation noise (parentheses, dashes, commas, colons) that doesn't
# change which journal a result belongs to.
_PUNCT_RE = re.compile(r"[\(\)\[\]\.,;:\-_/]+")
# Stopwords we drop before token comparison so "Journal of X" still matches
# "X Journal" etc. Conservative list — purely structural words.
_STOPWORDS = {"the", "a", "an", "of", "and", "on", "in", "for", "to", "by"}


def _normalize_venue(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    t = _TRAIL_ELLIPSIS_RE.sub("", t)
    t = _PUNCT_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> list[str]:
    return [w for w in _normalize_venue(text).split(" ") if w and w not in _STOPWORDS]


def venue_matches(target: str | None, venue: str | None) -> bool:
    """Return True if *venue* plausibly is *target* (or vice versa).

    Used as a belt-and-suspenders journal post-filter on top of each
    platform's URL-level filter. We are intentionally forgiving about:

    - trailing "..." / "…" truncations (Google Scholar abbreviates long titles)
    - case, punctuation, and order-irrelevant stopwords
    - subset matches (e.g. "Communications of the ACM (CACM), Volume 68, Issue 12"
      still matches target "Communications of the ACM")

    If either side is empty we treat the check as a pass-through.
    """
    if not target or not venue:
        return True
    t = _normalize_venue(target)
    v = _normalize_venue(venue)
    if not t or not v:
        return True
    if t in v or v in t:
        return True
    # Token-subset match: every meaningful word of the shorter side appears
    # in the longer side, regardless of order. Catches "Adv Neural Inf
    # Processing Systems" vs "Advances in Neural Information Processing
    # Systems" where neither is a substring of the other.
    tt = set(_tokens(target))
    vt = set(_tokens(venue))
    if not tt or not vt:
        return True
    short, long_ = (tt, vt) if len(tt) <= len(vt) else (vt, tt)
    if short and len(short & long_) / len(short) >= 0.7:
        return True
    return False


# ---------------------------------------------------------------------------
# Robust page.goto with retry on Firefox transient network aborts
# ---------------------------------------------------------------------------

_RETRYABLE_MARKERS: tuple[str, ...] = (
    "NS_ERROR_ABORT",
    "NS_ERROR_NET_INTERRUPT",
    "NS_ERROR_NET_RESET",
    "NS_ERROR_NET_TIMEOUT",
    "NS_BINDING_ABORTED",
    "net::ERR_ABORTED",
    "net::ERR_NETWORK_CHANGED",
)


def _is_retryable_network_error(exc: BaseException) -> bool:
    msg = str(exc)
    return any(marker in msg for marker in _RETRYABLE_MARKERS)


async def goto_with_retry(page, url: str, *, retries: int = 2, backoff: float = 1.5, **kwargs: Any):
    """page.goto with bounded retry on idempotent Firefox network aborts.

    Firefox + Camoufox occasionally tears down a load mid-flight with
    NS_ERROR_ABORT / NS_ERROR_NET_INTERRUPT, especially under DataDome /
    Cloudflare middleware that resets the connection. Those failures are
    idempotent (the navigation never partially "succeeded"), so retrying is
    safe and usually wins on the second attempt.

    Non-retryable errors (TimeoutError on a legitimate slow page, target-
    closed errors, etc.) are re-raised immediately so the caller can handle
    them with its existing logic.
    """
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await page.goto(url, **kwargs)
        except Exception as e:
            if not _is_retryable_network_error(e):
                raise
            last_exc = e
            if attempt < retries:
                wait = backoff * (attempt + 1)
                _print(f"[goto_with_retry] transient {type(e).__name__} on {url[:80]}; retry in {wait:.1f}s ({attempt + 1}/{retries})")
                await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc
