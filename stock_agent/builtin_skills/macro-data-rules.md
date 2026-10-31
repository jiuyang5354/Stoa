---
name: macro-data-rules
description: 宏观/历史数据取数口径：FRED 序列与数据版本（vintage）、利率目标区间 vs 有效利率、季调与频率、周/月起止日、区间涨跌基准
auto: (?i)\bFRED\b|ALFRED|vintage|data (?:from|as of|released on) |seasonally adjusted|\bSA\b|\bNSA\b|federal funds|fed funds|联邦基金|目标区间|target range|effective rate|有效利率|bank credit|H\.8|经常账户|current account|registrations|Ifo Business|最大单周|单周(?:涨|跌)|weekly (?:gain|drop|change)|week(?:ly)? (?:return|decline)|区间涨跌|最大回撤|统计年鉴|国家统计局|申万|行业指数
---
**取数前先确定这五件事（写进回答「口径」一行）**
1. 序列：同一指标常有多条序列（季调/非季调、月度/周度/季度、存量/流量、本币/美元）。按题目字面挑：「seasonally adjusted」→ SA；「annual」→ 年度序列或按题意加总/平均（流量加总、存量取期末、比率取均值），并写明是哪种。macro_series 搜到多条时比较 title/units/frequency 再选，题目给的数值量级可作校验。
2. 数据版本：题目写「data from 2025-07-18」「as published on…」→ macro_series 加 vintage=那天（ALFRED），不要用最新修订值。
3. 利率：问「federal funds rate」的某月某次决议 → 答 **FOMC 目标区间**（如 3.00%–3.25%）；只有题目说 effective（EFFR/DFF）才给有效利率，必要时两个都给并说明哪个是答案。
4. 中国数据：国家统计局/统计年鉴口径优先（cn_data kind=nbs，取不到按 hint 用年鉴原文）；经常账户等国际收支用外汇局口径（亿美元），注意 FRED 上的同名序列可能是 OECD/IMF 口径、单位不同。
5. 指数：申万行业用 cn_data sw_index / sw_rank，注意 2014 版与 2021 版行业不同（题目写 2014 就用 version=2014）。

**时间边界**
- 单周涨跌 = 本周最后一个交易日收盘 ÷ 上周最后一个交易日收盘 − 1；报「哪一周」时写**起点 = 上周最后一个交易日、终点 = 本周最后一个交易日**（如 2008/10/10–2008/10/17），并说明这种写法。
- 区间涨跌默认以 start 前一交易日收盘为基准（price_history range.change_pct_from_prev_close / sw_index 同名字段）；题目说「从 X 日收盘」就以 X 日收盘为基准。两种都算出来时写清用的是哪个。
- 非交易日：「截至某日」用该日或之前最后一个交易日；「某日之后第一个交易日」往后找。
- 连续 N 天上涨、多少天低于某值这类计数：price_history / cn_data 加 full=true 拿完整序列，用 run_python 读返回的 csv 计算，不要从摘要推。
