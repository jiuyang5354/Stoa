# position_limit 验证报告（v0.40.1）

对象：`stock_agent/position.py`（v0.40 引入，本次只做加固与验证，不改算法）
日期：2026-11-02
原则：**做成的写结论，没做成的写「未验证」并说明原因。**

## 一、证据表

| # | 证据类型 | 做了什么 | 结果 |
|---|---|---|---|
| 1 | **随机不变量** | `test_invariants_hold_on_random_market_conditions`：5000 组随机游走行情 × 随机资金（1 万–5000 万）/ 三档风险 / 随机止损幅度 / 随机已持仓，覆盖 A 股、港股、美股、科创板 | **通过**，六条不变量零违反 |
| 2 | **脏参数** | 7 类非法输入：`capital=Infinity / NaN / "100000" / -5`、`held=Infinity`、`stop_pct=NaN / -3` | **已修**：原来 `Infinity` 会抛 `ValueError`、`held=Infinity` 抛 `OverflowError`，现在统一当成「没给」 |
| 3 | **手算样例** | 上游已有 12 个用例（单票上限/流动性/风险预算三路取小、按手取整、港股未知每手、一手超上限、已持仓扣减、无资金只算流动性） | **通过**，本次未改动其断言 |
| 4 | **引用可回查** | `position_limit` 结果登记为可复算来源，正文里「单票上限 200000 元 / 止损参考 96 / 最多亏 8000 元」全部通过 `cite.verify` | **通过**（上游用例 `test_registered_as_recomputable_source_and_numbers_verify`） |
| 5 | **端到端评测** | `risk-07`（20 万稳健买茅台）、`risk-08`（50 万港元保守买腾讯、止损 8%） | 见「未验证」一栏 |
| 6 | **工具面** | 描述长度 < 100 字、只在提到资金/仓位时预选、不相关提问不预选 | **通过**（上游用例） |

## 二、未验证 / 已知边界（照实写）

| 项 | 状态 | 原因与处置 |
|---|---|---|
| `risk-07` / `risk-08` 真实模型得分 | **未验证** | 今晚只剩 Hermes 额度可用；这两题在 v0.40 里已加入但本地从未跑过真实基线，见「三」 |
| 冲击成本 | **未验证** | 只有日线，没有逐笔与盘口；流动性上限用的是「20 日均成交额 × 1%」，不是冲击成本模型 |
| 港股每手股数 | **未取到** | 工具按 1 股取整并写明 `lot_note`；真实每手（如腾讯 100 股/手）会让「不足一手」的判断偏乐观 |
| 科创板最小 200 股 | **按 100 取整** | 已在 `lot_note` 说明；未实现「200 股起、之后 1 股递增」的规则 |
| 交易日历 / 停牌 / 涨跌停 | **未做** | 工具不判断当天能否成交，只给算术上限 |
| PBO / Deflated Sharpe | **不适用** | 本工具是单笔仓位，不产生策略收益序列 |

## 三、这次修掉的三个裂缝

| 输入 | 修复前 | 修复后 |
|---|---|---|
| `capital=Infinity` | `ValueError: cannot convert float NaN to integer` | 当作没给资金 → 只算流动性上限 |
| `held=Infinity` | `OverflowError: cannot convert float infinity to integer` | 当作没持仓 |
| `capital="100000"` | 静默忽略 | 按 100000 处理（与传数字等价，有断言） |

来源：模型在工具参数里写 `Infinity` 或数字字符串并不罕见；`run_tool` 里的 `float()` 会把它变成 `inf`，
再进 `int(x // lot)` 就崩。修法是入口统一过 `pos_num()`：非有限值、0、负数、非数字字符串一律落到 `None`。

## 四、怎么复现

```bash
STOCK_AGENT_HOME=$TMPDIR/repro python -m pytest tests/test_position.py -q
```

`docs/position-manifest.json` 记录了三份输入文件的 sha256、全部默认参数与随机种子（20261102）。
同样的哈希与种子重跑，随机不变量用例必须逐例通过。
