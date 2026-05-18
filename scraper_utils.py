"""Shared helpers for the per-platform scrapers.

Keep this module dependency-light (only stdlib + playwright). Each helper is
self-contained so individual scrapers can adopt them piecemeal.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Iterable, Optional

from mcp_logging import safe_stderr_print

_print = safe_stderr_print


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
