# v1 — Binance aggTrade Direct FOK + 2s Cooldown Stop

## Strategy summary

```text
Binance aggTrade 穿越 open
→ 立即发 Polymarket FOK/FAK BUY 5 shares @ 0.65
→ 成交后进入 2s cooldown
→ cooldown 期间不因为来回穿越反复开仓/反手
→ 2s 后重新评估是否触发止损/退出
```

This version is designed to test whether Binance trade-level price movement leads Polymarket CLOB repricing by a few milliseconds to seconds.

The execution idea is intentionally closer to live trading than a conservative paper-sweep model:

- do **not** wait for REST orderbook fetch before sending the entry order;
- use Binance `@aggTrade` as the fast trigger;
- send a protected Polymarket immediate order with price cap;
- let Polymarket matching decide whether the order fills or cancels;
- keep full logs for diagnosis.

## Entry trigger

For each coin and 15-minute round:

1. Track round `open` price.
2. Subscribe to Binance `symbol@aggTrade`.
3. Detect first meaningful cross of the open price:

```text
previous side: below open
current aggTrade price: above open
=> UP signal

previous side: above open
current aggTrade price: below open
=> DOWN signal
```

Recommended first implementation:

```text
entry_source = Binance aggTrade
entry_window = final 10s, later compare final 5s / 3s / 2s
max_one_position_per_coin_round = true
```

## Entry order

On signal:

```text
BUY target outcome
order type: FOK preferred, FAK optional experiment
size: 5 shares
limit price / max average price: 0.65
max theoretical cost: 5 * 0.65 = 3.25 USDC
```

Expected behavior:

- if enough asks are available at `<= 0.65`, order fills;
- if not enough liquidity, FOK cancels/fails immediately;
- if FAK is used, available liquidity may partially fill and the rest cancels;
- the order must not execute above the price cap.

## Why skip pre-order orderbook checking?

The hypothesis is latency-sensitive:

```text
Binance aggTrade cross
→ Polymarket book may still be stale/cheap for a tiny window
→ direct protected order may capture the gap
```

A REST `/book` fetch or even local book simulation before order submission can add latency. Therefore v1 should test direct protected order attempts.

However, diagnostic WSS orderbook logging should still run in parallel to explain fills and misses.

## Cooldown rule

After a confirmed fill:

```text
cooldown_until = fill_response_time + 2 seconds
```

During cooldown:

- no new entry;
- no reverse entry;
- no stop-loss exit;
- only record price/orderbook movement.

Reason: Binance aggTrade can oscillate around the round open and create repeated false crosses. Cooldown prevents immediate overtrading and whipsaw exits.

## Stop-loss / exit check

After cooldown expires:

### Simple first version

```text
If position = UP and Binance latest aggTrade price < open:
    attempt stop-loss sell

If position = DOWN and Binance latest aggTrade price > open:
    attempt stop-loss sell
```

### More conservative variant

Require Binance and OKX latest prices to agree:

```text
UP position stop:
    Binance latest < open AND OKX latest < open

DOWN position stop:
    Binance latest > open AND OKX latest > open
```

## Exit order

Initial clean version:

```text
SELL held shares
order type: FOK or FAK
shares: actual filled shares
```

Notes:

- FOK keeps accounting simple: either close fully or keep holding.
- FAK can reduce risk but requires partial-position accounting.

## State machine

```text
FLAT
  ↓ Binance aggTrade open-cross
ORDER_SENT
  ↓ fill
POSITION_OPEN + cooldown_until = now + 2s
  ↓ cooldown expired
WATCH_STOP
  ↓ reverse-side condition
EXIT_SENT
  ↓ closed or failed
CLOSED / HOLD_TO_SETTLEMENT
```

If entry order does not fill:

```text
ORDER_SENT
  ↓ cancelled / rejected / no fill
FLAT
```

Recommended failed-attempt cooldown:

```text
100ms - 300ms per coin/round/direction
```

to avoid spamming FOK attempts on every aggTrade tick.

## Required logs

Every signal/order attempt should record:

```json
{
  "event": "direct_order_attempt",
  "coin": "btc",
  "round_start": 0,
  "source": "binance_aggTrade",
  "direction": "UP",
  "order_type": "FOK",
  "shares": 5,
  "limit_price": 0.65,
  "signal_exchange_ts": 0.0,
  "signal_receive_ts": 0.0,
  "submit_ts": 0.0,
  "response_ts": 0.0,
  "exchange_latency_ms": 0,
  "submit_latency_ms": 0,
  "roundtrip_ms": 0,
  "result": "filled|cancelled|rejected|error",
  "avg_fill_price": null,
  "filled_shares": 0,
  "cooldown_until": null
}
```

Also record parallel diagnostics:

- Binance aggTrade price/timestamp;
- OKX latest price/timestamp;
- Polymarket UP/DOWN best bid/ask from WSS cache;
- Polymarket book `updated` timestamp and age;
- fill/reject reason;
- stop-loss check result;
- settlement result.

## Research questions

v1 should answer:

1. Does Binance `aggTrade` cross arrive early enough to beat Polymarket repricing?
2. How often does protected `BUY 5 @ 0.65` actually fill?
3. When it fails, is failure due to price already moving, insufficient depth, API latency, signing latency, or rate limit?
4. Does 2s cooldown reduce overtrading without missing necessary exits?
5. Is Binance-only faster but noisier than Binance+OKX confirmation?

## Initial parameters

```text
entry_source: Binance aggTrade
entry_order_type: FOK
entry_size: 5 shares
entry_limit_price: 0.65
entry_window: final 10s
post_fill_cooldown: 2s
failed_attempt_cooldown: 200ms
stop_check_after: 2s
stop_trigger_v1: Binance latest reverse-crosses open
max_positions: one per coin per round
```

## Safety / correctness notes

- Never treat a missing order response as a fill.
- Never infer a fill only from local orderbook simulation.
- Do not use Gamma `outcomePrices` as execution price.
- Keep live order results separate from paper/theoretical fills.
- If no actual order/fill exists, show empty state or cancelled attempt directly.
