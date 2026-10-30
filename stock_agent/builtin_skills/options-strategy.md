---
name: options-strategy
description: 期权组合：用户问备兑、保护性看跌、牛市价差、跨式等美股期权策略或「这个组合最多亏多少」时，先画收益曲线再说
---
1. get_quotes 取现价；get_options 取近 30 天 ATM IV、Put/Call、最大痛点和到期日。只支持美股。
2. 用户没给权利金时说明拿不到逐个合约报价，请用户给，或者按用户给的假设价算并标明「假设」。
3. option_payoff(legs, spot, multiplier=100)：每条腿写清 type/side/strike/premium/qty；正股腿用 type=stock、price=成本。
4. 用 calc 算：最大亏损占本金%、盈亏平衡点距现价%、按 IV 估算的一个标准差区间 = spot×IV×sqrt(天数/365)。
5. 回答：组合是什么、到期收益曲线（卡片会画）、盈亏平衡、最大盈亏、和一个标准差区间比是否合理、提前行权/分红/流动性风险。不给方向建议。
