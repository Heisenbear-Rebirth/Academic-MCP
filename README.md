# Academic MCP Server (学术宇宙大模型能力扩展)

欢迎来到 **Academic MCP Server**。
这是一个为大规模语言模型（LLMs）量身打造的高效、突破反爬限制的论文结构化爬取抓取与 Markdown 分析服务器。它基于官方的 Model Context Protocol (MCP) 标准，底层依赖强力的 `Playwright` 内核以及最高级的 `PyMuPDF4LLM` 架构。旨在打通大模型与人类五大顶级学术数据库之间的物理与交互壁垒。

## 🌟 核心特性

- **五大顶尖学术库无缝集成**：
  一键支持 `CNKI` (中国知网)、`IEEE Xplore`、`arXiv`、`ACM Digital Library` 以及最难攻克反爬图灵的 `ScienceDirect` (Elsevier)。
- **降维免感级反爬绕过 (Anti-bot Bypass)**：
  原生内置浏览器上下文持久化。针对 ACM (Cloudflare) 以及 ScienceDirect (DataDome) 设置了拟真无头化与环境隔离机制。一次点击“我不是机器人”，免疫数周验证拦截。
- **全链路自动结构化沉淀**：
  提供从“自然语言查询 -> 提取 DOI & 摘要元数据 -> 将原始高保真包含插画图片的 PDF 静默下载 -> 二次分解重排为大模型最容易吸收的图文 Markdown”。
- **动态深度遍历与分页映射**：
  提供对如“学科筛选”、“文档类型细分”、“深度分页抓取”的底层字段转化包装。

## 📂 核心代码结构与必备调试器

| 文件名 | 功能描述 |
| :--- | :--- |
| `server.py` | MCP 后台服务器入口，暴露 `search_papers`, `get_paper_details`, `download_paper`, `read_paper_content` 给大语言模型客户端调用。 |
| `cnki_scraper.py` | 中国知网抓取爬虫底层（通过 requests 轻量化封装）。 |
| `ieee_scraper.py` | IEEE 平台搜索与基于底层浏览器 Preferences 强制劫持 PDF 下载流的免验证抓取引擎。 |
| `arxiv_scraper.py` | 针对物理、数学预印本的高速免限制 API 原生抓取器。 |
| `acm_scraper.py` | 攻破 ACM Cloudflare 无感验证网段的 Playwright 隔离脚本。 |
| `sd_scraper.py` | 针对 ScienceDirect 极端动态架构与 DataDome 拦截圈定制的爬虫。集成 React/Vue 挂载等待与隐式 md5 签名动态抓取，物理等效模拟真实点击重定向。 |
| `sd_auth.py` | **必要调试器**。针对 SD 平台，如长久未使用被 DataDome 拦截或 IP 限流时，用来初始化和物理固化人类凭证的单次鉴权脚本。 |
| `test_all_platforms.py` | **必要验证器**。终极全局压测程序。一键检测系统下所有 5 个端点能否正常执行检索、PDF提取和图文解偶转换（且无乱码报错）。 |
| `.xxx_profile` (隐蔽目录) | 系统自动生成，用以储存和接管 Chromium 物理核心。请保留，这是越狱 Cloudflare 和 DataDome 的核心通行证数据。|

## 🚀 极速部署使用指南

### 1. 环境依赖组装
本系统依托最新的异步事件总线和本地 PDF 视网膜映射转换库：
```bash
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. 授权您的第一次“人机认证通行证”（可选但极度推荐）
针对类似 ScienceDirect 这样严格的学术库，强烈建议通过以下程序获得您的 DataDome 免受控上下文：
```bash
# 执行后只需在弹出的小窗里手动完成一次算术或点选验证码
python sd_auth.py
```

### 3. 连接至大模型与 MCP 客服端
启动服务器后台驻留在任何终端中：
```bash
python server.py
```
或者在您的 `claude_desktop_config.json` 或此类 MCP 客户端配置中直接接入本仓库的 python 环境命令。

## 🛡️ 对于未来修改与反爬升级的核心警示

1. **绝对禁止纯无头模式 (`headless=True`)**：目前的抓取均基于带有 UI 渲染管线的物理浏览器实例加载（配置了 `headless=False` 等），一旦将其降级为真・无头模式，将立即触发目标学术机构（如 ScienceDirect / IEEE）的终身 IP 黑盾拦截。
2. **禁止使用 `--start-minimized`**：实测表明，将应用窗口在启动时强制最小化会大幅增加 DataDome 的检测阈值（系统无法生成有效的鼠标与屏幕绘图区指纹），因此系统已隐去该参数，在后台保留一个物理窗体是完全必要的！
3. **Chromium 内部 Preferences 偏好劫持**：所有 PDF 下载现已完全抛弃不可靠的 `fetch` API 或 DOM 模拟点击。代码会在启动前向 `Profile\Default\Preferences` 强行写入 `{"plugins": {"always_open_pdf_externally": true}}`。请勿随意更改 `sd_scraper.py` 或 `ieee_scraper.py` 内的 `window.location.href` 重定向路由，这是触发浏览器底层下载探针、完美旁开 Cloudflare WAF 的最后杀招。
