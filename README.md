# Stoa · 股市 Agent

一个**有记忆**、能自己跑的股市助手，覆盖 **A股 / 港股 / 美股**：拉取指数和自选股行情，算技术信号，可选用大模型写中文点评，每个交易日自动生成早报、晚报并推送到微信 / 企业微信 / 飞书 / Telegram。

## 能做什么

- 指数：上证、深证成指、创业板指、恒生、恒生科技、标普500、纳斯达克、道琼斯
- 自选股：在 `config.yaml` 里随便加
- 每只标的：收盘、单日 / 5 日 / 20 日涨跌、RSI、20/60 日均线
- 研究资料（v0.7，免密钥）：个股新闻（东方财富 + Yahoo）、公司公告（东方财富公告中心、美股 SEC EDGAR）、财报与估值（东方财富 F10 + Yahoo Finance）、联网搜索（DuckDuckGo / Bing，可选 Tavily、Brave）、读网页正文
- 记忆图谱（v0.12）：左上 Graph / Ctrl+G / 输入 /graph。把 USER/MEMORY 条目、技能、判断账本、网页对话按股票、主题（板块与宏观词表）、市场串成力导向图；点股票看相关记忆、对话和判断对错战绩，一键「问 Stoa」。Agent 新工具 memory_links：分析前顺着图谱一次取齐相关记忆。接口 GET /api/graph。全部规则抽取，不调模型
- MCP 客户端（v0.11）：设置 → MCP 服务器，接入本地命令（stdio，如 `uvx mcp-server-fetch`、`npx -y @modelcontextprotocol/server-filesystem`）或远程 URL（Streamable HTTP，/sse 结尾走旧版 SSE），工具在对话里直接可用（mcp__服务器__工具）。可粘贴 Claude Desktop / Cursor 的 mcpServers 配置导入，逐个工具开关；配置存数据目录 mcp.json，`${变量}` 读 .env；失败的服务器 5 分钟内不重试；MCP_TIMEOUT 默认 60 秒
- 多会话（v0.10）：左上角 History（Ctrl+J）打开对话历史，新建、切换、全文搜索、置顶、改名、删除；重启后接着聊旧对话，Stoa 会带回这段对话最近 40 条问答作为上下文；输入 /new 新建
- 自动更新（v0.9）：启动时检查 GitHub 新版本，设置 → 数据与关于 →「更新并重启」一键换新（数据不动，STOA_UPDATE=off 关闭）；每次发版先在 GitHub 的 Windows 机器上真机自检（双击 Stoa.exe、开独立窗口、静默卸载），过了才挂包
- 价格预警与公告提醒（v0.9）：对话里说「腾讯跌破 450 提醒我」就设好；支持涨到/跌到某价、单日涨跌超某百分比、有新公告；自选股默认有新公告就提醒；约每 5 分钟检查（ALERT_INTERVAL），推到已开的渠道，网页开着会弹出提示
- 记忆会整理（v0.8）：每条记忆带日期，快满时自动合并去重（改写前备份）；做完多步骤分析会把做法存成技能，下次照着做；判断账本按方向、市场、周期统计准确率，找出短板
- 信号：单日大涨大跌、RSI 超买超卖、上穿 / 跌破 20 日线、52 周新高新低
- AI 点评与对话：DeepSeek、通义、Kimi、智谱、豆包、Claude、Gemini、Ollama 等二十多家模型，行情源可接 Tushare、Twelve Data 等，对外提供 OpenAI 兼容接口和 MCP
- 定时：GitHub Actions 每个工作日北京时间 08:30、21:00 自动跑，报告存进 `reports/`

## Windows 绿色版（不用 Docker、不用装 Python）

到 [Releases](https://github.com/jiuyang5354/Stoa/releases) 下载 `Stoa-Windows.zip`，解压后双击 `Stoa.exe`：自带嵌入式 Python 3.12 和全部依赖，双击直接打开独立的 Stoa 窗口（Windows 自带 WebView2 内核，不开浏览器、无控制台）。第一次运行会用记事本打开 `.env`，填 `LLM_PROVIDER` 和 `LLM_API_KEY` 保存后重开即可。关窗口即退出，日志在 `%LOCALAPPDATA%\Stoa\stoa.log`。数据在 `%LOCALAPPDATA%\Stoa`，换新版不丢。右上角 Settings（或 Ctrl+,）里能改模型和密钥、管理记忆和自选股、开推送渠道、调早晚报时间、导出备份，保存立即生效。卸载就双击包里的 `卸载 Stoa.exe`，可选是否连数据一起删。

自己打包：在 Linux 上运行 `packaging/windows/build.sh`（需要 mingw-w64），产物在 `dist/`。其它系统也能用 `python -m stock_agent desktop` 得到同样的桌面模式。

## 一键部署：独立沙箱常驻运行（推荐）

像 Hermes Agent 一样跑成一个常驻智能体：装进 Docker 隔离沙箱，24 小时在线，记忆存在挂载卷里，重启、升级都不会丢。

```bash
git clone https://github.com/jiuyang5354/Stoa.git && cd Stoa
cp .env.example .env        # 填 LLM_PROVIDER 和 LLM_API_KEY，其余按需
docker compose up -d
```

打开 http://localhost:8080 就能聊天：顶部滚动行情条，右侧可切换 A股/港股/美股 的走势卡片（悬停看每日点位）和它记住的你。界面控件参考 21st.dev（Ticker、Market Snapshot、Sparkline、AI Chat Input、Suggestions、Text Dots），原生重写，无需前端构建。

容器里同时跑着：

- **网页聊天**：带记忆面板；放到服务器上设置 `WEB_TOKEN`，用 `http://host:8080/?token=口令` 访问
- **Telegram 网关**（可选）：填 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，只回复白名单里的人
- **定时早晚报**：工作日 08:30 / 21:00（北京时间，`REPORT_TIMES` 可改），推送到已配置的渠道
- **沙箱代码执行**：`ALLOW_CODE=1` 时 Agent 可以自己写 Python 做回测、算相关性等分析

所有渠道共享同一套记忆。闲置 30 分钟自动结束会话并整理记忆；聊天里发 `/new` 立刻整理并开新对话，`/memory` 查看记忆。

**沙箱隔离**

| 措施 | 说明 |
|---|---|
| 非 root 用户 | 容器内以 uid 10001 运行 |
| 只读根文件系统 | 只有 `/data` 和 `/tmp`（256 MB 内存盘）可写 |
| 去掉全部 Linux capabilities | `cap_drop: ALL` + `no-new-privileges` |
| 资源上限 | 1.5 CPU、1.5 GB 内存、256 个进程 |
| 代码执行再隔离 | 独立子进程、60 秒超时、1 GB 内存上限、看不到任何密钥环境变量 |
| 端口只绑本机 | 默认 `127.0.0.1:8080`，对外暴露需自己配 |

数据都在 Docker 卷 `agent-data`（容器内 `/data`）：`memory/`（记忆）、`data/sessions.db`（对话与判断账本）、`config.yaml`（自选股）、`reports/`、`workspace/`（沙箱文件）。备份：`docker cp stoa:/data ./backup`。

## 外接 API（全部兼容）

运行 `python -m stock_agent providers` 查看当前用的模型和行情源。

**大模型**：`.env` 里设 `LLM_PROVIDER` + `LLM_API_KEY` 就行，工具调用在各家之间自动转换。

| 协议 | 预设 |
|---|---|
| OpenAI 兼容 | deepseek · openai · qwen（通义）· kimi · zhipu（智谱）· doubao（豆包）· hunyuan · minimax · stepfun · baichuan · siliconflow · openrouter · groq · together · fireworks · mistral · xai · perplexity |
| 本地 | ollama · lmstudio · vllm |
| 原生 | anthropic（Claude）· gemini · azure |

任何别的 OpenAI 兼容服务（中转站、One API、自建网关）：`LLM_BASE_URL` 填地址即可。`LLM_FALLBACK=qwen,ollama:qwen2.5:7b` 让主模型挂了自动换。

**行情源**：`DATA_PROVIDERS` 按顺序尝试，默认 `yahoo,eastmoney`（免费、免密钥）。

| 源 | A股 | 港股 | 美股 | 指数 | 密钥 |
|---|---|---|---|---|---|
| tushare | ✓ | ✓* | ✓* | A股指数 | TUSHARE_TOKEN |
| twelvedata | ✓* | ✓* | ✓ | ✓ | TWELVEDATA_API_KEY |
| alphavantage | ✓ | ✓ | ✓ | – | ALPHAVANTAGE_API_KEY |
| finnhub | ✓* | ✓* | ✓* | – | FINNHUB_API_KEY |
| polygon | – | – | ✓ | 美股* | POLYGON_API_KEY |
| fmp | ✓* | ✓* | ✓ | ✓* | FMP_API_KEY |
| tiingo | – | – | ✓ | – | TIINGO_API_KEY |
| custom | 自建接口 `DATA_CUSTOM_URL` | | | | 可选 |

\* 需要对应套餐的权限。比如 A股为主可以写 `DATA_PROVIDERS=tushare,yahoo,eastmoney`。

**对外接口**（`serve` 模式，口令用 `Authorization: Bearer <WEB_TOKEN>`）：

- OpenAI 兼容：`http://host:8080/v1`，模型名 `stoa`。Cherry Studio、LobeChat、NextChat、Open WebUI、沉浸式翻译这类能填自定义 OpenAI 地址的软件都能直接把它当模型用，记忆照样生效。
- MCP：远程填 `http://host:8080/mcp`；本地 Claude Desktop / Cursor 配置：
  ```json
  {"mcpServers": {"stoa": {"command": "python", "args": ["-m", "stock_agent", "mcp"], "cwd": "/path/to/stoa", "env": {"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "sk-..."}}}}
  ```
  提供 get_quotes、market_overview、get_news、get_announcements、get_financials、web_search、fetch_url、memory、session_search、watchlist、record_call、call_stats，配了大模型还有 ask_agent。
- REST：`POST /api/chat` `{"text","session"}`、`GET /api/indices`、`GET /api/memory`、`GET /api/report`。

**推送**：Telegram、Server酱、PushPlus、企业微信、飞书、钉钉、Bark、ntfy、Discord、Slack、通用 Webhook，填哪个推哪个。

## 记忆（参考 Hermes Agent）

和它聊得越多，它越懂你：

| 层 | 文件 | 存什么 |
|---|---|---|
| 用户画像 | `memory/USER.md`（上限 1375 字） | 持仓与成本、风险偏好、投资风格、关注板块、沟通习惯 |
| Agent 笔记 | `memory/MEMORY.md`（上限 2200 字） | 复盘得出的经验、对个股的长期观察、你纠正过它的地方 |
| 对话检索 | `data/sessions.db`（SQLite FTS5） | 所有历史对话，「上次说过的」能全文搜回来 |
| 判断账本 | `data/sessions.db` 的 calls 表 | 它给出的每个看涨/看跌判断，到期自动对照现价打分 |

- 两份记忆每次对话都自动注入；满了必须合并精简，不会无限膨胀。
- Agent 自己决定存什么：你透露持仓、偏好时当场写入；每 6 轮提醒自己回顾一次；退出时再整理一遍。
- **学习闭环**：判断到期后自动复盘，判断错了就把教训写进 MEMORY.md，下次带着教训分析。
- 早晚报的 AI 点评也会读这两份记忆，结合你的持仓来写。
- 记忆是纯文本，随时可以手动改；持仓属于隐私，建议仓库设为 Private。

```bash
python -m stock_agent chat     # 对话（需要 LLM_API_KEY），/memory 查看记忆，/exit 退出并整理记忆
python -m stock_agent serve    # 常驻模式（网页 + Telegram + 定时报告），不用 Docker 也能跑
python -m stock_agent memory   # 打印当前记忆
python -m stock_agent review   # 手动复盘到期的判断
```

对话里能做的：查行情和技术指标、看新闻和公告、查财报估值、联网搜索并读原文、增删自选股、记住你的持仓、搜以前聊过的内容、给判断并记账、看历史胜率。

## 本地运行

```bash
pip install -r requirements.txt
python -m stock_agent --session evening          # 生成报告到 reports/
python -m stock_agent --no-llm --stdout          # 只打印，不调大模型
python -m stock_agent --json                     # 输出 JSON 快照
LLM_API_KEY=sk-xxx python -m stock_agent --push  # 带 AI 点评并推送
```

## 加自选股

编辑 `config.yaml`，代码用 Yahoo Finance 格式：

```yaml
watchlist:
  - {symbol: "600519.SS", name: "贵州茅台"}   # 上交所 .SS
  - {symbol: "000858.SZ", name: "五粮液"}     # 深交所 .SZ
  - {symbol: "0700.HK",   name: "腾讯控股"}   # 港股 .HK
  - {symbol: "NVDA",      name: "英伟达"}     # 美股
```

## 开启自动运行

仓库 Settings → Secrets and variables → Actions，按需添加：

| Secret | 作用 |
|---|---|
| `LLM_API_KEY` | 大模型密钥，不填就不写 AI 点评 |
| `LLM_BASE_URL` / `LLM_MODEL` | 覆盖 config.yaml 里的接口和模型 |
| `SERVERCHAN_KEY` | Server酱，推送到微信 |
| `WECOM_WEBHOOK` | 企业微信群机器人 |
| `FEISHU_WEBHOOK` | 飞书群机器人 |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram |

Actions 页面也可以手动点 “Run workflow” 立即生成一份。

## 数据说明

行情来自 Yahoo Finance；A股 / 港股部分指数历史不全时，自动用东方财富日K补齐，再不行只显示最新价和当日涨跌。新闻、公告、财报来自东方财富、SEC EDGAR 和 Yahoo Finance，联网搜索默认 DuckDuckGo。读网页只允许公网地址。数据可能有延迟。

**仅供参考，不构成投资建议。**

## 素材

- 首页胸像：Poly Haven「Marble Bust 01」，作者 Rico Cilliers，CC0（stock_agent/assets/bust.glb，已裁成只含网格 + 1k 法线贴图）。
- 背景岩壁与 3D 神庙石材：ambientCG「Travertine 009」（https://ambientcg.com/view?id=Travertine009），CC0。stock_agent/assets/travertine009_color.jpg 为原 1K 颜色图（重新压缩）；travertine009_normal.jpg 为原 NormalGL 叠加由颜色图亮度推导的浅浮雕法线（原法线图几乎是平的，这块是抛光板材）；travertine009_roughness.jpg 为原粗糙度图拉伸到 0.45–0.85 并缩到 512（原图接近镜面）。
- 首页标题字体：Archeologicaps（dafont，标注 100% Free），stock_agent/assets/archeologicaps.woff2，已子集化为 ASCII。
