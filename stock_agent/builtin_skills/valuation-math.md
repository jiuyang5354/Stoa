---
name: valuation-math
description: 估值与偿债计算口径：LBO、DCF、EV/EV 倍数、每股价值、FCCR/利息保障/偿债覆盖、同业均值——先过口径清单再算
auto: (?i)\bLBO\b|杠杆收购|leveraged buyout|sources and uses|资金来源|\bDCF\b|现金流折现|discounted cash flow|\bWACC\b|terminal value|终值|enterprise value|\bEV\b|EV/EBITDA|企业价值|\bFCCR\b|fixed[- ]charge|coverage ratio|interest coverage|利息保障|偿债覆盖|DSCR|debt service|per[- ]share value|implied (?:share )?price|每股价值|隐含股价|comparables?|可比公司|peer (?:average|median|group)|同业(?:均值|平均|中位)|P/B|price[- ]to[- ]book
---
**先做口径检查（每条写进回答的「口径」一行，题目另有规定以题目为准）**
1. 股本：用哪一期、哪种。估值/每股价值默认用**题目指定文件**（10-K 就用 10-K）里的稀释加权股数或封面流通股——不要换成更新的 10-Q 封面；写明「股本 = X 股（来源 文件+页/Item）」。股数差 0.1% 也会让每股价值差几分钱而被判错。
2. 利息：FCCR/利息保障默认用**毛利息费用**（Interest expense，gross），不是净利息（interest expense − interest income）。FCCR =（EBITDA 或 EBIT + 租金/租赁费用）÷（毛利息 + 租金/租赁费用 [+ 当期到期本金，若定义含]）——先在信贷协议/文件里找定义原文，有定义按定义。
3. 资金来源：LBO 的 sources 只算**新债 + 发起人股权（+ 题目明确说滚存/可用的现金）**；目标公司账上现金**不默认当资金来源**。股权投入 = 总用途（收购股权价 + 再融资债务 + 交易费用 + 融资费用）− 新债。
4. 权益与少数股东：EV = 股权市值 + 总有息债务（含融资租赁按题意）+ **优先股** + **少数股东权益** − 现金及等价物（短期投资按题意）。算 P/B、ROE 时「股东权益」默认**普通股权益**（总权益 − 优先股 − 少数股东）；题目说「total equity」才用总权益。
5. DCF 约定：写明 期初/期末折现（mid-year 与否）、终值方法（Gordon：FCF_{n+1}/(WACC−g)，或退出倍数）、终值折现期数 = n、FCF 定义（UFCF = EBIT×(1−t) + D&A − Capex − ΔNWC）、税率取哪个。EV → 股权价值 → ÷ 稀释股本。
6. 同业均值：先列出每家公司的值和来源再平均；写明算术均值还是中位数，剔除的公司要说为什么。
7. 期间：TTM / 财年 / 日历年不要混；财年不是 12 月的公司，题目按「申报时点」或「最近一期」问时，按文件实际报告期算，**不要因为财年错位就拒算**——说明口径后给数。

**计算**
- 全部数字先从工具取（sec_xbrl 精确科目 / sec_document 表格 / get_statements），每个输入写来源；用 calc 一次算完（{名字: 表达式} 可串联），不要心算。
- 结果保留题目要求的位数；中间值不要提前四舍五入。
- 回答结构：结论数字 → 口径一行 → 输入表（数值+来源）→ 公式 → 敏感点（哪一个口径换了结果变多少）。
