# Academic MCP Server V2.6 (学术宇宙大模型强力引擎)

欢迎来到 **Academic MCP Server**。
这是一个为大规模语言模型（LLMs）量身打造的高效、突破反爬限制的论文结构化抓取与 Markdown 分析服务器。底层依托 `Camoufox` (极强隐蔽性浏览器引擎)、`Playwright` 以及 `PyMuPDF4LLM`，完美打通大模型与人类学术数据库之间的物理封锁。

## 🌟 核心特性 (V2.6)

- **五大顶尖学术库极限打通**：
  融合 `CNKI` (知网)、`IEEE Xplore`、`arXiv`、`ACM Digital Library`、`ScienceDirect` (Elsevier) 和 `Google Scholar`。
- **降维级反爬对抗机制 (Anti-bot Bypass & HITL)**：
  - **Camoufox 物理拟真**：抛弃原生 Playwright 指纹，全面引入 Camoufox OS/WebGL 随机化指纹防屏蔽。
  - **动态人机接管 (HITL)**：对于 ACM 和 IEEE 等配备地狱级 Cloudflare/DataDome 保护圈的平台，创新性引入了原生**有头可视化 (Headful)** 免拦截降维打击。当普通爬虫仍在 `503` 或弹窗“无限验证码”沉沦时，我们主动抛弃僵硬的无头协议，模拟拥有系统原生分辨率帧率特征的真实人类，几乎免验证直达目标。
  - **ArXiv 前端抓取**：弃用极易被 `503 Service Unavailable` API 限流熔断的 `urllib` 请求机制，将预印本同样全面迁徙至前端 Web 物理渲染层，单次安全吐出百万级检索容量而毫发无伤。
- **全链路自动结构化沉淀**：
  从“查询 -> DOI 摘取 -> 高保真 PDF 静默劫持下载 -> 多模态拆解重排 Markdown（带图表独立分离）”，直接向大模型喂送黄金格式文本。

## 📂 核心代码结构与模块

| 文件名 | 功能描述 |
| :--- | :--- |
| `server.py` | MCP 后台服务器入口，连接 LLM 端点。暴露 `search_papers` 等结构化接口。 |
| `cnki_scraper.py` | 中国知网底层接口爬虫（requests 轻量化处理）。 |
| `ieee_scraper.py` | **原生有头模式 (Headful)**。利用物理特征瞬间击穿防火墙，Preferences 拦截下载。 |
| `arxiv_scraper.py` | **原生强行解析 (Frontend Scraping)**。规避开放 API 请求速率限制。 |
| `acm_scraper.py` | **原生有头模式 (Headful)**。越狱 ACM 极端变态护盾圈 (Cloudflare Turnstile) 的核心隔离脚本。 |
| `sd_scraper.py` / `gs_scraper.py` | 针对 ScienceDirect/Google Scholar。首选全静默无头执行，采用 React/Vue DOM 捕获最大数据块。若遇盾击，则优雅降级由可视化窗口倒计数 60 秒等待人类一键干预。 |
| `.xxx_profile` | **千万留存**。保存各种验证饼干 (Cookies) 与硬件设备标识。 |
| `scratch/` | 我们保留历史调试器、缓存数据的沙盒，它已被 Git 封印，可任意使用。 |

## 🚀 极速部署使用指南

### 1. 环境依赖组装
本系统依托最新的异步事件总线配置：
```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
# 拉取并安装 Camoufox 真实内核环境
python -m camoufox fetch
```

### 2. 启动 MCP 服务
在您的 LLM/智能体端点内注册本 Server 或独立挂起：
```bash
# 请必须使用虚拟环境对应的解释器
.\.venv\Scripts\python.exe server.py
```

## 🛡️ 版本特别守则与警示

1. **绝对禁止退回单纯的无界面 `headless=True` (针对 ACM/IEEE)**：
   不要觉得弹出一个网页窗口“不够后台”。当前的设定是经过最高血泪教训沉淀的最优解（`force_headful=True`）。把它们隐藏在后台盲目前进，只会招致 IP 重度拉黑；而坦荡地展示给反爬探针，反而能利用高完整度的人类指纹享受“绿卡”待遇。
2. **下载拦截内核黑魔法**：
   `ieee` 等下载中强制劫持了浏览器内部 `always_open_pdf_externally` preference 钩子，它把下载动作从“模拟抓取”变成了“浏览器级缓存投递”，确保过盾过程不可分拆。请勿在此路由链随意增减等待事件。
3. **数字极大值寻迹纠错**：
   针对学术网站经常更换 ClassName 导致无法匹配 `total_results` (未知文献数的 bug)，已在 SD/ACM 等全面更换基于 XPath Regex 提取纯数字阵列并 `max()` 的算法。这是鲁棒性的核心护城河。
