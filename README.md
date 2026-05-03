# 20260503-poly

Polymarket 15m UP/DOWN strategy research notes.

This repository records strategy versions, assumptions, execution design, and future implementation notes for latency-sensitive Polymarket trading experiments.

## Versions

- [`v2-trade-lead-profit-maximizer`](./v2-trade-lead-profit-maximizer/) — Fresh design: Binance `@trade` microburst lead signal with adaptive protected FOK/FAK entries, 2s cooldown, stop/take-profit management, and latency-funnel logging.
- [`v1-binance-aggtrade-direct-fok-cooldown-stop`](./v1-binance-aggtrade-direct-fok-cooldown-stop/) — Binance aggTrade open-cross direct FOK/FAK entry with 2s cooldown and post-cooldown stop-loss check.

## Core principle

Do not fabricate fills. For paper/research logs, separate:

1. signal timing,
2. Polymarket orderbook state,
3. direct order attempt result,
4. fill/reject/cancel response,
5. post-entry stop/settlement result.
