# 20260503-poly — Polymarket Strategy Versions

这个仓库按版本目录保存 Polymarket 15m Up/Down 策略设计与 dry-run/production 代码。

## Versions

- [`v4-source-relative-fastest-dryrun`](./v4-source-relative-fastest-dryrun/) — **当前最新 v4**：多交易所最快报价源 + timestamp/source-relative open；Binance `@trade`、Coinbase `market_trades`、Bybit `orderbook.1` mid、OKX `books5` mid 同时监控；final 10s 任一 source 穿越自己的 round open 即 dry-run FAK/FOK。
- [`v3-multi-factor-dynamic-cap-takeprofit`](./v3-multi-factor-dynamic-cap-takeprofit/) — 多因子复合信号 + 动态价格上限 + 分批止盈 + 多币种择优，以利润最大化为目标。
- [`v2-trade-lead-profit-maximizer`](./v2-trade-lead-profit-maximizer/) — 以 Binance `@trade` 原始成交流为核心领先信号，用微爆发评分、动态价格 cap、直接 FOK/FAK、2 秒冷却、止损/止盈和延迟漏斗日志来最大化利润。
- [`v1.1-clob-sdk-fak-midexit`](./v1.1-clob-sdk-fak-midexit/) — 跳过 orderbook，直接用 py-clob-client SDK 发 FAK 订单，支持中途平仓止损。
- [`v1-binance-aggtrade-direct-fok-cooldown-stop`](./v1-binance-aggtrade-direct-fok-cooldown-stop/) — Binance `aggTrade` 穿越 open 后，直接发 Polymarket 受价格保护的 FOK/FAK 订单，成交后 2 秒冷却，再检查止损。

## v4 quick links

- [v4 README](./v4-source-relative-fastest-dryrun/README.md)
- [v4 trader](./v4-source-relative-fastest-dryrun/src/poly_v1_trader.py)
- [v4 tests](./v4-source-relative-fastest-dryrun/tests/test_v1_trader.py)
- [v4 deploy template](./v4-source-relative-fastest-dryrun/deploy/poly-v1-trader.env.example)

## Safety

v4 默认 `LIVE_TRADING=0`，只做 dry-run 日志，不真实下单。开启 live trading 前必须先检查 dry-run 日志、钱包/funder/signature_type、余额和 allowance。
