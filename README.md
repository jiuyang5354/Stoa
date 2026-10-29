# Stoa · 股市 Agent

一个**有记忆**、能自己跑的股市助手，覆盖 **A股 / 港股 / 美股**：拉取指数和自选股行情，算技术信号，可选用大模型写中文点评，每个交易日自动生成早报、晚报并推送到微信 / 企业微信 / 飞书 / Telegram。

## 能做什么

- 指数：上证、深证成指、创业板指、恒生、恒生科技、标普500、纳斯达克、道琼斯
- 自选股：在 `config.yaml` 里随便加
- 每只标的：收盘、单日 / 5 日 / 20 日涨跌、RSI、20/60 日均线
- 研究资料（v0.7，免密钥）：个股新闻（东方财富 + Yahoo）、公司公告（东方财富公告中心、美股 SEC EDGAR）、财报与估值（东方财富 F10 + Yahoo Finance）、联网搜索（DuckDuckGo / Bing，可选 Tavily、Brave）、读网页正文
- 记忆图谱（v0.12，v0.13 改为简约神殿风：象牙白拱门、顶光与一圈淡金光环，股票是星，每个主题由掌管它的神守护——财报是雅典娜、AI 是普罗米修斯、降息是克洛诺斯，三个市场是命运三女神）：左上 Graph / Ctrl+G / 输入 /graph。把 USER/MEMORY 条目、技能、判断账本、网页对话按股票、主题（板块与宏观词表）、市场串成力导向图；点股票看相关记忆、对话和判断对错战绩，一键「问 Stoa」。Agent 新工具 memory_links：分析前顺着图谱一次取齐相关记忆。接口 GET /api/graph。全部规则抽取，不调模型
- MCP 客户端（v0.11）：设置 → MCP 服务器，接入本地命令（stdio，如 `uvx mcp-server-fetch`、`npx -y @modelcontextprotocol/server-filesystem`）或远程 URL（Streamable HTTP，/sse 结尾走旧版 SSE），工具在对话里直接可用（mcp__服务器__工具）。可粘贴 Claude Desktop / Cursor 的 mcpServers 配置导入，逐个工具开关；配置存数据目录 mcp.json，`${变量}` 读 .env；失败的服务器 5 分钟内不重试；MCP_TIMEOUT 默认 60 秒
- 多会话（v0.10）：左上角 History（Ctrl+J）打开对话历史，新建、切换、搜索（v0.17 起关键词 + 语义）、置顶、改名、删除；重启后接着聊旧对话，Stoa 带回「会话纪要 + 最近的原文」作为上下文（v0.17 起不再简单截断成最近 40 条）；输入 /new 新建
- 自动更新（v0.9）：启动时检查 GitHub 新版本，设置 → 数据与关于 →「更新并重启」一键换新（数据不动，STOA_UPDATE=off 关闭）；每次发版先在 GitHub 的 Windows 机器上真机自检（双击 Stoa.exe、开独立窗口、静默卸载），过了才挂包
- 价格预警与公告提醒（v0.9）：对话里说「腾讯跌破 450 提醒我」就设好；支持涨到/跌到某价、单日涨跌超某百分比、有新公告；自选股默认有新公告就提醒；约每 5 分钟检查（ALERT_INTERVAL），推到已开的渠道，网页开着会弹出提示
- 记忆会整理（v0.8）：每条记忆带日期，快满时自动合并去重（改写前备份）；做完多步骤分析会把做法存成技能，下次照着做；判断账本按方向、市场、周期统计准确率，找出短板
- 多角色深度分析（v0.15 · Agora）：问「深度分析 X」「该不该买 X」时，四位分析师并行取数 → 多空研究员辩论 → 风控官查你的持仓集中度与止损（可否决）→ 基金经理给出 看多/看空/中性 + 置信度 + 关键依据 + 失效条件，自动记账；网页可展开「辩论纪要」
- 数字强制引用（v0.15 · Citatio）：工具数据带来源与时间戳，回答里的价格、涨跌幅、财务数字后面有 [n] 角标（可悬停、点开原链接），末尾列来源；找不到出处的数字会被打回重写，仍不改就标「未核实」
- 语义记忆 + 长对话压缩 + 财报事件（v0.17）：换个说法也能想起以前说过的话（「离场价」能找到「止损 455」），关键词 + 向量混合检索；对话长了自动把较早的部分滚动摘要成会话纪要，关键数字和你的决定原样保留、带当时的来源；跟踪持仓与自选股的财报日，财报前提醒，财报一出自动写事件研报（实际 vs 一致预期、同比、管理层指引、和你的持仓逻辑对照：加强 / 削弱 / 被证伪）。详见下文
- 更深的数据 + 年报（v0.16）：A股主力/超大单资金流、北向资金、龙虎榜席位、分析师一致预期（目标价区间、评级分布、EPS 预测）、近 1–5 日分钟线、美股期权（隐含波动率、Put/Call、最大痛点）；能读 A股/港股年报 PDF 和美股 10-K，按章节检索段落、对比两年风险因素变化，角标直接跳到 PDF 对应页；报价等关键数字两个数据源交叉校验，差异过大会在回答里如实说明。详见下文
- 回测与评测（v0.14 · Probatio）：时间穿越回测（严格屏蔽未来数据，分市场的方向胜率、置信度校准、多空组合 vs 买入持有 vs 随机 / 一直看多 / 动量基线）+ 32 道固定评测题（报价准确、拒绝编造、风控、记忆、工具选择），没有大模型密钥也能跑，详见下文
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

中转站示例（以 sui-xiang.com 为例，任何 `/v1/chat/completions` + function calling 的中转都一样）：

```ini
LLM_PROVIDER=openai
LLM_BASE_URL=https://sui-xiang.com/v1
LLM_MODEL=gpt-6.1-terra        # 填中转后台里你的分组能用的模型名
LLM_API_KEY=sk-...             # 中转给你的密钥
LLM_CONTEXT=200000             # 中转不报窗口大小，按模型实际窗口填，长对话压缩按它定预算
EMBEDDINGS=local               # 中转没有 /v1/embeddings 时用本地向量，省得每次先失败再回退
LLM_RETRIES=5                  # 中转限流（429）/网关 5xx 时退避重试次数，默认 3
```

中转常限并发：回测 `--concurrency 1–2`，辩论回测慢时加 `--role-timeout 120`。

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
| 对话检索 | `data/sessions.db`（SQLite FTS5 + v0.17 语义向量） | 所有历史对话，「上次说过的」能搜回来，换个说法也行 |
| 会话纪要 | `data/sessions.db` 的 session_digest 表（v0.17） | 长对话较早部分的滚动摘要 + 关键数字与用户决定（带来源） |
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
python -m stock_agent memory reindex       # 重建语义索引（v0.17；平时增量自动建）
python -m stock_agent memory search 离场价  # 试一下混合检索
python -m stock_agent earnings             # 持仓与自选股的财报日历（refresh 立即刷新，report 0700.HK 手动出研报）
python -m stock_agent review   # 手动复盘到期的判断
```

对话里能做的：查行情和技术指标、看新闻和公告、查财报估值、联网搜索并读原文、增删自选股、记住你的持仓、搜以前聊过的内容、给判断并记账、看历史胜率。

## 多角色深度分析（v0.15 · Agora）

普通快问（价格、涨跌、某个指标）仍是一个 Agent 一问一答。问「深度分析一下腾讯」「我该不该买英伟达」「茅台值不值得买」时，Stoa 开一场小型投研会：

1. **分析师（并行）**：基本面（`get_financials`）、技术面（`get_quotes`）、消息与情绪（`get_news` + `get_announcements`）、宏观与行业（`market_overview` + `web_search`），各自只看自己的数据，写一份带证据的短报告（判断 + 置信度 + 3 条证据）。
2. **多空辩论**：多方研究员 vs 空方研究员，第 1 轮各自立论，之后每轮逐条反驳对方上一轮（`rounds` 1–3）。
3. **风控官**：读 USER.md 里的持仓与风险偏好，规则先查一遍（单票仓位 ≥40% 否决加仓、25–40% 降级、组合里另有重仓、保守偏好仓位上限 10%、RSI 超买、止损参考），模型只能比规则更严。否决时不能给看多，降级时置信度下调。
4. **基金经理**：最终 看多 / 看空 / 中性 + 置信度 + 3 条关键依据 + 「什么情况下结论失效」，自动 `record_call` 进判断账本，到期复盘。

**预算与降级**：每次最多调用模型 `max_calls` 次、估算 token 不超过 `max_tokens`、每个角色 `role_timeout` 秒（`config.yaml` 的 `debate:` 段，或 `DEBATE_ROUNDS / DEBATE_MAX_CALLS / DEBATE_MAX_TOKENS / DEBATE_ROLE_TIMEOUT`）。哪个角色失败、超时或撞上预算，就降级为规则摘要（基金经理降级时按分析师投票），整场不会因为一个角色失败而失败，答案里会写明谁降级了。

每场辩论存 `data/debates/<id>.json`（每个角色的状态、用时、取了哪些数据、输出、未核实的数字、预算用量）。网页答案下方有「AGORA · 辩论纪要」，展开看分析师四宫格、逐轮交锋、风控与裁决。接口：`GET /api/debates`、`GET /api/debates/<id>`；模型也可以自己调用 `deep_analysis` 工具。

**回测**：`python -m stock_agent backtest --agent debate [--rounds 2] [--max-calls 600]`。回测版辩论只给技术面分析师截断后的行情（盲测），基本面 / 消息 / 宏观三位分析师禁用、风控只看波动、不记账，和 baseline / llm 单轮用同一套打分直接对比。没有密钥时 `--llm mock` 用规则扮演各角色，只验证管线（mock 结果等同规则 Agent）。

## 数字强制引用（v0.15 · Citatio）

- 每个工具结果登记来源：行情（数据源名 + 行情页链接 + 数据日期）、财报（来源 + 链接）、每条新闻 / 公告 / 搜索结果（标题 + 原文链接 + 时间）、记忆与账本。交给模型的数据里带 `_cite` 编号，模型在关键数字后写 [n]。
- **核对器**（`stock_agent/cite.py`）：从最终回答里抽出价格、涨跌幅、财务数字（跳过日期、天数、股数、代码、小整数），和本轮工具数据比对——四舍五入、万/亿换算、小数↔百分数、两数之差、涨跌幅、股数×差价都算有出处。
- 找不到出处的数字：自动让模型修正一次；还不行就在数字后标「未核实」并在文末提示。有出处但模型没标的数字自动补角标，按出现顺序重新编号，末尾附「—— 来源 ——」清单。
- 网页把 [n] 渲染成小角标：悬停看来源名、数据时间和链接，点击打开原文。

## 更深的数据 + 年报（v0.16）

全部免密钥。每个工具都带 **来源名、链接、数据时间**，接入引用角标；每个尝试过的数据源成败都列在结果里，**多源数字差异过大时写 warning**，回答里必须告诉你「两个数据源不一致」而不是挑一个当真。

| 工具 | 数据 | 数据源（主 → 备 / 交叉校验） |
|---|---|---|
| `get_fund_flow` | A股个股主力 / 超大单净流入（日级，近 N 日，亿元）、5 日合计 | 东方财富资金流 → 新浪财经历史资金流 → 东方财富当日分时；两源重叠日期比对（两家「主力」口径不同，阈值放宽到 35%） |
| `get_northbound` | 北向成交总额、个股北向持股与变化 | 东方财富数据中心。**2024-08-19 起交易所不再披露北向每日净买入**，个股持股改为季度披露——拿不到的字段返回 null 并说明 |
| `get_dragon_tiger` | 龙虎榜：上榜日期、原因、净买额、买入/卖出前五席位 | 东方财富数据中心；很久没上榜会如实说明 |
| `get_consensus` | 目标价区间 / 均值、相对现价空间、评级分布、EPS 预测、近一年研报 | A股：东方财富盈利预测 + 研报列表 + Yahoo；美股：Yahoo + Nasdaq；港股：Yahoo（EPS 币种与股价不同会提示） |
| `get_intraday` | 近 1–5 日分钟 K 线摘要：开高低收、高低点时刻、VWAP、走势点 | Yahoo chart → 东方财富分钟线 → 腾讯分时（A/港，当日），各源最新价比对 |
| `get_options` | 美股期权：最近到期 + 约 30 天到期的 ATM 隐含波动率、Put/Call 比、最大痛点 | Yahoo options（yfinance）→ Nasdaq 期权链（无 IV）；Put/Call 与最大痛点两源比对 |
| `get_quotes`（增强） | 日线收盘之外再拿新浪 / 腾讯 / Yahoo 实时报价比对 | 偏差超过 `QUOTE_DIFF_PCT`（默认 0.5%）写 warning；`QUOTE_CROSSCHECK=off` 关闭 |
| `read_filing` | 年报 / 季报 / 10-K 段落：按章节（管理层讨论与分析、风险因素、主要财务数据、分部收入）定位 + 关键词 BM25 选段，每段带页码 | A股巨潮资讯（失败退东方财富公告 PDF）、港股披露易、美股 SEC EDGAR |
| `compare_filings` | 两年同一章节的新增 / 删除 / 改写句子（带页码）+ 数量统计 | 同上 |

**年报解析**（`stock_agent/filings.py`）：PDF 用 pypdf 抽文本（纯 Python，已进主依赖，Windows 包增加约 1 MB）；港股中文年报常见字体没有编码映射、抽出来是乱码——装了 `pdfminer.six`（可选，`requirements-dev.txt`）就用它重抽乱码页，没装就自动改读英文版。两个库都没有时返回原文链接并提示安装。跳过目录页，章节到下一章（第 X 节 / Item N / 同级小节标号）为止。缓存在 `数据目录/data/filings/`，首次下载解析约 5–20 秒，之后秒开。回答里写「第 N 页」会自动挂上角标，点开就是 `PDF#page=N`（SEC 的 HTML 用文本片段链接定位）。

**深度分析接入**：技术面分析师另看 A股资金流 / 美股期权，消息情绪分析师另看龙虎榜，基本面分析师另看一致预期和最近两年年报风险因素对比（限时 `DEBATE_FILING_TIMEOUT` 默认 25 秒，首次下载超时就跳过、后台继续缓存；`DEBATE_FILINGS=off` 关闭）。

**网页**：答案下方的「Tabulae · 数据」小卡：资金流柱（细线 = 超大单）、一致预期区间条（金线区间、菱形均值、黑线现价）、分时线（虚线 = 昨收、点线 = VWAP）、期权表、北向与龙虎榜表、年报段落（P. N 直达 PDF 页）、年报对比（＋新增 / －删除）、报价校验；每张卡写来源、截至时间，有校验问题时用赤陶色小注标出。接口 `GET /api/cards/<id>`、`GET /api/filings`。

**回测**：这些新数据默认在回测里一律禁用（`backtest.BT_TOOL_POLICY`）：一致预期、期权、分钟线只有当前快照，北向持股与龙虎榜的披露时点无法逐条确认。唯一例外是资金流历史日线——能按日期严格截断，加 `--with-flow` 才给辩论的技术面分析师，且只给主力净占比（盲测不暴露成交规模），测试里证明每个决策点用到的资金流最后日期都等于决策日。

## 语义记忆 · 长对话压缩 · 财报事件（v0.17）

**语义记忆**（`stock_agent/vectors.py`）：历史对话、会话纪要、USER.md / MEMORY.md 条目、判断账本的理由、Agora 辩论纪要摘要都建了向量索引（存 `sessions.db` 的 `mem_items` / `mem_vecs`，float32 blob），检索时 BM25 关键词和向量余弦各取前 50，用 RRF（k=60）融合。

- 嵌入后端可插拔：`EMBEDDINGS=auto`（默认）时，已配置的模型服务商有 OpenAI 兼容 `/embeddings` 就用它（openai、qwen 通义、zhipu 智谱、siliconflow、mistral、together、fireworks、ollama、lmstudio、vllm、hunyuan；见 `providers.EMBED_MODELS`），没有（如 deepseek、kimi、Claude、Gemini 原生）或没密钥就用**本地向量**：中文按字 + 双字、英文按词，哈希到 1024 维的 TF-IDF 向量，外加概念词——股票名字/代码归一（腾讯 = 腾讯控股 = 0700.HK）、图谱主题词表（芯片 ≈ 半导体）、一张金融同义词表（止损 ≈ 离场价 ≈ 割肉、成本 ≈ 买入价、偏好 ≈ 态度…）。纯 numpy，不装 torch，Windows 包大小不变。也可单独指定 `EMBED_PROVIDER` / `EMBED_MODEL` / `EMBED_BASE_URL` / `EMBED_API_KEY`；`EMBEDDINGS=local` 只用本地，`off` 只用关键词。远端失败自动退回本地，10 分钟内不再重试。
- 增量建索引：每次检索前只补新消息和变化的条目，`serve` 里还有后台线程每 2 分钟补一次；`python -m stock_agent memory reindex` 全量重建。
- Agent 的 `session_search` 改走混合检索，新增 `search_memory`（可限定 用户画像 / 笔记 / 判断 / 辩论 / 对话 / 纪要）；`memory_links` 在规则图谱之外补上「语义相关」，规则匹配仍是兜底。

**长对话压缩**（`stock_agent/context.py`）：按模型上下文窗口算预算（`providers.CONTEXT`，没列的默认 32k，`LLM_CONTEXT` 覆盖；预算 = 窗口 × `CONTEXT_RATIO` 默认 0.6 − 工具定义，或直接设 `CONTEXT_BUDGET`）。token 有 tiktoken 就用，没有就按字符估（中文 ≈ 1 字 1 token）。

- 超预算时把较早的消息滚动摘要成「会话纪要」，保留最近几轮原文 + 纪要 + **关键数字与用户决定**（规则抽取，原话保留，助手说过的数字附上当时的来源名和截至时间）；纪要由模型写，没模型或失败时用规则摘要。恢复旧对话时读纪要 + 纪要之后的原文，原文超过 40 条或超预算就继续滚动摘要。
- 纪要也登记为引用来源：压缩后复述早先的数字照样有角标，不会被核对器误判为编造。
- 工具结果过长时按结构收缩（列表取前几条、长文本截断），`_cite`、链接、来源、时间、warnings 原样保留，不再从 JSON 中间硬切。
- 工具循环轮数按预算动态：上限 `config.yaml` 的 `agent.max_tool_rounds`（默认 12，`AGENT_MAX_ROUNDS` 覆盖），上下文预算先用完也会收尾——最后一轮不给工具，让模型基于已拿到的结果作答并说明没查到什么。

**财报事件**（`stock_agent/earnings.py`）：跟踪 USER.md 里的持仓 + 自选股的财报日（A股 东方财富「业绩预约披露」，美股/港股 Yahoo earnings dates），存 `data/events.json`，复用提醒的常驻线程，日历 12 小时刷新一次。

- 财报前 `earnings.remind_days`（默认 3）天推一条提醒：日期、市场预期（EPS 一致预期、目标价区间）、你上次对它的判断与理由。
- 财报发布后（确认已披露，或已过财报日一天，5 天内）自动生成事件研报 `data/reports/events/<代码>-<日期>.md`：实际 vs 一致预期（美/港：季度 EPS；A股：累计 EPS 占全年一致预期的完成度 vs 去年同期完成度）、关键指标同比、管理层指引（财报前后提到指引/展望的公告与新闻；有模型时据此概括）、和你的持仓逻辑对照（USER.md 持仓、账本里该股的历史判断理由、最近一次 Agora 结论）——**加强 / 削弱 / 被证伪 / 基本不变**，规则先判，模型只能改得更保守，且不得出现数据里没有的数字。`earnings.debate: true` 时顺带跑一次 Agora 辩论。
- 推送到已开的渠道（Telegram 等），网页弹出提示并在右栏「Eventus · 财报事件」列出将至的财报日和研报，点开是研报全文（来源角标、原文链接）。对话里也可以直接问「腾讯财报出来了，帮我做个事件研报」（工具 `earnings_report`）或「我的持仓什么时候出财报」（`earnings_calendar`）。
- 接口：`GET /api/events`、`GET /api/events/report/<id>`、`POST /api/events/check`（仅本机）、`GET /api/memory/search?q=`。`EARNINGS=off` 关闭。

**网页**：对话里较早的部分归档后，会出现一条克制的「此前 N 条对话已归档为纪要」金线分隔，点开看纪要；记忆图谱多了金色点线（意思相近的记忆之间的语义边），搜索框在规则没命中时给出「Reminiscentia · 回想」语义结果；History 搜索里语义命中的对话标「≈ 语义」。

## 回测与评测（v0.14 · Probatio）

给 Stoa 的每一次改进一把尺子：同一套题、同一段历史，改前改后各跑一次，看分数变没变。

**时间穿越回测**：回到历史上每个时点，只把截至那天的收盘价给 Agent，让它说 看多 / 看空 / 中性 + 置信度，再用之后 N 个交易日的真实收盘价打分。

```bash
python -m stock_agent backtest                    # 规则 Agent（均线），默认 12 只 A/港/美股，近一年，每 5 个交易日，持有 5/20 日
python -m stock_agent backtest --agent llm --max-calls 200 --concurrency 4
python -m stock_agent backtest --symbols 600519.SS,0700.HK,NVDA --start 2025-06-01 --end 2026-06-01 --step 10 --horizons 5,20
python -m stock_agent backtest --synthetic        # 离线：合成行情跑通管线
```

- **未来数据屏蔽**：行情序列按决策日截断后才交给 Agent；新闻、公告、财报、联网搜索、记忆、对话检索、判断账本在回测里全部不给。大模型 Agent 默认「盲测」：隐去代码和日期、价格归一化到 100，减少它凭训练记忆「背答案」（`--no-blind` 关闭）。
- **指标**（A股 / 港股 / 美股 分开）：方向胜率（不含中性）、含中性的命中率（中性 = 涨跌在 ±1.5%×√(h/5) 内）、按置信度分组的胜率（校准）、多空组合累计收益、买入持有、最大回撤；同时跑三条基线：随机、一直看多、20 日动量。「全部」组合按市场等额分仓，避免不同交易日历的持有期重复计算。
- **大模型**：同一时点 + 同一标的 + 同一模型只调用一次（缓存在 `data/backtests/llm_cache.db`），`--max-calls` 封顶、`--concurrency` 限并发，结果里有调用数、缓存命中、估算 token 与费用（`--price-in/--price-out` 或 `LLM_PRICE_IN/OUT`，每百万 token 美元）。
- 结果：`data/backtests/<id>.json`；装了 matplotlib（`pip install -r requirements-dev.txt`）还会出一张 `<id>.png`。网页：设置 → 回测与评测，或右栏账本下的「Probatio · 回测」，能看最近一次的核心数字、分市场曲线，并在后台发起新回测。

**评测集**：`stock_agent/evals/cases.yaml` 65 道固定题，覆盖报价准确性（回答里的数字要和工具返回一致）、拒绝编造（问不存在的代码）、风控（不承诺收益、有免责）、记忆（记得持仓，跨会话）、工具选择（该查公告时查公告），v0.15 加了引用（角标与来源清单齐全、核对器能抓出编造数字且不误伤推导出的数字、模型幻觉被打回后改掉）和深度分析（走了辩论、风控否决重仓、保守偏好限仓、快问不走辩论），v0.16 加到 57 题：资金流（含港股不支持、北向净买入停披露、龙虎榜久未上榜要如实说）、一致预期与期权、年报问答（带页码）、年报对比、数据源冲突（两源报价 / 目标价不一致必须说出来）；v0.17 加到 65 题：语义记忆（同义改写能召回止损价、偏好、持仓成本、过去的判断理由）、长对话（人为压小预算触发压缩后，仍记得最早的止损价/目标价，复述早先查到的收盘价带角标且通过核对）、事件研报（有预期对比、同比、持仓逻辑结论，研报文件齐全）。工具返回用固定数据，在临时数据目录里跑真正的 Agent，规则打分。

```bash
python -m stock_agent eval            # 有密钥用当前模型，没有就用 mock 跑通管线（mock 分数不代表模型水平）
python -m stock_agent eval --only 记忆 -v
python -m stock_agent eval --judge    # 另请大模型评委给每题 1–5 分（不计入规则总分）
```

结果存 `data/evals/<id>.json`，摘要追加到 `data/evals/history.jsonl`，输出里会和上一次同模型的分数对比。接口：`GET /api/backtests`、`GET /api/backtests/<id>`（`<id>.png` 为图）、`POST /api/backtest`（后台线程，仅本机或带 WEB_TOKEN）、`GET /api/evals`。

测试：`pip install -r requirements-dev.txt && python -m pytest -q tests`。

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
