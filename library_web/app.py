"""FastAPI management UI for the local academic library.

Launch via the start_library_web scripts or directly:
    python -m uvicorn library_web.app:app --host 127.0.0.1 --port 5577
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from library import get_library
from runtime_config import ensure_runtime_environment, library_root_path

ensure_runtime_environment()

_HERE = Path(__file__).resolve().parent

app = FastAPI(title="Academic Library Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _qs(**kwargs) -> str:
    cleaned = {k: v for k, v in kwargs.items() if v not in (None, "", 0)}
    return urlencode(cleaned)


templates.env.globals["qs"] = _qs


def _render(template: str, request: Request, **ctx):
    lib = get_library()
    ctx.setdefault("library_enabled", lib.enabled)
    ctx.setdefault("library_disabled_reason", lib._disabled_reason)
    ctx.setdefault("library_root", str(library_root_path()))
    return templates.TemplateResponse(request, template, ctx)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    lib = get_library()
    stats = lib.stats()
    return _render("dashboard.html", request, stats=stats)


@app.get("/papers", response_class=HTMLResponse)
def papers_list(
    request: Request,
    platform: str = Query("", description="filter by platform"),
    q: str = Query("", description="title/author/native_id substring"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=5, le=200),
):
    lib = get_library()
    result = lib.list_papers(
        platform=platform or None,
        keyword=q or None,
        page=page,
        page_size=page_size,
    )
    total_pages = max(1, math.ceil(result["total"] / page_size))
    return _render(
        "papers.html", request,
        rows=result["rows"], total=result["total"],
        page=page, page_size=page_size, total_pages=total_pages,
        platform=platform, q=q,
    )


@app.get("/papers/{platform}/{native_id:path}", response_class=HTMLResponse)
def paper_detail(request: Request, platform: str, native_id: str):
    lib = get_library()
    paper = lib.get_paper(platform, native_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return _render("paper_detail.html", request, paper=paper, platform=platform, native_id=native_id)


@app.post("/papers/{platform}/{native_id:path}/delete")
def paper_delete(platform: str, native_id: str, remove_files: str = ""):
    lib = get_library()
    lib.delete_paper(platform, native_id, remove_files=(remove_files == "on"))
    return RedirectResponse(url="/papers", status_code=303)


@app.get("/searches", response_class=HTMLResponse)
def searches_list(request: Request, page: int = Query(1, ge=1), page_size: int = Query(30, ge=5, le=200)):
    lib = get_library()
    result = lib.list_searches(page=page, page_size=page_size)
    total_pages = max(1, math.ceil(result["total"] / page_size))
    return _render(
        "searches.html", request,
        rows=result["rows"], total=result["total"],
        page=page, page_size=page_size, total_pages=total_pages,
    )


@app.post("/searches/{search_id}/delete")
def search_delete(search_id: int):
    lib = get_library()
    lib.delete_search(search_id)
    return RedirectResponse(url="/searches", status_code=303)


@app.get("/browser-state", response_class=HTMLResponse)
def browser_state(request: Request):
    lib = get_library()
    rows = lib.list_browser_states()
    return _render("browser_state.html", request, rows=rows)


@app.post("/browser-state/{platform}/clear-cookies")
def browser_state_clear_cookies(platform: str):
    """Drop cookies + verified flag but keep the pinned fingerprint, so the
    next session re-verifies cleanly without changing identity."""
    lib = get_library()
    lib.clear_browser_state(platform, keep_fingerprint=True)
    return RedirectResponse(url="/browser-state", status_code=303)


@app.post("/browser-state/{platform}/reset")
def browser_state_reset(platform: str):
    """Full reset: drop cookies AND fingerprint. Next session generates a
    brand-new identity (use if a platform hard-banned the fingerprint)."""
    lib = get_library()
    lib.clear_browser_state(platform, keep_fingerprint=False)
    return RedirectResponse(url="/browser-state", status_code=303)


@app.get("/file")
def serve_file(path: str):
    """Serve a file from inside the library root. Path traversal is rejected."""
    root = library_root_path().resolve()
    target = Path(path)
    if not target.is_absolute():
        target = (root / target).resolve()
    else:
        target = target.resolve()
    if not _is_inside(target, root):
        raise HTTPException(status_code=403, detail="Path is outside the library root")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media = None
    suffix = target.suffix.lower()
    if suffix == ".pdf":
        media = "application/pdf"
    elif suffix == ".md":
        media = "text/markdown; charset=utf-8"
    return FileResponse(str(target), media_type=media, filename=target.name)


@app.get("/health")
def health():
    lib = get_library()
    return {
        "enabled": lib.enabled,
        "reason": lib._disabled_reason,
        "library_root": str(library_root_path()),
    }
