# Academic MCP Server

> 中文版：[README.zh-CN.md](README.zh-CN.md)

A Model Context Protocol (MCP) server that exposes a uniform paper-discovery
and full-text-extraction API on top of eight academic / patent sources:

| Code      | Source                                       | Engine             |
| --------- | -------------------------------------------- | ------------------ |
| `ARXIV`   | arXiv (preprints)                            | Camoufox (Firefox) |
| `CNKI`    | 中国知网 (China National Knowledge Infrastructure) | Playwright Chromium |
| `IEEE`    | IEEE Xplore                                  | Camoufox (Firefox) |
| `ACM`     | ACM Digital Library                          | Camoufox (Firefox) |
| `SD`      | ScienceDirect (Elsevier)                     | Camoufox (Firefox) |
| `GS`      | Google Scholar                               | Camoufox (Firefox) |
| `PATYEE`  | Patyee 专利之星                                  | Playwright Chromium |
| `DAWEI`   | 大为专利 (pat.daweisoft.com)                     | Playwright Chromium |

Every source is wrapped by a per-platform scraper that surfaces a consistent
result schema, and every download/conversion is routed through a local
MySQL-backed library so repeated queries are served from cache, browser
state is shared across concurrent MCP clients, and downloaded PDFs/Markdown
are deduplicated on disk.

The whole thing is meant to be embedded as a tool provider in an LLM agent
(Claude Desktop, Codex, etc.) via MCP, but the underlying tools are also
useful as a CLI/library on their own.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  MCP client (Claude / Codex / ...)                                   │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ MCP tool calls
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  server.py  —  FastMCP entry, dispatches to per-platform scrapers    │
│  ┌──────────────────────────┐    ┌────────────────────────────────┐  │
│  │  Library (library.py)    │◄──►│  Scrapers (8 modules)          │  │
│  │  MySQL: papers,          │    │  - one Camoufox/Chromium       │  │
│  │         search_queries,  │    │    persistent context per      │  │
│  │         browser_state    │    │    platform (singleton)        │  │
│  │  Files: .repo/{platform} │    │  - pooled per-PID profile      │  │
│  │         /{native_id}/    │    │    when canonical is busy      │  │
│  └──────────────────────────┘    └────────────────────────────────┘  │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ optional
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  library_web/  —  FastAPI/Jinja2 management console (port 5577)      │
│  /  /papers  /papers/{p}/{id}  /searches  /browser-state  /file      │
└──────────────────────────────────────────────────────────────────────┘
```

Every MCP tool call passes through the Library first:

- **Search**: the (platform, query, filters) tuple is hashed; on cache hit
  the cached result is returned without launching a browser.
- **Details / Download / Read**: the paper is identified by
  `(platform, native_id)` extracted from the URL. If the metadata / PDF /
  Markdown is already on disk, it is mirrored to the caller's requested
  `output_dir` without re-fetching.
- **Browser state**: each Camoufox scraper pins a single fingerprint per
  platform and reuses verification cookies (cf_clearance / DataDome / …),
  so one manual CAPTCHA carries across reboots and across concurrent MCP
  clients.

---

## Components

| Path                         | Role                                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| `server.py`                  | FastMCP server. Exposes the five MCP tools and wires them through the Library.                    |
| `runtime_config.py`          | Loads `mcp_runtime_config.json`, resolves project paths, computes profile dirs (with suffix).     |
| `mcp_logging.py`             | UTF-8-safe stderr printer that doesn't break when the MCP transport is JSON-RPC over stdio.       |
| `library.py`                 | MySQL DAO for `papers`, `search_queries`, `browser_state` + helpers for filesystem mirroring.     |
| `scraper_utils.py`           | Shared helpers: `goto_with_retry`, `venue_matches`, profile pool, fingerprint + cookie sharing.   |
| `pdf_utils.py`               | `convert_pdf_to_markdown()` — uses pymupdf4llm, falls back to per-page images for scanned PDFs.   |
| `{platform}_scraper.py`      | One module per source. All expose `search_papers / get_paper_details / download_paper / read_paper_content / close`. |
| `library_web/`               | FastAPI app + Jinja2 templates for the management console.                                        |
| `library_web_{start,stop}.{bat,ps1}` | Helper scripts to launch / stop the console (port from `mcp_runtime_config.json`).      |
| `.repo/`                     | Canonical file store: `.repo/{PLATFORM}/{safe_native_id}/paper.pdf` and `paper.md`.               |
| `.{platform}_profile/`       | Per-platform browser profile. Canonical profile + per-PID ephemeral copies as needed.             |

---

## Requirements

- **Python 3.10+** (tested on 3.11/3.12). 3.10 is the lower bound for the
  PEP 604 union syntax used in a few places.
- **MySQL 5.5+** locally reachable. The schema is intentionally portable
  down to 5.5 (`MEDIUMTEXT` instead of `JSON`, ASCII charset on indexed
  identifier columns, `DATETIME` columns populated via `NOW()` in DML so
  no `DEFAULT CURRENT_TIMESTAMP` is required).
- **Camoufox kernel** (auto-fetched into `%LOCALAPPDATA%\camoufox` on
  Windows; under `~/.cache/camoufox` on Linux).

---

## Installation

```bash
python -m venv .venv
.\.venv\Scripts\activate          # Windows
# source .venv/bin/activate       # POSIX
pip install -r requirements.txt
python -m camoufox fetch          # downloads the patched Firefox kernel
```

Create `.env` from `.env.example` and fill in your MySQL credentials:

```dotenv
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=...
MYSQL_DATABASE=academic_mcp
```

The library auto-creates the database and the three tables on first start.
If MySQL is unreachable the Library disables itself with a clear warning
and the server falls back to passthrough mode (no caching, no shared
browser state — but everything still works).

---

## Configuration

`mcp_runtime_config.json` (committed) holds non-secret tunables:

| Key                                   | Type    | Default          | Purpose                                                                                 |
| ------------------------------------- | ------- | ---------------- | --------------------------------------------------------------------------------------- |
| `playwright_browsers_path`            | string  | `".ms-playwright"` | Where Playwright stores its Chromium download.                                          |
| `override_playwright_browsers_path`   | bool    | `true`           | Force-export the path so the right Chromium is picked up by Playwright on every launch. |
| `set_cwd_to_project_root`             | bool    | `true`           | `chdir` to the project root on import — keeps relative paths sane in MCP servers.       |
| `allow_headful_fallback`              | bool    | `false`          | Master switch: on anti-bot block, may we relaunch with a visible browser for human verification? |
| `allow_headful_fallback_platforms`    | list    | `["ACM","SD"]`   | Subset of platforms allowed to do the headful CAPTCHA fallback.                         |
| `manual_verification_timeout_seconds` | int     | `180`            | How long we wait (in headful mode) for a human to click through the CAPTCHA.            |
| `library_enabled`                     | bool    | `true`           | Master switch for the MySQL+filesystem Library. Set to `false` to disable caching.      |
| `library_root`                        | string  | `".repo"`        | Where canonical PDFs/MD/images live (relative to project root or absolute).             |
| `library_web_host` / `library_web_port` | string/int | `127.0.0.1:5577` | Bind address for the management console.                                              |
| `profile_suffix`                      | string  | `""`             | Optional shared suffix; per-client override via `MCP_PROFILE_SUFFIX` env var (see below). |

Secrets live in `.env` (gitignored). Only MySQL credentials so far.

---

## MCP tools

All five tools are registered on the FastMCP server (`server.py`) and
appear to the LLM with the docstrings preserved verbatim.

### `search_papers(query, platform="CNKI", ...)`

| Parameter      | Type          | Description                                                                                                 |
| -------------- | ------------- | ----------------------------------------------------------------------------------------------------------- |
| `query`        | str           | The search terms. Free text; per-platform syntax (e.g. IEEE supports `("Publication Title":"...")`) is allowed. |
| `platform`     | str           | One of `CNKI`, `IEEE`, `ARXIV`, `ACM`, `SD`, `GS`, `PATYEE`, `DAWEI`.                                       |
| `search_field` | str           | Restricts the query field. Platform-specific (see docstring); defaults to each platform's "all".            |
| `db_scope`     | str           | CNKI only: `总库 / 中文 / 外文`.                                                                                  |
| `source_type`  | str           | Document type filter (research-article, conference, journal, …). Platform-specific.                         |
| `journal`      | str \| None   | Restrict to a publication. Server combines URL-level filters with a client-side fuzzy `venue_matches` check that tolerates Google-Scholar-style "…" truncations.    |
| `start_year`   | int \| None   | Inclusive lower bound on publication year.                                                                  |
| `end_year`     | int \| None   | Inclusive upper bound on publication year.                                                                  |
| `sort_by`      | str           | `relevance` (default), `citations`, or `date_desc`.                                                         |
| `start_index`  | int           | Pagination offset; pass e.g. 20 to skip the first page of 10–20 results.                                    |
| `limit`        | int           | Max number of results to return (default 10).                                                               |

**Returns** a JSON string with:

```json
{
  "total_results": "1707",
  "papers": [
    {
      "id": "<8-hex hash of detail_link>",
      "title": "...",
      "author": "...",
      "source": "...",                // raw publisher line (Volume / Issue / etc.)
      "venue_name": "...",            // canonical journal / conference name when extractable
      "doi": "10.1145/...",           // ACM only; others fill in from get_paper_details
      "date": "March 2026",
      "db_type": "Research article",
      "detail_link": "https://..."
    }
  ],
  "_cache_hit": true                  // server-only flag; true when served from MySQL cache
}
```

Cache key is `(platform, normalized query, filters, pagination)`. Cache
entries never expire by default (the user picked permanent caching during
the initial design). To force a refresh, delete the row from the
`search_queries` table or via the web console.

### `get_paper_details(url, platform="CNKI")`

Fetches abstract / keywords / DOI for a specific paper. Re-uses cached
data when the same paper has already been seen (abstract column in
`papers`). Returns:

```json
{
  "url": "...",
  "abstract": "...",
  "keywords": ["..."],
  "doi": "...",
  "_cache_hit": true
}
```

`get_paper_details` is wrapped in `goto_with_retry` — transient Firefox
network aborts (`NS_ERROR_ABORT`, `NS_ERROR_NET_INTERRUPT`, ...) get up to
two automatic retries with exponential backoff before the failure
propagates.

As a side effect, every successful details fetch also stores an
EndNote-importable RIS record on `papers.ris_text` (see
[RIS auto-store](#ris-auto-store) below).

### `download_paper(url, output_dir, platform="CNKI")`

Downloads the PDF. If the file is already on disk under
`.repo/{PLATFORM}/{native_id}/...pdf` it is just mirrored to `output_dir`.
On a fresh download, it is written into the canonical location AND
mirrored to `output_dir`. Returns the local filesystem path.

ACM and SD now prefer request-level retrieval once a verified browser
session exists. ACM tries the DOI PDF endpoint with verified cookies and
classifies papers whose detail page exposes no PDF link as unavailable
rather than failed.

SD search/details use direct HTTP where possible; PDF download navigates
only far enough to obtain/trigger the signed ScienceDirect asset URL, then
saves the PDF through programmatic download/body capture. After the PDF
host challenge has been solved once in the visible browser, fresh SD PDFs
typically save in a few seconds instead of timing out in the viewer.

### `read_paper_content(url, output_dir, platform="CNKI")`

Three-level cache:

1. If Markdown is already in the library → mirror MD + images/ to
   `output_dir` and return immediately.
2. If only the PDF is cached → re-run `pdf_utils.convert_pdf_to_markdown`
   against the cached PDF.
3. Cache miss → full scrape, store PDF + MD + images, mirror to
   `output_dir`.

Returns the MD file path and a 1000-character preview.

### `convert_local_pdf(pdf_path, output_dir)`

Standalone helper to convert a user-supplied PDF to Markdown using the
same image-extraction pipeline. Does not touch the Library.

---

## The Library (MySQL + filesystem)

### Schema

```
papers          (platform, native_id) -> unique key
                title, author, source, venue_name, pub_date, db_type, doi,
                detail_link, abstract, keywords (json text),
                pdf_path, md_path, images_dir, extra (json text),
                ris_text (platform-native or synthesized RIS export),
                created_at, updated_at

search_queries  (platform, md5(query + filters)) -> unique key
                query_text, filters (json text), total_results,
                results (json text), fetched_at

browser_state   platform PRIMARY KEY
                fingerprint (pickled+b64 BrowserForge Fingerprint),
                cookies     (json: Playwright context.cookies()),
                user_agent, verified_at, updated_at, note
```

All JSON-shaped columns are `MEDIUMTEXT` so the schema works unchanged on
MySQL 5.5+. ASCII charset is pinned on indexed identifier columns
(`native_id`, `query_hash`) to stay inside InnoDB's 767-byte prefix limit
under `utf8mb4`.

### Filesystem layout

```
.repo/
  ARXIV/2605.16230/
    arxiv_2605_16230.pdf
    arxiv_2605_16230.md
    images/
      arxiv_2605_16230.pdf-0002-08.png
      ...
  IEEE/9266228/
    ieee_9266228.pdf
    ieee_9266228.md
    images/...
  CNKI/CJFD|...
```

`native_id` is normalized to a filesystem-safe slug. The canonical
location is the single source of truth; every download mirrors to the
caller's `output_dir` afterward.

### RIS auto-store

Every `get_paper_details` call that produces a usable abstract also
populates `papers.ris_text` with an EndNote-importable RIS record.
The server prefers the platform's own RIS export endpoint (richer
pagination, ISSN, conference proceedings name) and falls back to
synthesizing one from the stored metadata (`ris_utils.synthesize_ris`)
when the platform doesn't expose an endpoint or the network call fails.
Either way the column is populated idempotently — re-running details
on a cached row doesn't refetch.

| Platform | Source         | Endpoint / strategy                                                                                                        |
| -------- | -------------- | -------------------------------------------------------------------------------------------------------------------------- |
| IEEE     | platform       | `/rest/search/citation/format`, with `/xpl/downloadCitations` form POST fallback.                                          |
| ACM      | platform       | `/action/downloadCitation?format=ris&include=abs`.                                                                         |
| SD       | platform       | `/sdfe/arp/cite?format=application/x-research-info-systems` — issued via `page.evaluate("fetch(url)")` so DataDome sees an in-app XHR (a direct `context.request.get` returns a 403 challenge page). |
| ARXIV / CNKI / GS / PATYEE / DAWEI | synthesized | `ris_utils.synthesize_ris` infers `TY` from db_type + venue, splits `AU`, parses `PY/DA` from `pub_date`, and tags `AN  - {platform}:{native_id}` for round-tripping. |

A bundled export is available from the web console: filter `/papers` by
state, tick the checkboxes, and POST to `/papers/export-ris`.
Rows that are missing `ris_text` (e.g. a synthesizer was added after the
row was first seen) are synthesized on demand during the export and the
result is written back, so the next export hits cache.

---

## Browser state pool (concurrency + verification reuse)

Firefox/Camoufox refuses to open the same profile directory from two
processes simultaneously. Without coordination, running two MCP clients
(e.g. one inside Claude Desktop and one inside Codex) would either hang
on the "Firefox is already running" modal or force every client to clear
its own CAPTCHA.

The browser-state pool solves both:

1. **Pooled profile dirs.** The first MCP server to claim a platform uses
   the canonical `.<platform>_profile/`. Concurrent servers transparently
   fall back to an ephemeral per-PID copy `.<platform>_profile__p<pid>/`
   (auto-removed on `close()`). A PID-aware sentinel file
   (`.mcp_owner`) prevents stale locks from crashed processes from
   blocking new launches.

2. **Pinned fingerprint.** Cloudflare's `cf_clearance` and DataDome
   tokens are bound to the browser fingerprint. We generate ONE
   `browserforge` Fingerprint per platform with `os="windows"`, store it
   pickled+base64 in `browser_state.fingerprint`, and pass it to every
   `AsyncCamoufox(fingerprint=...)` launch. All concurrent clients on a
   platform present the same identity.

3. **Shared cookies.** After a successful pass of CAPTCHA / Cloudflare /
   DataDome, the scraper captures `context.cookies()` into
   `browser_state.cookies`. The next launch (any client, any MCP server
   process) injects those cookies via `context.add_cookies` *before* the
   first navigation. One manual verification covers all clients until
   the platform invalidates the token.

A row in `browser_state` is marked `verified_at = NOW()` whenever the
captured cookies contain a known clearance cookie (`cf_clearance`,
`datadome`, `__ddg`, `incap_ses`, `reese84`).

### Limitations

`cf_clearance` is bound to `(IP, fingerprint, TLS JA3)`. We pin
fingerprint; your IP is your local IP; but Camoufox's outgoing TLS
fingerprint is not byte-identical across launches. In practice this
means most launches reuse the verification, but a tightened Cloudflare
policy can still occasionally force a re-verify. This is still strictly
better than the unfixed state (every client, every restart needs
manual interaction).

### Web console pages

| URL                                       | Purpose                                                                          |
| ----------------------------------------- | -------------------------------------------------------------------------------- |
| `/`                                       | Counts per platform + totals.                                                   |
| `/papers`                                 | Paginated listing with platform / keyword / state filter. Checkbox-select rows and POST `/papers/export-ris` for a bundled `.ris` download. |
| `/papers/{platform}/{native_id}`          | Full metadata, abstract, links to served PDF / Markdown.                        |
| `/searches`                               | Cached search queries; delete per row to force refetch.                         |
| `/browser-state`                          | Per-platform fingerprint / cookies / verification status; clear or full reset.  |
| `/file?path=...`                          | Static file server scoped to `.repo/`. Path traversal is rejected.              |
| `/health`                                 | JSON: `{enabled, reason, library_root}`.                                        |

---

## Running

### MCP server (production)

Wire the server into your MCP client's configuration. For Claude
Desktop's `mcp.json` (or Codex's equivalent):

```jsonc
{
  "academic-mcp": {
    "command": "E:\\Projects\\CNKI-MCP\\.venv\\Scripts\\python.exe",
    "args": ["E:\\Projects\\CNKI-MCP\\server.py"],
    "env": {
      "MCP_PROFILE_SUFFIX": "claude"
    }
  }
}
```

`MCP_PROFILE_SUFFIX` is **optional**. Leave it unset for the default
behavior (one canonical profile, automatic per-PID fallback for the
second client). Set it differently in each client only if you want each
MCP server to have its own dedicated profile set
(`.ieee_profile_claude`, `.ieee_profile_codex`, etc.). The DB-backed
browser state is shared regardless.

### MCP server (standalone, for debugging)

```bash
.\.venv\Scripts\python.exe server.py
```

The server speaks MCP over stdio, so a raw invocation will just hang
waiting for JSON-RPC. Useful only when attached from an MCP client.

### Management console

```bat
library_web_start.bat   :: starts uvicorn on the configured port, opens the browser
library_web_stop.bat    :: kills the listener on that port
```

PowerShell equivalents (`library_web_start.ps1` /
`library_web_stop.ps1`) are also provided. The console runs read-write
(it can delete cache rows / clear browser state) but is bound to
`127.0.0.1` by default — change `library_web_host` in
`mcp_runtime_config.json` if you really want remote access.

---

## Multi-client guidance

| Scenario                                            | What happens                                                                                  | What you should do            |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------- | ----------------------------- |
| One MCP client at a time                            | Canonical profile, full caching, one CAPTCHA per platform per token TTL.                      | Nothing.                      |
| Two clients, same platform, same time               | First client gets canonical profile; second gets per-PID ephemeral copy. Both share cookies.  | Nothing.                      |
| Two clients, you want fully isolated profiles       | Set `MCP_PROFILE_SUFFIX=claude` / `MCP_PROFILE_SUFFIX=codex` in each client's env.             | Edit MCP client config.       |
| CAPTCHA appeared and you solved it once             | Cookies captured to `browser_state`. Subsequent launches inject them and skip the challenge.  | Nothing.                      |
| Platform banned the fingerprint                     | All clients fail equally because they all present the same identity.                          | `/browser-state` → "完全重置". |
| Just want to drop stale cookies                     | `verified_at` will go stale; new searches may take the manual path again.                     | `/browser-state` → "清 Cookie". |

---

## Validation snapshot

Latest cold-result smoke run: `scratch/full_platform_test.py`,
run id `20260614_full_final`, 3 download attempts per platform where
downloads apply. Hard failures: **0**. Unavailable rows are publisher or
tool semantics, not scraper exceptions.

| Platform | Search | Details | Download result | Read result |
| -------- | ------: | ------: | --------------- | ----------- |
| `ARXIV`  | 6.584 s | 4.976 s | 3/3 OK: 15.065, 12.841, 10.203 s | success, 20.405 s |
| `CNKI`   | 1.587 s | 0.198 s | 3/3 OK: 1.488, 1.479, 1.325 s | success, 2.178 s |
| `IEEE`   | 4.165 s | 1.149 s | 3/3 OK: 29.498, 8.352, 2.809 s | success, 36.236 s |
| `ACM`    | 25.385 s | 3.034 s | 3/3 unavailable: tested articles expose no PDF link | unavailable |
| `SD`     | 15.312 s | 4.913 s | 3/3 OK: 4.835, 5.381, 3.819 s | success, 22.149 s |
| `GS`     | 2.443 s | 0.000 s | not applicable / unavailable by design | not applicable |
| `PATYEE` | 1.695 s | 0.374 s | 1/3 OK, 2/3 unavailable | success, 8.778 s |
| `DAWEI`  | 2.551 s | 0.755 s | 3/3 OK: 3.115, 3.284, 2.598 s | success, 4.768 s |

---

## Troubleshooting

| Symptom                                                              | Likely cause                                                          | Fix                                                                                                                |
| -------------------------------------------------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `Failed to launch the browser process / exitCode=0` on Camoufox      | Stale `parent.lock` in profile dir from a crashed previous run.       | Auto-cleaned on next launch by `scraper_utils.acquire_profile`. If still stuck, delete `.<platform>_profile/parent.lock`. |
| `TargetClosedError: Target page, context or browser has been closed` | Cloudflare timeout left the persistent context wedged.                | Auto-handled: `_context_is_alive` detects and rebuilds on next call. If repeated, restart the MCP server.          |
| `ProfileInUseError: ... already in use by another MCP server`        | A second client tried to claim a profile already held by a *live* PID. | Either close the other client or set `MCP_PROFILE_SUFFIX` to a different value in this client's env.               |
| SD PDF page is visible but no file is saved                           | Firefox opened the PDF internally instead of emitting a download.     | Auto-handled: the SD profile forces `application/pdf` to save-to-disk and the scraper captures the download event. |
| ACM download returns unavailable                                      | The article page and `/doi/pdf/...` endpoint redirect to abstract HTML with no PDF link. | Treated as publisher-unavailable, not a scraper failure. Try a different ACM article/DOI.                         |
| `_cache_hit=true` but the result looks stale                         | Permanent search cache returned a fossil row.                         | `/searches` → delete the row, re-run the search.                                                                   |
| `journal=` filter returned 0 results on CNKI                         | CNKI relevance ordering may not surface the target journal in page 1. | Increase `limit`, narrow the query, or accept that CNKI's cross-journal coverage is sparse for general queries.    |
| Library disabled at startup (`[Library] Disabled: ...`)              | MySQL config missing / wrong / unreachable.                           | Check `.env`. The server still runs in passthrough mode — caching and shared state are just turned off.            |

---

## Project conventions

- `scratch/` is git-ignored. Debug scripts, dump HTML, screenshots, etc.
  belong there.
- `.{platform}_profile/`, suffixed profile dirs such as
  `.sd_profile_codex/`, `.repo/`, `.venv/`, `.env` are all git-ignored.
- The `*_scraper.py` modules export a `scraper_instance` singleton
  (`server.py` imports those directly) so we keep a long-lived
  Camoufox/Chromium context per platform per MCP server process.
- Per-platform native IDs (used as the library's primary identity):
    - `ARXIV`: `2605.16230` (the arXiv ID, version suffix kept if present)
    - `IEEE`:  `9266228` (the `arnumber` from `/document/<n>/`)
    - `ACM`:   `10.1145/3597503.3608128` (the DOI, parsed straight from `/doi/...`)
    - `SD`:    `S0263224125035523` (the PII)
    - `CNKI`:  `DBCODE|FileName`
    - `GS`:    SHA1 of the result URL (external link → hashed)
    - `PATYEE`: the `pn=` value
    - `DAWEI`: the `PNM` (publication number)

---

## License

See repository root.
