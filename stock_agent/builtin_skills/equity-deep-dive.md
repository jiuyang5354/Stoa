---
name: equity-deep-dive
description: 个股深度研报：用户要「深度研究/写研报/把这家公司讲清楚」时用，比个股体检更重基本面，所有派生数字代码硬算
---
目标：一份能拿给别人看的研报——业务怎么赚钱、钱花在哪、和预期比怎样、最大的风险是什么。每个数字带出处，派生数字用 calc。

1. 记忆：memory_links(股票)，用户的持仓逻辑写在开头「你的逻辑」一节，后面逐条对照。
2. 业务与分部：read_filing(section=segment) 取分产品/分地区收入；用 calc 算各分部占比和同比（share、pct）。
3. 利润结构：financial_bridge——营收到净利每一步的差额和毛利率/净利率；get_financials 补近几期趋势；用 calc 算变化（百分点）。
4. 管理层怎么说：read_filing(section=mdna) 取 2–3 段关键表述；compare_filings 看今年比去年多了哪些风险。
5. 预期与估值：get_consensus（目标价区间、EPS 预测）；用 calc 算当前价相对目标均价空间、PE 相对自身历史。
6. 事件与资金：get_corporate_events（解禁/减持/回购/质押或内部人交易）；A股加 get_chips、get_fund_flow。
7. 关键价位：price_levels，只作为参考位写一行。
8. 结构（固定）：
   一、一句话结论 二、业务与分部 三、利润结构（引用瀑布图） 四、管理层表述变化 五、预期与估值 六、事件与资金 七、风险清单（按严重程度） 八、和你的持仓逻辑对照（加强/削弱/证伪）。
   方向判断放最后一句，注明择时不显著。
9. 用户要存档时 earnings_report 或 skill 外的笔记用 memory 记一句结论（不记数字）。

坑：港股分部数据常在年报 PDF 里，read_filing 要等十几秒；A股季报是累计口径，单季要用 calc 相减。
