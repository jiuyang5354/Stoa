# 本地日线库（data/bars.db）

把取过的日线落到本地 SQLite，供回测、因子和区间统计反复查询——不用每次都联网，也不引入新依赖（全仓本来就用 `sqlite3`）。

```bash
python -m stock_agent.marketdb --sync 600519.SS,0700.HK,AAPL --days 400
python -m stock_agent.marketdb --stats
python -m stock_agent.marketdb --sql "select symbol, count(*) n, min(date) a, max(date) b from bars group by 1"
```

## 契约

| 项 | 说明 |
|---|---|
| 文件 | `$STOCK_AGENT_HOME/data/bars.db`（默认就是程序目录下的 `data/`） |
| 表 | `bars(symbol, date, open, high, low, close, volume, turnover, source, currency, fetched_at)`，主键 `(symbol, date)` |
| 表 | `sync_state(symbol, first_date, last_date, rows, source, updated_at)` |
| 幂等 | 重复同步同一段只覆盖，不会产生重复行；新增交易日才是增量 |
| 失败隔离 | 一只标的取数失败只记在返回值里，不影响其它标的 |
| 只读 | `--stats` / `--sql` 不联网；`bars()` 库里没有就返回空列表 |
| 取数 | 复用 `edgar._daily_rows`：A 股配了 `HITHINK_API_KEY` 走同花顺官方日线，否则 Yahoo，再不行走原来的备用链路 |

## 和第三期原计划的差别

原计划是用同花顺的全市场 Parquet（10 年日 K + 复权因子）+ DuckDB。那条路要 API Key，并且要给安装包加 `duckdb` 依赖；现在改成**零依赖的本地 SQLite**，先解决「回测/因子需要反复查历史」这个真问题。

全市场导出仍然是更好的长期方案：等 Key 到位后，`marketdb` 可以加一个 `--import-dump` 分支，把官方 Parquet 直接灌进同库，查询层不用改。
