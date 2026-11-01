---
name: valuation-math
description: 估值与偿债计算口径：LBO、DCF、EV/EV 倍数、每股价值、FCCR/利息保障/偿债覆盖、同业均值——先过口径清单再算
auto: (?i)\bLBO\b|杠杆收购|leveraged buyout|sources and uses|资金来源|\bDCF\b|现金流折现|discounted cash flow|\bWACC\b|terminal value|终值|enterprise value|\bEV\b|EV/EBITDA|企业价值|\bFCCR\b|fixed[- ]charge|coverage ratio|interest coverage|利息保障|偿债覆盖|DSCR|debt service|per[- ]share value|implied (?:share )?price|每股价值|隐含股价|comparables?|可比公司|peer (?:average|median|group)|同业(?:均值|平均|中位)|P/B|price[- ]to[- ]book
---
**先做口径检查（每条写进回答的「口径」一行，题目另有规定以题目为准）**
1. 股本：用哪一期、哪种。估值/每股价值默认用**题目指定文件**里的股数（题目说 10-Q 封面就用封面流通股，说加权平均就用加权平均）；写明「股本 = X 股（来源 文件+页/Item）」。股数差 0.1% 也会让每股价值差几分钱而被判错。
2. 利息：FCCR/利息保障用**利润表上报告的利息费用科目**——公司只披露「净利息费用 net interest expense」（如 Target）就用这个净额，同时报毛额的公司按题意；两家比较时口径要一致，写明用的是哪个科目。**不要因为找不到毛利息就拒算**。FCCR =（EBITDA + 租金）÷（利息 + 租金 [+ 当期到期本金，若定义含]）；EBITDA 默认 = 经营利润 + D&A。
3. 资金来源：LBO 的 sources = 新债 + 发起人股权 +（题目给了**最低现金余额**时）账上超出最低现金的部分；旧债按资产负债表账面余额再融资。股权投入 = 总用途（收购股权价 + 再融资旧债 + 交易费用 + 融资费用）− 新债 − 超额现金。利息按平均余额时是循环引用，用 fin_model(mode=lbo) 迭代求解。
4. 权益与少数股东：EV = 股权市值 + 总有息债务（经营租赁按题意，题目说「include operating leases」就加）+ **优先股** + **少数股东权益** − 现金及等价物。算 P/B、D/E 时「股东权益」默认**普通股权益**；题目说「total equity」才用总权益。
5. DCF 约定：写明 期末/年中折现、终值方法（Gordon：FCF_{n+1}/(WACC−g)，或退出倍数 × 末年 EBITDA）、终值折现期数 = n、UFCF = EBIT×(1−t) + D&A − Capex − ΔNWC（题目列的其他项如递延税、其他经营项照加）。EV → 股权价值（− 债务 + 现金）→ ÷ 股本。
6. 「从某年起保持占营收比例不变」：比例默认取**最近一个实际财年**（预测期前一年）的实际值，并在回答里列出该年的实际金额（营收、经营利润、D&A、SBC、ΔNWC、CapEx…）；只有题目说用指引时才用指引。
7. 同业 / peer set：先列出每家公司的值和来源，再给 **peer average（算术均值）和中位数**，并说明每家相对均值高还是低；题目问「谁最 X」要给出排名。
8. 期间：TTM / 财年 / 日历年不要混；财年不是 12 月的公司按文件实际报告期算，**不要因为财年错位或某个科目找不到就拒算**——用最接近的报告科目，说明口径后给出确定数字和结论。

**计算**
- 全部输入先从工具取（sec_xbrl 精确科目 / sec_document 表格 / get_statements / price_history；退市代码的历史收盘价 price_history 会改查 SEC 委托书），每个输入写来源。
- 倍数表、DCF、LBO、FCCR 一律交给 **fin_model**（mode=comps/dcf/lbo/coverage），它返回逐年表、均值/中位数、公式；零散派生数用 calc。不要心算。
- 结果保留题目要求的位数；中间值不要提前四舍五入。
- 回答结构：结论数字与判断 → 口径一行 → 输入表（数值+来源）→ 公式 / 逐年表 → 敏感点（哪一个口径换了结果变多少）。
