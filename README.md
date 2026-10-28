# 股市 Agent

一个**有记忆**、能自己跑的股市助手，覆盖 **A股 / 港股 / 美股**：拉取指数和自选股行情，算技术信号，可选用大模型写中文点评，每个交易日自动生成早报、晚报并推送到微信 / 企业微信 / 飞书 / Telegram。

## 能做什么

- 指数：上证、深证成指、创业板指、恒生、恒生科技、标普500、纳斯达克、道琼斯
- 自选股：在 `config.yaml` 里随便加
- 每只标的：收盘、单日 / 5 日 / 20 日涨跌、RSI、20/60 日均线
- 信号：单日大涨大跌、RSI 超买超卖、上穿 / 跌破 20 日线、52 周新高新低
- AI 点评（可选）：任何 OpenAI 兼容接口（DeepSeek、通义、OpenAI 等）
- 定时：GitHub Actions 每个工作日北京时间 08:30、21:00 自动跑，报告存进 `reports/`

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
python -m stock_agent memory   # 打印当前记忆
python -m stock_agent review   # 手动复盘到期的判断
```

对话里能做的：查行情和技术指标、看新闻、增删自选股、记住你的持仓、搜以前聊过的内容、给判断并记账、看历史胜率。

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

行情来自 Yahoo Finance；A股 / 港股部分指数历史不全时，自动用东方财富日K补齐，再不行只显示最新价和当日涨跌。数据可能有延迟。

**仅供参考，不构成投资建议。**
