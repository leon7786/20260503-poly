# 20260503-poly

Polymarket 15分钟 UP/DOWN 量化策略研究记录。

这个仓库用于记录不同策略版本的设计、假设、执行逻辑、风控规则和未来实现注意事项。

## 版本列表

- [`v1-binance-aggtrade-direct-fok-cooldown-stop`](./v1-binance-aggtrade-direct-fok-cooldown-stop/) — Binance `aggTrade` 穿越 open 后，直接发 Polymarket 受价格保护的 FOK/FAK 订单，成交后 2 秒冷却，再检查止损。
- [`v2-trade-lead-profit-maximizer`](./v2-trade-lead-profit-maximizer/) — 全新设计：以 Binance `@trade` 原始成交流为核心领先信号，用微爆发评分、动态价格 cap、直接 FOK/FAK、2 秒冷却、止损/止盈和延迟漏斗日志来最大化利润。

## 核心原则

不要虚构成交。研究、paper trading 和实盘日志必须分开记录：

1. 信号触发时间；
2. Polymarket 订单薄状态；
3. 直接下单尝试；
4. fill / reject / cancel 响应；
5. 入场后的止损、止盈或结算结果。

如果没有真实订单或真实成交，就直接显示空状态、取消状态或失败状态，不把本地模拟当成真实进展。
