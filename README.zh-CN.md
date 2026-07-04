# Academic MCP Server（中文）

> English version: [README.md](README.md)

一个 Model Context Protocol（MCP）服务器，对外暴露统一的论文检索 +
全文抽取接口，底层覆盖 11 个学术 / 专利来源：

| 代号       | 来源                                       | 浏览器引擎          |
| --------- | ----------------------------------------- | ------------------ |
| `ARXIV`   | arXiv（预印本）                              | Camoufox（Firefox） |
| `CNKI`    | 中国知网                                     | Playwright Chromium |
| `IEEE`    | IEEE Xplore                               | Camoufox（Firefox） |
| `ACM`     | ACM Digital Library                       | Camoufox（Firefox） |
| `SD`      | ScienceDirect（Elsevier）                   | Camoufox（Firefox） |
| `AIAA`    | AIAA Aerospace Research Central           | request-first HTTP + Camoufox 认证 |
| `MDPI`    | MDPI 期刊                                  | request-first HTTP |
| `WOS`     | Web of Science Core Collection            | API-level WoS fetch + Chromium 认证 |
| `GS`      | Google Scholar                            | Camoufox（Firefox） |
| `PATYEE`  | 专利之星 Patyee                              | Playwright Chromium |
| `DAWEI`   | 大为专利（pat.daweisoft.com）                 | Playwright Chromium |

每个来源都包了一层 per-platform scraper 以保证返回结构统一，所有
下载 / 转换都先过一遍本地 MongoDB 论文库：重复搜索走缓存、多个 MCP
客户端共享浏览器认证状态、PDF / Markdown 在磁盘上去重。

整套设计的目标使用方式是作为 MCP 工具接入 LLM Agent（Claude
Desktop、Codex 等），但底层模块单独当 CLI / Python 库用也没问题。

---

## 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  MCP 客户端（Claude / Codex / ...）                                  │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ MCP 工具调用
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  server.py  —  FastMCP 入口，按 platform 派发到对应 scraper           │
│  ┌──────────────────────────┐    ┌────────────────────────────────┐  │
│  │  Library（library.py）   │◄──►│  Scrapers（11 个模块）           │  │
│  │  Mongo: papers,          │    │  - 每平台单例持有一份             │  │
│  │         search_queries,  │    │    Camoufox/Chromium             │  │
│  │         browser_state    │    │    persistent context            │  │
│  │  Files: .repo/{平台}     │    │  - canonical profile 被占用时    │  │
│  │         /{native_id}/    │    │    自动落到 per-PID 副本         │  │
│  └──────────────────────────┘    └────────────────────────────────┘  │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ 可选
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  library_web/  —  FastAPI/Jinja2 管理控制台（默认端口 5577）          │
│  /  /papers  /papers/{p}/{id}  /searches  /browser-state  /file      │
└──────────────────────────────────────────────────────────────────────┘
```

每次 MCP 工具调用都会先穿过 Library：

- **搜索**：对 `(platform, query, filters)` 三元组取哈希；命中缓存
  则直接返回，不启动浏览器。
- **详情 / 下载 / 阅读**：用 URL 抽出 `(platform, native_id)` 作为
  论文 ID。元数据 / PDF / Markdown 已在磁盘上就直接镜像到调用方
  的 `output_dir`，跳过抓取。
- **浏览器状态**：每个 Camoufox scraper 在 platform 维度固定一份
  指纹，并复用过验证后的 cookies（cf_clearance / DataDome / …），
  使得一次手动过验证能跨重启、跨多个并发 MCP 客户端继续生效。

---

## 组件

| 路径                          | 职责                                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| `server.py`                  | FastMCP 服务器。暴露 5 个 MCP 工具并接入 Library。                                                  |
| `runtime_config.py`          | 加载 `mcp_runtime_config.json`，解析项目路径、计算 profile 目录（含 suffix）。                       |
| `mcp_logging.py`             | UTF-8 安全 stderr 打印器，避免 MCP 的 JSON-RPC over stdio 通道被搞坏。                              |
| `library.py`                 | `papers / search_queries / browser_state` 集合的 MongoDB DAO + 文件系统镜像辅助。                   |
| `scraper_utils.py`           | 共享工具：`goto_with_retry`、`venue_matches`、profile 池、指纹 + cookie 共享。                       |
| `pdf_utils.py`               | `convert_pdf_to_markdown()`：用 pymupdf4llm，扫描版 PDF 自动回退到逐页图像方案。                     |
| `{platform}_scraper.py`      | 每个来源一个模块，统一暴露 `search_papers / get_paper_details / download_paper / read_paper_content / close`。 |
| `library_web/`               | 管理控制台的 FastAPI 应用 + Jinja2 模板。                                                          |
| `library_web_{start,stop}.{bat,ps1}` | 控制台启 / 停脚本（端口取自 `mcp_runtime_config.json`）。                                  |
| `.repo/`                     | 文件存储仓库：`.repo/{平台}/{safe_native_id}/paper.pdf` 与 `paper.md`。                              |
| `.{platform}_profile/`       | 每平台一份浏览器 profile。canonical profile 之外可有 per-PID 临时副本。                              |

---

## 系统要求

- **Python 3.10+**（在 3.11 / 3.12 上测过）。3.10 是 PEP 604 联合类型
  语法用到的最低版本。
- **MongoDB 3.6+**，本机可达（在 Docker 的 `mongo:4.4` 上测过）。集合和
  索引在首次启动时自动创建；JSON 形状的字段（`keywords`、`extra`、
  `results`、`filters`、`cookies`、`fingerprint`）以原生 BSON 文档/数组存储。
- **Camoufox 内核**：Windows 下自动 fetch 到
  `%LOCALAPPDATA%\camoufox`，Linux 在 `~/.cache/camoufox`。

---

## 安装

```bash
python -m venv .venv
.\.venv\Scripts\activate          # Windows
# source .venv/bin/activate       # POSIX
pip install -r requirements.txt
python -m camoufox fetch          # 下载 Camoufox 的 patched Firefox 内核
```

照 `.env.example` 拷一份 `.env` 指向你的 MongoDB：

```dotenv
# 完整连接 URI（推荐——支持鉴权 / 副本集 / 各种选项）
MONGODB_URI=mongodb://127.0.0.1:27017
MONGODB_DATABASE=academic_mcp
# 开启鉴权的服务器要带上凭证 + authSource，例如：
# MONGODB_URI=mongodb://user:pass@127.0.0.1:27017/?authSource=admin
```

也可以不设 `MONGODB_URI`，改用离散的 `MONGODB_HOST` / `MONGODB_PORT` /
`MONGODB_USER` / `MONGODB_PASSWORD` 拼装。

库会在第一次启动时自动创建数据库、集合和索引。MongoDB 不可达时 Library 会
明示禁用并打 warning，服务器回退到 passthrough 模式（无缓存 / 无
共享浏览器状态，但功能本身全部能跑）。

---

## 配置

`mcp_runtime_config.json`（已入仓）放非机密的可调项：

| Key                                   | 类型        | 默认             | 用途                                                                                       |
| ------------------------------------- | ----------- | --------------- | ------------------------------------------------------------------------------------------ |
| `playwright_browsers_path`            | string      | `".ms-playwright"` | Playwright 的 Chromium 下载目录。                                                          |
| `override_playwright_browsers_path`   | bool        | `true`          | 启动时强制 export 该路径，避免 Playwright 用错 Chromium 版本。                                 |
| `set_cwd_to_project_root`             | bool        | `true`          | 模块导入时 `chdir` 到项目根，避免 MCP server 里相对路径出问题。                                 |
| `allow_headful_fallback`              | bool        | `false`         | 总开关：遇到反爬封锁时允不允许换 headful 模式人工过验证？                                       |
| `allow_headful_fallback_platforms`    | list        | `["ACM","SD","AIAA","WOS"]` | 允许触发 headful 回退的平台子集。                                                            |
| `manual_verification_timeout_seconds` | int         | `180`           | headful 模式下等用户点完 CAPTCHA 的最长时间（秒）。                                            |
| `library_enabled`                     | bool        | `true`          | MongoDB + 文件系统库的总开关。设 `false` 直接禁缓存。                                          |
| `library_root`                        | string      | `".repo"`       | PDF/MD/图像的存储根（相对项目根或绝对路径）。                                                  |
| `library_web_host` / `library_web_port` | string/int | `127.0.0.1:5577` | 管理控制台的监听地址。                                                                      |
| `profile_suffix`                      | string      | `""`            | 全局共享后缀；可在每个 client 用 `MCP_PROFILE_SUFFIX` 环境变量覆盖（见下）。                    |

机密放 `.env`（gitignored）。目前只有 MongoDB 连接串。

---

## MCP 工具

5 个工具全部注册在 FastMCP server（`server.py`）上，docstring 原文
透传给 LLM。

### `search_papers(query, platform="CNKI", ...)`

| 参数            | 类型           | 描述                                                                                                          |
| -------------- | ------------- | ------------------------------------------------------------------------------------------------------------- |
| `query`        | str           | 搜索词。自由文本；可用每平台原生语法（如 IEEE 的 `("Publication Title":"...")`）。                              |
| `platform`     | str           | `CNKI` / `IEEE` / `ARXIV` / `ACM` / `SD` / `AIAA` / `MDPI` / `WOS` / `GS` / `PATYEE` / `DAWEI` 之一。                      |
| `search_field` | str           | 限定查询字段。每平台口径不同（详见 docstring），默认走该平台的「全部」。                                          |
| `db_scope`     | str           | CNKI 专用：`总库 / 中文 / 外文`。                                                                                |
| `source_type`  | str           | 文献类型过滤（research-article / conference / journal / …），每平台口径不同。                                    |
| `journal`      | str \| None   | 期刊 / 来源限定。服务端结合 URL 级过滤 + 客户端 `venue_matches` 模糊比对（容忍 Google Scholar 的 `…` 截断标记）。 |
| `start_year`   | int \| None   | 发表年下界（含）。                                                                                              |
| `end_year`     | int \| None   | 发表年上界（含）。                                                                                              |
| `sort_by`      | str           | `relevance`（默认）/ `citations` / `date_desc`。                                                                |
| `start_index`  | int           | 分页偏移；如传 20 跳过前 10–20 条。                                                                              |
| `limit`        | int           | 返回最多多少条（默认 10）。                                                                                      |

**返回** JSON 字符串：

```json
{
  "total_results": "1707",
  "papers": [
    {
      "id": "<detail_link 的 8 位 hex 哈希>",
      "title": "...",
      "author": "...",
      "source": "...",                // 出版社的原始来源行（卷期等）
      "venue_name": "...",            // 可抽取时给出规范的期刊 / 会议名
      "doi": "10.1145/...",           // ACM 直接给；其余平台靠 get_paper_details 补
      "date": "March 2026",
      "db_type": "Research article",
      "detail_link": "https://..."
    }
  ],
  "_cache_hit": true                  // 服务端标记，从 MongoDB 缓存返回时为 true
}
```

缓存 key 是 `(platform, 标准化 query, filters, 分页)`。默认永不过期
（初版设计时刻意选了永久缓存）。要强制刷新，从 `search_queries` 删
对应行，或在 web 控制台删。

### `get_paper_details(url, platform="CNKI")`

抓某篇论文的摘要 / 关键词 / DOI。同一篇命中过就走缓存
（`papers.abstract` 列）。返回：

```json
{
  "url": "...",
  "abstract": "...",
  "keywords": ["..."],
  "doi": "...",
  "_cache_hit": true
}
```

`get_paper_details` 已经包了 `goto_with_retry` —— 对瞬时的 Firefox 网
络中止错误（`NS_ERROR_ABORT`、`NS_ERROR_NET_INTERRUPT` …）会做最多
2 次指数退避重试再上抛。

副作用：每次成功抓详情都会把 EndNote 可导入的 RIS 记录写到
`papers.ris_text`（见下方 [RIS 自动入库](#ris-自动入库)）。

### `download_paper(url, output_dir, platform="CNKI")`

下载 PDF。文件已在 `.repo/{平台}/{native_id}/...pdf` 就直接镜像到
`output_dir`。新下载时同时写入仓库正式位置 **和** 镜像给调用方。
返回本地文件路径。

当前下载栈是 request-first。对有反爬验证的平台，浏览器主要用于建立
合法验证态；如果站点要求动态生成签名资源 URL，则浏览器只推进到能
产生该 URL 的页面状态。实际数据获取优先使用直接 HTTP、页内 `fetch`、
响应体捕获或 Playwright download 事件，而不是依赖按钮点击自动化。

平台差异：

- `ACM`：使用验证 cookies 请求 DOI PDF 端点。如果详情页和
  `/doi/pdf/...` 都只暴露摘要 HTML，则归类为 `unavailable`，不视为
  scraper 失败。
- `SD`：搜索 / 详情优先走直接 HTTP。PDF 下载只导航到能触发签名
  ScienceDirect asset 流的位置，随后通过程序侧 download / 响应体捕获
  保存字节。PDF host 的挑战一旦通过，可在出版方令牌失效前被后续请求复用。
- `AIAA`：搜索、详情、PDF、RIS 全部优先直接请求 Atypon 端点
  （`/action/doSearch`、`/doi/...`、`/doi/pdf/...`、
  `/action/downloadCitation`）。浏览器只用于刷新 Cloudflare 验证态；
  直接请求复用该验证态，并使用实测匹配的 Firefox 135 网络指纹。
- `MDPI`：搜索、详情、PDF、RIS 全部走 request-first。scraper 会在
  进程内解析 MDPI 的 Akamai interstitial（`bm-verify` + `pow`），POST 到
  `/_sec/verify?provider=interstitial` 后复用 session cookie 继续请求
  HTML、`/pdf` 和 `/export`，不需要浏览器 warmup。
- `WOS`：仅作为发现 / 高级检索平台。搜索直接调用已侦察出的
  `/api/wosnx/core/runQuerySearch` NDJSON 接口，解析标题、作者、来源、
  DOI、摘要、文献类型和引用次数。`search_field="ADVANCED"` 会启用
  Web of Science 原生高级检索语法，例如
  `TS=("aeroelastic flutter") AND PY=(2020-2026)`；普通字段检索已对齐
  WoS 下拉菜单：`ALL`、`TS`、`TI`、`AU`、`SO`、`PY`、`OG`、`FO`、
  `PUBL`、`DOP`、`AB`、`UT`、`AD`、`AI`、`AK`、`CF`、`DT`、`DO`、
  `ED`、`FG`、`GP`、`KP`、`LA`、`PMID`、`WC`，并为常用英文/中文
  字段名提供别名。普通字段检索也支持多行布尔连接，可传 JSON rows，例如
  `[{"field":"TS","text":"flutter"},{"op":"OR","field":"TI","text":"aeroelastic"},{"op":"NOT","field":"AU","text":"Smith"}]`，
  或多行简写：`TS=flutter`、`OR TI=aeroelastic`、`NOT AU=Smith`。当前同济
  VPN 链路下，普通 Python/curl HTTP 栈在
  TLS 阶段会失败，而 Chromium 能成功，因此实现使用已验证的 Chromium
  profile 在页面上下文中发 API-level `fetch()`；不靠页面按钮点击解析结果。
  WOS 不负责 PDF 下载或全文阅读，应转交 DOI/出版商平台。

### `read_paper_content(url, output_dir, platform="CNKI")`

三级缓存：

1. Markdown 已在库中 → 镜像 MD + images/ 到 `output_dir`，立刻返回。
2. 只有 PDF → 用 `pdf_utils.convert_pdf_to_markdown` 在缓存的 PDF 上
   重跑一遍。
3. 全部 miss → 完整抓 + 存 PDF + MD + images，镜像给调用方。

返回 MD 文件路径 + 前 1000 字预览。

### `convert_local_pdf(pdf_path, output_dir)`

独立工具：把用户给的 PDF 走同样的图像提取流水线转成 Markdown。
不碰 Library。

---

## 论文库（MongoDB + 文件系统）

### 集合（Collections）

```
papers          唯一索引 (platform, native_id)；整数 `id`（供 Web UI 用）
                title, author, source, venue_name, pub_date, db_type, doi,
                detail_link, abstract, keywords (数组),
                pdf_path, md_path, images_dir, extra (子文档),
                ris_text（平台原生导出或合成 RIS），
                created_at, updated_at

search_queries  唯一索引 (platform, md5(query + filters))；整数 `id`
                query_text, filters (子文档), total_results,
                results (子文档数组), fetched_at

browser_state   _id == platform
                fingerprint（子文档：BrowserForge Fingerprint）,
                cookies（数组：Playwright context.cookies()）,
                user_agent, verified_at, updated_at, note

counters        _id == 集合名；seq -> 模拟 AUTO_INCREMENT，让 Web UI 依赖的
                整数 `id` 保持稳定。
```

JSON 形状的字段以原生 BSON 文档/数组存储（不再来回做文本编解码）。
`papers` / `search_queries` 上的整数 `id` 只在真正插入时从 `counters`
集合分配，因此 Web UI 的论文导出 / 搜索删除所依赖的数字键始终稳定。

### 文件系统布局

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

`native_id` 会做文件系统安全的 slug 化。仓库位置是唯一真相源；
每次下载都会再镜像一份到调用方指定的 `output_dir`。

### RIS 自动入库

每次成功取到摘要的 `get_paper_details` 调用都会顺手把一份 EndNote
可导入的 RIS 记录写入 `papers.ris_text`。服务器优先用平台自己的 RIS
导出接口（页码 / ISSN / 会议名更全），平台没接口或接口失败时回退到
基于已存元数据的 `ris_utils.synthesize_ris` 合成。无论哪条路径，这
一列都是幂等写入 —— 命中缓存行不会重抓。

| 平台 | 来源       | 接口 / 策略                                                                                                                                  |
| ---- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| IEEE | 平台原生   | `/rest/search/citation/format`，失败回落 `/xpl/downloadCitations` 表单 POST。                                                                  |
| ACM  | 平台原生   | `/action/downloadCitation?format=ris&include=abs`。                                                                                          |
| SD   | 平台原生   | `/sdfe/arp/cite?format=application/x-research-info-systems` —— 用 `page.evaluate("fetch(url)")` 从浏览器内 JS 上下文发起，让 DataDome 看到的是一次同源 in-app XHR；直接走 `context.request.get` 会被返 403 + 挑战页。 |
| AIAA | 平台原生   | `/action/downloadCitation` 表单 POST，参数 `format=ris&include=abs`。                                                                         |
| MDPI | 平台原生   | `/export` 表单 POST，参数 `articles_ids[]` 和 `export_format_top=ris`。                                                                        |
| ARXIV / CNKI / WOS / GS / PATYEE / DAWEI | 合成 | `ris_utils.synthesize_ris` 按 db_type + venue_name 推断 `TY`，拆 `AU`，从 `pub_date` 解析 `PY/DA`，并加 `AN  - {platform}:{native_id}` 用于回环。 |

批量导出走 web 控制台：在 `/papers` 按状态过滤，勾选复选框，POST
到 `/papers/export-ris`。导出过程中若发现某行 `ris_text` 为空
（例如该平台的合成器是后来才加上的），会当场合成并把结果回写回
库，下一次导出就走缓存了。

---

## 浏览器状态池（并发 + 复用验证）

Firefox / Camoufox 不允许两个进程同时打开同一个 profile 目录。
没协调时，开两个 MCP 客户端（比如 Claude Desktop 一个 + Codex 一个）
要么卡在「Firefox is already running」模态框，要么每个客户端都得
自己过一遍 CAPTCHA。

浏览器状态池一并解决：

1. **池化 profile 目录**。第一个抢到某平台的 MCP server 用
   canonical `.<平台>_profile/`。并发的其余 server 会自动落到 per-PID
   临时副本 `.<平台>_profile__p<pid>/`（`close()` 时连同目录一起清）。
   一个带 PID 的哨兵文件（`.mcp_owner`）防止崩溃残留把新启动锁死。

2. **固定指纹**。Cloudflare 的 `cf_clearance` 和 DataDome 令牌都
   绑定浏览器指纹。我们对每个平台只生成一次 `browserforge` Fingerprint
   （`os="windows"`），pickle+base64 存在 `browser_state.fingerprint`，
   之后每次 `AsyncCamoufox(fingerprint=...)` 启动都加载同一份。
   同一平台的所有并发 client 呈现同一身份。

3. **共享 cookies**。过 CAPTCHA / Cloudflare / DataDome 之后，scraper
   把 `context.cookies()` 抓回 `browser_state.cookies`。下一次启动
   （任何 client、任何 MCP server 进程）在第一次导航 **之前** 用
   `context.add_cookies` 注入。一次人工验证覆盖所有 client，直到
   平台让令牌失效。

`browser_state` 里某一行只要捕获的 cookies 中含已知 clearance cookie
（`cf_clearance`、`datadome`、`__ddg`、`incap_ses`、`reese84`）就会
被打上 `verified_at = NOW()`。

### 局限

`cf_clearance` 绑的是 `(IP, fingerprint, TLS JA3)` 三元组。我们能
固定指纹、本机 IP 不变，但 Camoufox 的 TLS 指纹未必每次启动逐字节
一致。实际效果是绝大多数启动都能复用上次的验证；偶尔在 Cloudflare
策略收紧时还会被迫重过一次。但这仍然远好于不修的状态（每个 client
每次重启都要人工过验证）。

### Web 控制台路由

| URL                                       | 用途                                                                              |
| ----------------------------------------- | -------------------------------------------------------------------------------- |
| `/`                                       | 各平台计数 + 总计。                                                                |
| `/papers`                                 | 分页列表，含平台 / 关键词 / 状态过滤。可勾选行 POST 到 `/papers/export-ris` 批量下载 `.ris`。 |
| `/papers/{platform}/{native_id}`          | 完整元数据 + 摘要 + 已存 PDF / Markdown 链接。                                       |
| `/searches`                               | 已缓存的搜索请求；逐行删除强制重抓。                                                 |
| `/browser-state`                          | 每平台的指纹 / cookies / 验证状态；支持「清 Cookie」/「完全重置」。                      |
| `/file?path=...`                          | `.repo/` 下的静态文件服务，会拒绝路径穿越尝试。                                       |
| `/health`                                 | JSON：`{enabled, reason, library_root}`。                                         |

---

## 运行

### MCP server（生产）

把 server 接进 MCP 客户端的配置。Claude Desktop 的 `mcp.json`
（Codex 的对应文件类似）：

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

`MCP_PROFILE_SUFFIX` 是 **可选** 的。不设 → 走默认行为（一份
canonical profile，第二个 client 自动 per-PID 回退）。每个 client
设成不同的值 → 各拿一套独立 profile（如 `.ieee_profile_claude` /
`.ieee_profile_codex`）。DB 里的 browser state 不管这个后缀都共享。

### MCP server（独立运行，调试用）

```bash
.\.venv\Scripts\python.exe server.py
```

server 通过 stdio 讲 MCP，所以裸跑会一直卡着等 JSON-RPC 输入。
只有从 MCP 客户端连进来才有意义。

### 管理控制台

```bat
library_web_start.bat   :: 在配置的端口起 uvicorn，自动开浏览器
library_web_stop.bat    :: 杀掉该端口上的进程
```

PowerShell 版本（`library_web_start.ps1` / `library_web_stop.ps1`）
也有。控制台读写都开（能删缓存行 / 清浏览器状态），默认只绑
`127.0.0.1`；真要远程访问改 `mcp_runtime_config.json` 里的
`library_web_host`。

---

## 多客户端使用指南

| 场景                                              | 实际发生什么                                                                         | 你该做什么                       |
| ------------------------------------------------ | --------------------------------------------------------------------------------- | ------------------------------ |
| 一次只开一个 MCP client                            | 用 canonical profile、完整缓存，每个平台每个令牌 TTL 周期内只过一次 CAPTCHA。            | 啥都不用。                       |
| 两个 client、同一平台、同一时间                      | 第一个抢到 canonical profile，第二个用 per-PID 临时副本。两个共享 cookies。              | 啥都不用。                       |
| 两个 client、想要完全隔离的 profile                  | 给两个 client 分别设 `MCP_PROFILE_SUFFIX=claude` / `MCP_PROFILE_SUFFIX=codex`。       | 改 MCP client 配置文件。           |
| CAPTCHA 出现、你过了一次                            | cookies 已抓回 `browser_state`；之后的启动会注入它们跳过挑战。                          | 啥都不用。                       |
| 平台直接封了这个指纹                                | 所有 client 同时挂（因为身份一致）。                                                  | `/browser-state` 点「完全重置」。    |
| 只是想清掉过期 cookies                              | `verified_at` 会显示陈旧，新搜索可能再走一遍人工路径。                                   | `/browser-state` 点「清 Cookie」。  |

---

## 验证与性能

最近一次全平台集成冒烟测试：`scratch/full_platform_test.py`，run id
`20260614_full_final`（2026-06-14）。AIAA 为新增平台，单独用 run id
`20260628_aiaa_final_relevance`（2026-06-28）验证；MDPI 用 run id
`20260628_mdpi_retry`（2026-06-28）验证。WOS 作为 discovery-only 平台用
run id `20260704_wos_fields_boolean`（2026-07-04）验证，覆盖
`search_field="ADVANCED"` 的原生高级检索 payload、基础字段下拉映射和
Boolean 多行连接。支持下载的平台最多各测 3 个
新样本下载。这些测试硬失败均为 **0**。`unavailable` 表示出版方或工具语义
没有为该样本暴露可下载 PDF，不计为传输或解析失败。

| 平台 | 搜索 | 详情 | 下载分类 | 阅读 |
| ---- | ---: | ---: | -------- | ---: |
| `ARXIV`  | 6.584 s | 4.976 s | 3/3 OK：15.065、12.841、10.203 s | success，20.405 s |
| `CNKI`   | 1.587 s | 0.198 s | 3/3 OK：1.488、1.479、1.325 s | success，2.178 s |
| `IEEE`   | 4.165 s | 1.149 s | 3/3 OK：29.498、8.352、2.809 s | success，36.236 s |
| `ACM`    | 25.385 s | 3.034 s | 3/3 unavailable：样本文章不暴露 PDF 链接 | unavailable |
| `SD`     | 15.312 s | 4.913 s | 3/3 OK：4.835、5.381、3.819 s | success，22.149 s |
| `AIAA`   | 1.879 s | 0.828 s | 3/3 OK：8.993、5.554、3.004 s | success，14.402 s |
| `MDPI`   | 2.388 s | 0.976 s | 3/3 OK：4.977、11.925、19.477 s | success，7.186 s |
| `WOS`    | 9.026 s | 0.000 s | 设计上不适用 | not applicable |
| `GS`     | 2.443 s | 0.000 s | 设计上不适用 | not applicable |
| `PATYEE` | 1.695 s | 0.374 s | 1/3 OK，2/3 unavailable | success，8.778 s |
| `DAWEI`  | 2.551 s | 0.755 s | 3/3 OK：3.115、3.284、2.598 s | success，4.768 s |

运行说明：

- ACM、SD、AIAA 的部分请求需要有效验证态。可见挑战通过后，后续请求会在
  出版方接受的前提下复用已保存的浏览器身份和 cookies。
- SD PDF 成功率同时受 ScienceDirect 详情页 DataDome 状态和
  `pdf.sciencedirectassets.com` 的独立挑战影响。
- AIAA 的直接程序请求要求请求指纹和生成 `cf_clearance` 的 Firefox
  profile 匹配；当前实现使用 `curl_cffi` 的 `firefox135` 指纹。
- AIAA 按最新日期排序时，部分 2026 书章节会从 `/doi/pdf/...` 返回
  出版方购买页；这类样本按 `unavailable` 处理，不算传输失败
  （`20260628_aiaa_date_sort_classified` 硬失败为 0）。
- MDPI 是开放获取平台，使用轻量 Akamai interstitial；当前实现可纯程序
  完成验证，不需要浏览器 profile warmup。
- WOS 可能触发 hCaptcha / passive verification；遇到
  `Server.passiveVerificationRequired` 时运行
  `warmup_platform_auth(platforms="WOS")` 并在 Chromium 窗口里完成验证。
  验证后搜索走 WoS NDJSON 接口。WOS 在本 MCP 中不是 PDF 来源。
- Google Scholar 在本服务中定位为搜索入口；除非后续为某个结果接入
  下游全文源，否则下载 / 阅读按 not applicable 返回。

---

## 故障排除

| 现象                                                                  | 可能原因                                                            | 修复                                                                                                                  |
| -------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| Camoufox `Failed to launch the browser process / exitCode=0`         | 上次崩溃在 profile 里留了 `parent.lock`。                              | 下次启动 `scraper_utils.acquire_profile` 会自动清。再卡死就手删 `.<平台>_profile/parent.lock`。                              |
| `TargetClosedError: Target page, context or browser has been closed` | Cloudflare 超时把 persistent context 卡死了。                          | 已自处理：`_context_is_alive` 检测到后下次调用前重建。频繁出现就重启 MCP server。                                                |
| `ProfileInUseError: ... already in use by another MCP server`        | 第二个 client 想抢的 profile 被另一个 **活的** PID 占着。                | 关掉另一个 client，或者给当前 client 设个不一样的 `MCP_PROFILE_SUFFIX`。                                                       |
| SD PDF 页面能看到正文但没有保存文件                                     | Firefox 把 PDF 当内置 viewer 打开，没发 download 事件。                    | 已自处理：SD profile 会强制 `application/pdf` save-to-disk，scraper 捕获 download 事件保存。                                  |
| AIAA 直接请求返回 `Just a moment...` / 403                            | Cloudflare cookie 缺失、过期，或绑定到不同浏览器指纹。                       | 运行 `warmup_platform_auth(platforms="AIAA")`；之后 scraper 会用已验证状态走 `firefox135` 直接 HTTP 请求。                   |
| MDPI 返回很短的 Akamai interstitial 页面                               | 当前 session 还没完成 `bm-verify` 检查。                                    | 已自处理：scraper 会解析挑战并 POST 到 `/_sec/verify?provider=interstitial`，随后重试原请求。                                |
| WOS 返回 `Server.passiveVerificationRequired`                          | 当前机构 / IP 会话需要 hCaptcha / passive verification。                     | 运行 `warmup_platform_auth(platforms="WOS")`，在 Chromium 中完成验证后重试搜索。                                             |
| WOS 的 Python/curl 直连在 TLS 阶段失败                                  | 当前同济 VPN / Clash 链路接受 Chromium 网络栈，但普通 TLS client 被断开。      | 属于当前已知限制；scraper 使用已验证 Chromium profile 发 API-level `fetch()`，不做 UI 点击抓取。                              |
| ACM 下载返回 unavailable                                              | 文章页和 `/doi/pdf/...` 都回到摘要 HTML，不暴露 PDF 链接。                | 按出版方不可下载处理，不算 scraper 失败；换一篇 ACM 文章 / DOI 再试。                                                           |
| `_cache_hit=true` 但结果一看就陈旧                                     | 永久搜索缓存返回了一条化石。                                            | `/searches` 删掉该行，重新搜。                                                                                            |
| CNKI 上 `journal=` 过滤返 0 结果                                       | CNKI 的相关性排序未必把目标期刊塞进第 1 页。                              | 加大 `limit`、把 query 改得更具体，或接受 CNKI 在跨期刊覆盖上对宽 query 稀疏的事实。                                            |
| 启动时 Library 被禁（`[Library] Disabled: ...`）                       | MongoDB 配置缺失 / 错误 / 不可达。                                     | 检查 `.env`。服务器仍跑 passthrough 模式，只是缓存和共享状态关掉了。                                                          |

---

## 项目约定

- `scratch/` 已 git-ignore。调试脚本、抓回的 HTML、截图都放这。
- 本地交接笔记（`HANDOFF.md`、`NEXT_SESSION_PROMPT.md`）已忽略，
  不应进入提交。
- `.{platform}_profile/`、`.sd_profile_codex/` 这类带后缀的 profile
  目录、`.repo/`、`.venv/`、`.env` 全部 git-ignore。
- 各 `*_scraper.py` 模块导出一个 `scraper_instance` 单例
  （`server.py` 直接 import），用以让每个平台在 MCP server 进程内
  持有一份长寿命的 Camoufox/Chromium context。
- 每平台原生 ID（论文库主键的身份依据）：
    - `ARXIV`：`2605.16230`（arXiv ID，带 version 后缀就保留）
    - `IEEE`： `9266228`（`/document/<n>/` 里的 arnumber）
    - `ACM`：  `10.1145/3597503.3608128`（DOI，直接从 `/doi/...` 解）
    - `SD`：   `S0263224125035523`（PII）
    - `AIAA`： `10.2514/6.2022-2883`（DOI，直接从 `/doi/...` 解）
    - `MDPI`： `2072-666X/17/7/785`（文章路径；DOI 存入元数据）
    - `WOS`：  `WOS:001064481000002`（Web of Science accession number）
    - `CNKI`： `DBCODE|FileName`
    - `GS`：   原始结果 URL 的 SHA1（外站链接 → 哈希后用）
    - `PATYEE`：`pn=` 参数值
    - `DAWEI`： `PNM`（公开号）

---

## License

见仓库根目录。
