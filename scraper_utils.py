"""Shared helpers for the per-platform scrapers.

Keep this module dependency-light (only stdlib + playwright). Each helper is
self-contained so individual scrapers can adopt them piecemeal.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Iterable, Optional

from urllib.parse import urlparse

from mcp_logging import safe_stderr_print

_print = safe_stderr_print


# ---------------------------------------------------------------------------
# Cross-platform URL routing helpers
# ---------------------------------------------------------------------------
# Google Scholar aggregates results across publishers, so its `detail_link`
# can be an arXiv abs page, an IEEE Xplore document, an MDPI article, a
# Springer chapter, etc. Most callers want to ask "can I hand this URL to
# get_paper_details, and if so on which platform?". The mapping below
# answers that without hard-coding the lookup in every caller.

_PLATFORM_DOMAIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ARXIV",  re.compile(r"(?:^|\.)arxiv\.org$", re.IGNORECASE)),
    ("IEEE",   re.compile(r"(?:^|\.)ieee(?:xplore)?\.ieee\.org$|(?:^|\.)ieeexplore\.ieee\.org$", re.IGNORECASE)),
    ("ACM",    re.compile(r"(?:^|\.)acm\.org$", re.IGNORECASE)),
    ("SD",     re.compile(r"(?:^|\.)sciencedirect\.com$|(?:^|\.)linkinghub\.elsevier\.com$", re.IGNORECASE)),
    ("CNKI",   re.compile(r"(?:^|\.)cnki\.(?:com\.cn|net)$", re.IGNORECASE)),
    ("PATYEE", re.compile(r"(?:^|\.)patyee\.com$", re.IGNORECASE)),
    ("DAWEI",  re.compile(r"(?:^|\.)daweisoft\.com$", re.IGNORECASE)),
)


def platform_hint_from_url(url: str) -> str:
    """Map a URL to the platform code that can fetch its details.

    Returns one of ARXIV / IEEE / ACM / SD / CNKI / PATYEE / DAWEI when the
    URL hostname matches a known publisher; returns ``"EXTERNAL"`` for any
    third-party site (MDPI, Springer, Wiley, Nature, ...) the server cannot
    route to a native scraper.
    """
    if not url:
        return "EXTERNAL"
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "EXTERNAL"
    if not host:
        return "EXTERNAL"
    for code, pattern in _PLATFORM_DOMAIN_PATTERNS:
        if pattern.search(host):
            return code
    return "EXTERNAL"


# ACM DOIs are universally prefixed `10.1145/`, but the public site sometimes
# serves URLs without that prefix in the path (`/doi/3447928.3456707` instead
# of `/doi/10.1145/3447928.3456707`). The naive `split('/')[-1]` fallback that
# previously lived in acm_scraper produced non-canonical DOIs in that case,
# which then poisoned the PDF download URL and the library cache key. This
# helper resolves both forms to the canonical DOI.
_ACM_DOI_WITH_PREFIX = re.compile(r"/doi/(?:abs/|full/|pdf/)?(10\.\d+/[^/?#]+)", re.IGNORECASE)
_ACM_DOI_BARE = re.compile(r"/doi/(?:abs/|full/|pdf/)?(\d+(?:\.\d+)+)(?:[/?#]|$)")


def normalize_acm_doi(url: str) -> str:
    """Return the canonical ACM DOI for a dl.acm.org URL, or '' if unparseable."""
    if not url:
        return ""
    m = _ACM_DOI_WITH_PREFIX.search(url)
    if m:
        return m.group(1)
    m = _ACM_DOI_BARE.search(url)
    if m:
        return f"10.1145/{m.group(1)}"
    return ""


# ---------------------------------------------------------------------------
# Per-profile single-owner guard
# ---------------------------------------------------------------------------
# Camoufox is Firefox-based: a profile directory can only be opened by one
# browser process at a time. When two MCP servers (e.g. one spawned by Codex
# and one by Claude) both try to drive the same .xxx_profile, the second
# Firefox pops a blocking GUI modal ("Firefox is already running...") that
# hangs the whole tool call. We front-run that by writing a PID sentinel into
# the profile dir and refusing -- with a clear error -- when a *live* foreign
# process already owns it. Stale sentinels (from a crashed server) are
# detected via PID liveness and cleared automatically.

class ProfileInUseError(RuntimeError):
    pass


_SENTINEL_NAME = ".mcp_owner"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if not ok:
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def acquire_profile(profile_dir: str, platform: str) -> None:
    """Claim *profile_dir* for this process. Raises ProfileInUseError if a
    different, still-running process already owns it."""
    try:
        os.makedirs(profile_dir, exist_ok=True)
    except Exception:
        pass
    sentinel = os.path.join(profile_dir, _SENTINEL_NAME)
    if os.path.exists(sentinel):
        try:
            owner_pid = int((open(sentinel, encoding="utf-8").read().split(":", 1)[0] or "0").strip())
        except Exception:
            owner_pid = 0
        if owner_pid and owner_pid != os.getpid() and _pid_alive(owner_pid):
            raise ProfileInUseError(
                f"{platform} browser profile '{profile_dir}' is already in use by another "
                f"MCP server (PID {owner_pid}). Run only one MCP client against this platform "
                f"at a time, or give each client its own profile via the 'profile_suffix' "
                f"setting in mcp_runtime_config.json."
            )
        # Stale sentinel from a crashed/exited process -- safe to take over.
    try:
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}:{platform}")
    except Exception as e:
        _print(f"[profile-guard] could not write sentinel for {platform}: {e}")


def release_profile(profile_dir: str) -> None:
    """Drop this process's claim on *profile_dir* (best-effort)."""
    sentinel = os.path.join(profile_dir, _SENTINEL_NAME)
    try:
        if os.path.exists(sentinel):
            owner_pid = 0
            try:
                owner_pid = int((open(sentinel, encoding="utf-8").read().split(":", 1)[0] or "0").strip())
            except Exception:
                pass
            if owner_pid in (0, os.getpid()):
                os.remove(sentinel)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pooled profiles: let N concurrent MCP servers each get a usable profile dir
# ---------------------------------------------------------------------------
# Strategy: the first server to claim a platform uses the canonical
# .<plat>_profile (its cookies persist naturally). Concurrent servers fall
# back to an ephemeral per-PID copy so Firefox never sees two processes on
# one profile. The expensive auth state (cf_clearance + pinned fingerprint)
# is shared out-of-band via the MySQL library, not the profile dir.


def pooled_profile(base_name: str, platform: str) -> tuple[str, bool]:
    """Return (profile_dir, is_ephemeral).

    Tries the canonical profile first. If a *live* foreign MCP server already
    owns it, allocates an ephemeral per-PID directory instead so we never
    block on Firefox's single-instance modal.
    """
    from runtime_config import profile_path

    canonical = profile_path(base_name)
    try:
        acquire_profile(canonical, platform)
        return canonical, False
    except ProfileInUseError:
        ephemeral = profile_path(f"{base_name}__p{os.getpid()}")
        # A per-PID dir can only be ours; acquire still records the sentinel.
        try:
            acquire_profile(ephemeral, platform)
        except ProfileInUseError:
            pass
        _print(f"[pooled_profile] {platform} canonical busy; using ephemeral {ephemeral}")
        return ephemeral, True


def cleanup_pooled_profile(profile_dir: Optional[str], is_ephemeral: bool) -> None:
    if not profile_dir:
        return
    release_profile(profile_dir)
    if is_ephemeral:
        import shutil

        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared fingerprint + cookies (via the MySQL library)
# ---------------------------------------------------------------------------

def load_or_create_fingerprint(platform: str):
    """Return a browserforge Fingerprint that is STABLE across MCP servers.

    cf_clearance / DataDome clearance cookies are bound to the browser
    fingerprint; reusing one verification across clients only works if every
    client presents the same fingerprint. We pin one per platform in MySQL.
    Returns None if the library is unavailable or anything goes wrong (caller
    then falls back to Camoufox's default per-launch fingerprint).
    """
    import base64
    import pickle

    try:
        from camoufox.fingerprints import generate_fingerprint
        from library import get_library

        lib = get_library()
        if not lib.enabled:
            return None
        state = lib.get_browser_state(platform)
        blob = (state or {}).get("fingerprint") or {}
        if isinstance(blob, dict) and blob.get("pickle_b64"):
            try:
                fp = pickle.loads(base64.b64decode(blob["pickle_b64"]))
                return fp
            except Exception as e:
                _print(f"[fingerprint] {platform} stored fingerprint unusable ({e}); regenerating")
        fp = generate_fingerprint(os="windows")
        try:
            ua = str(fp.navigator.userAgent)
        except Exception:
            ua = None
        encoded = base64.b64encode(pickle.dumps(fp)).decode("ascii")
        lib.save_browser_fingerprint(platform, {"pickle_b64": encoded}, user_agent=ua)
        return fp
    except Exception as e:
        _print(f"[fingerprint] {platform} load/create failed ({e}); using default")
        return None


_VERIFICATION_COOKIE_HINTS = ("cf_clearance", "datadome", "__ddg", "incap_ses", "visid_incap", "reese84")


def _sanitize_cookies(cookies: list) -> list:
    clean = []
    for c in cookies or []:
        if not c.get("name") or "domain" not in c:
            continue
        cc = {k: c[k] for k in ("name", "value", "domain", "path") if k in c}
        if "expires" in c and isinstance(c["expires"], (int, float)) and c["expires"] > 0:
            cc["expires"] = c["expires"]
        for b in ("httpOnly", "secure"):
            if b in c:
                cc[b] = bool(c[b])
        ss = c.get("sameSite")
        if ss in ("Strict", "Lax", "None"):
            cc["sameSite"] = ss
        clean.append(cc)
    return clean


async def apply_browser_cookies(context, platform: str) -> bool:
    """Inject shared cookies into *context* before first navigation."""
    try:
        from library import get_library

        lib = get_library()
        if not lib.enabled:
            return False
        state = lib.get_browser_state(platform)
        cookies = _sanitize_cookies((state or {}).get("cookies") or [])
        if not cookies:
            return False
        await context.add_cookies(cookies)
        _print(f"[cookies] {platform}: injected {len(cookies)} shared cookies")
        return True
    except Exception as e:
        _print(f"[cookies] {platform} inject failed: {e}")
        return False


async def capture_browser_cookies(context, platform: str, note: str = None) -> None:
    """Persist *context* cookies to the shared store. Marks the platform
    'verified' when a known anti-bot clearance cookie is present so the web
    console / other clients can tell the verification is fresh."""
    try:
        from library import get_library

        lib = get_library()
        if not lib.enabled:
            return
        cookies = await context.cookies()
        if not cookies:
            return
        has_clearance = any(
            any(h in (c.get("name", "").lower()) for h in _VERIFICATION_COOKIE_HINTS)
            for c in cookies
        )
        lib.save_browser_cookies(
            platform,
            cookies,
            mark_verified=has_clearance,
            note=note or ("verification cookie present" if has_clearance else "session cookies"),
        )
        _print(f"[cookies] {platform}: stored {len(cookies)} cookies (verified={has_clearance})")
    except Exception as e:
        _print(f"[cookies] {platform} capture failed: {e}")


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
