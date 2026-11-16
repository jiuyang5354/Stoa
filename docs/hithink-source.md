# 同花顺官方 A 股数据源（第一期）

> 上游：同花顺金融数据服务 `hithink-finance`，开源仓库 [Hithink-Tech-Org/Financial-API](https://github.com/Hithink-Tech-Org/Financial-API)（MIT），
> 契约文档 <https://fuyao.aicubes.cn/docs/>，聚合版 <https://fuyao.aicubes.cn/llms.txt>。
>
> 代码：`stock_agent/hithink.py`。

## 开关

| 变量 | 作用 |
|---|---|
| `HITHINK_API_KEY`（或 `HITHINK_FINANCE_API_KEY`） | 唯一开关。留空 = 完全未接入，所有调用点退回原来源 |
| `HITHINK_TIMEOUT` | 单次请求超时秒数，默认 8 |

Key 在 <https://fuyao.aicubes.cn/admin/> 申请。**不要写进代码、日志、提交或可共享配置**，只放本地 `.env`。

## 第一期改变了什么

| 位置 | 行为 |
|---|---|
| `marketdata._sources()` | 配了 Key 才有「同花顺」这一条，且排在最前（官方源优先），其余新浪/腾讯/Yahoo 不变 |
| `marketdata.realtime()` / `quote_check()` | A 股实时报价多一个官方源参与交叉校验；单源失败只记自己那条错误，不影响整张报价表 |
| `serve.market_metadata()` | A 股交易状态改用官方交易日历核验：`calendar_verified: true`，休市时给 `closed_reason`（`holiday` / `weekend` / `outside_hours`）。港股/美股仍标 `false` |
| `edgar.price_history()` | A 股（`.SS/.SZ/.BJ`）优先用官方日线：查具体某天用**不复权**（=当时实际成交价），查区间用**前复权**（涨跌幅/回撤可比）；失败、空数据、港股美股一律回落 Yahoo |

## 明确的边界

- 只覆盖 **A 股**。港股/美股行情、分钟 K、tick、L2、宏观、公告原文、研报原文**都不提供**，继续走 Yahoo / 东财 / 巨潮。
- `null` 表示未披露，客户端原样保留，**不补零**。
- 上游「不限累计调用次数」，但会动态限流：HTTP 429 或 `code=4001`。客户端把这两种都当「这个源这次没数据」，由调用方决定回落，不重试放大压力。
- 没有 Key 时，本仓库的行为与接入前逐字节一致（有回归测试锁定）。

## 验证

```bash
# 全部离线：采用官方响应示例构造的 fixture
python -m pytest tests/test_hithink.py -q

# 配了 Key 之后的一次真机自检（不会打印 Key）
python -c "from stock_agent import hithink; print(hithink.describe())"
python -c "from stock_agent import hithink; print(hithink.snapshot('600519.SS'))"
```

## 后续两期（尚未实现）

1. **第二期（进行中）**：`price_history` A 股分支已接入官方日线；余下 A 股财报 / 龙虎榜 / 市场情绪切官方源。换源会让评测分数漂移，必须先固定来源再改断言。
2. **第三期**：全市场 Parquet（10 年日 K、复权因子）+ 本地 DuckDB，支撑回测与因子研究。
