---
name: earnings-season
description: 财报季研判：财报出来前后，问「财报怎么样」「超预期吗」「财报前要注意什么」时用
---
财报前：
1. earnings_calendar 看日期；get_consensus 拿一致预期（营收、EPS、目标价）。
2. A股看 get_corporate_events(kinds=[forecast]) 有没有业绩预告——有预告时实际业绩往往在预告区间内，预期差主要看区间位置。
3. 美股看 get_options：约 30 天 ATM 隐含波动率 = 市场预期的波动幅度；Put/Call 偏高说明避险需求。
财报后：
4. earnings_report(代码) 生成事件研报（实际 vs 预期、同比、指引、与持仓逻辑对照）。
5. read_filing(section=mdna) 找管理层对下季度的表述；compare_filings 看风险因素新增了什么。
6. 结论：加强 / 削弱 / 证伪用户的持仓逻辑，各给一条最关键的证据。
