# v2 — Trade Lead Profit Maximizer

## One-line thesis

Use **trade-level CEX prints** (`@trade`, not slow ticker summaries) as the earliest public signal, then send **price-protected immediate Polymarket orders** before the CLOB fully reprices.

```text
CEX @trade prints reveal the real crossing first
→ infer near-final direction before Polymarket fully reprices
→ send protected FOK/FAK order immediately
→ maximize captured edge with adaptive entry price, cooldown, and smart exit
```

v2 intentionally ignores old assumptions and starts from one core idea:

> The edge is not prediction over minutes. The edge is micro-latency: CEX trade prints may lead Polymarket repricing.

---

## What v2 changes from v1

v1 was clean and conservative:

```text
Binance aggTrade crosses open
→ FOK/FAK BUY 5 shares @ 0.65
→ 2s cooldown
→ then evaluate stop
```

v2 upgrades the core engine:

1. Use **Binance `@trade`** as primary trigger, not `@aggTrade`.
2. Add **microburst confirmation** instead of a single cross.
3. Use **adaptive price ladder** instead of fixed 0.65 only.
4. Separate **entry capture** from **profit maximization**.
5. Use **post-fill edge scoring** to decide hold / exit / hedge.
6. Log every attempt as an execution experiment, not only as a trade.

---

## Data sources

### Primary fast source

```text
Binance symbol@trade
```

Raw trade stream emits individual trades with:

```json
{
  "e": "trade",
  "E": 0,
  "s": "BTCUSDT",
  "t": 0,
  "p": "100000.00",
  "q": "0.001",
  "T": 0,
  "m": true
}
```

Use:

- `p` = trade price
- `q` = trade size
- `T` = exchange trade timestamp
- local receive timestamp = network latency measurement

### Secondary confirmation sources

Use latest in-memory values only; never wait for them in the hot path unless the signal is weak.

```text
OKX trades/ticker latest
Bybit trades/ticker latest
Polymarket WSS book cache for diagnostics and optional gate
```

---

## Core strategy flow

```text
1. Maintain current 15m round open price.
2. Subscribe to Binance @trade for selected coins.
3. In final window, process every trade print.
4. Detect open-cross microburst.
5. Compute signal score within <1ms local CPU time.
6. If score passes threshold, send protected Polymarket immediate order.
7. If filled, enter cooldown + position management.
8. After cooldown, decide hold / stop / take-profit / hedge.
```

---

## Signal: trade-cross microburst

A single trade crossing open is too noisy. v2 uses a tiny rolling window, e.g. 100ms to 300ms.

### Rolling fields per coin

```text
last_trade_price
last_trade_ts
side_vs_open: UP / DOWN / FLAT
trades_100ms
trades_300ms
volume_100ms
volume_300ms
signed_volume_100ms
signed_volume_300ms
cross_count_300ms
last_cross_ts
```

### Direction definition

```text
price > open + epsilon => UP
price < open - epsilon => DOWN
otherwise FLAT
```

`epsilon` prevents dust-level fake crosses.

Suggested first values:

```text
BTC epsilon: 0.5 to 1.0 USD
ETH epsilon: 0.03 to 0.10 USD
SOL epsilon: 0.005 to 0.02 USD
DOGE/XRP: use percentage epsilon
```

---

## Entry score

v2 should not simply ask: “did it cross?”

It should ask:

> Is this cross strong enough to justify an immediate FOK/FAK attempt?

Suggested score:

```text
score = direction_strength
      + microburst_volume_score
      + time_left_score
      + source_agreement_score
      - chop_penalty
      - stale_market_penalty
```

### Components

#### 1. Direction strength

```text
abs(last_trade_price - open) / open
```

Bigger cross = stronger.

#### 2. Microburst volume score

```text
same-side trade volume in last 100ms / recent baseline volume
```

Rationale: a real move usually has burst volume; a fake cross may be one tiny print.

#### 3. Time-left score

Most valuable window is late but not too late:

```text
final 10s to 3s: good
final 3s to 0.5s: high potential but dangerous
<0.5s: order latency risk high
```

#### 4. Source agreement score

Do not wait, but reward if other sources already agree:

```text
OKX latest same side: +small
Bybit latest same side: +small
Both disagree: penalty
```

#### 5. Chop penalty

If side flipped too many times in 300ms to 1s, reduce or block entry.

```text
if cross_count_300ms >= 3:
    block or require stronger volume
```

---

## Entry order design

v2 uses price-protected immediate orders. It does **not** REST-fetch orderbook in the hot path.

### Base order

```text
side: BUY target outcome
size: 5 shares minimum
order type: FOK preferred for clean tests
limit price: adaptive cap
```

### Adaptive cap ladder

Instead of always using `0.65`, v2 tries to maximize profit without destroying expectancy.

Suggested ladder by signal strength:

```text
Weak valid signal:    cap 0.60
Normal signal:        cap 0.65
Strong microburst:    cap 0.70
Very strong late move: cap 0.75
```

Important:

- This is not “pay anything.”
- Every cap is explicit and logged.
- Expected value must be evaluated per cap bucket.

### Why adaptive cap?

If Polymarket reprices fast, 0.65 may almost never fill. A strong signal at 0.70 may still be profitable if true win probability is high enough.

Expected value rough logic:

```text
EV per share = P(win) * 1.00 - entry_price
```

Examples:

```text
entry 0.65 requires P(win) > 65%
entry 0.70 requires P(win) > 70%
entry 0.75 requires P(win) > 75%
```

v2 should learn which cap bucket is actually profitable.

---

## Position management

### After fill: hard cooldown

```text
post_fill_cooldown = 2s
```

During cooldown:

- no reverse entry;
- no duplicate entry;
- no immediate stop;
- keep recording all CEX trades and Polymarket book changes.

Purpose: avoid getting chopped around the open line.

### After cooldown: three-way decision

After 2s, do not only ask “stop or hold.” Ask:

```text
1. Is the trade still aligned?       => hold
2. Is the signal invalidated?        => stop / reduce
3. Did price move strongly in favor? => take profit if bid is rich
```

---

## Stop logic

### Stop condition v2-basic

```text
UP position:
    Binance @trade latest price < open - epsilon_stop

DOWN position:
    Binance @trade latest price > open + epsilon_stop
```

Suggested:

```text
epsilon_stop >= epsilon_entry
```

This avoids stopping on tiny one-tick noise.

### Stop confirmation options

Choose one per experiment:

```text
A. Binance-only fastest stop
B. Binance + OKX agreement stop
C. two consecutive reverse @trade prints
D. reverse microburst volume threshold
```

v2 default recommendation:

```text
Use B or D for real money, A for latency research.
```

---

## Take-profit logic

This is the profit-maximization upgrade.

In Polymarket binary markets, if entry is cheap and target side quickly reprices to high bid, we can lock profit instead of holding to settlement.

After cooldown, if position is still aligned:

```text
if Polymarket bid for held token >= take_profit_bid:
    sell held shares using FAK/FOK
```

Suggested first thresholds:

```text
entry <= 0.65 → take profit bid >= 0.85
entry <= 0.70 → take profit bid >= 0.88
entry <= 0.75 → take profit bid >= 0.92
```

Why:

- final seconds can reverse violently;
- taking +20c/share may be better than risking settlement flip;
- actual data will tell whether hold-to-expiry or take-profit has higher EV.

---

## Order types

### Entry

Prefer:

```text
FOK BUY 5 shares @ cap
```

Reason:

- all-or-none;
- clean ledger;
- no weird partial minimum-size position.

Optional experiment:

```text
FAK BUY 5 shares @ cap
```

Only if partial fill accounting is implemented.

### Exit

Prefer for take-profit:

```text
FAK SELL held shares
```

because selling some shares at rich bid can de-risk.

For clean first version:

```text
FOK SELL full held shares
```

---

## Anti-overtrading controls

### Per coin-round max position

```text
max_one_active_position_per_coin_round = true
```

### Failed-attempt throttle

If entry FOK fails:

```text
same coin + same direction cooldown = 150ms to 300ms
```

But do not block opposite direction forever; final-window reversals may matter.

### Chop lockout

If too many crosses occur:

```text
if cross_count_1s >= 5:
    lock coin-round for 1s or require very high score
```

### End-of-round guard

Avoid initiating new entries too close to settlement unless latency is proven:

```text
no new entry if secs_left < 0.35s
```

Tune after measuring order roundtrip latency.

---

## Hot-path engineering requirements

The hot path must be extremely small.

### Do in hot path

```text
parse Binance trade JSON
update rolling window
compute side/score
if signal: submit prebuilt order intent
```

### Do not do in hot path

```text
REST fetch market
REST fetch book
heavy JSON logging sync write
complex strategy loops
dashboard rendering
large Python object copying
```

### Required architecture

```text
CEX WSS thread/task
    → lock-free or minimal-lock state update
    → signal queue

Order executor task
    → signs/submits immediate order
    → records response

Logger task
    → async/batched JSONL writes

Polymarket WSS task
    → book cache + diagnostics only
```

---

## Latency metrics

Every attempt must log:

```json
{
  "event": "v2_order_attempt",
  "coin": "btc",
  "direction": "UP",
  "source": "binance_trade",
  "trade_id": 0,
  "trade_price": 100000.0,
  "trade_size": 0.01,
  "exchange_trade_ts": 0.0,
  "local_receive_ts": 0.0,
  "signal_decision_ts": 0.0,
  "order_submit_ts": 0.0,
  "order_response_ts": 0.0,
  "exchange_to_receive_ms": 0,
  "receive_to_decision_ms": 0,
  "decision_to_submit_ms": 0,
  "submit_to_response_ms": 0,
  "total_trade_to_response_ms": 0,
  "score": 0.0,
  "cap": 0.65,
  "order_type": "FOK",
  "requested_shares": 5,
  "result": "filled|cancelled|rejected|error",
  "filled_shares": 0,
  "avg_fill_price": null
}
```

Without this, v2 cannot know whether it is losing because of market speed or bot speed.

---

## Evaluation framework

Do not judge v2 only by total PnL at first. First separate the funnel:

```text
CEX trade crosses
→ valid microburst signals
→ order attempts
→ accepted by API
→ filled
→ profitable after fees/spread/slippage
→ best exit policy
```

Key metrics:

```text
signals/hour
attempts/hour
fill rate by cap bucket
win rate by cap bucket
EV per filled share
median trade-to-submit latency
median submit-to-response latency
fill rate vs latency bucket
PnL hold-to-expiry vs take-profit vs stop
```

---

## Initial v2 parameters

```text
primary_source: Binance @trade
secondary_sources: OKX latest, Bybit latest
entry_window: final 10s
no_entry_after: 0.35s left
entry_epsilon: asset-specific small threshold
score_threshold: 1.0 baseline
entry_caps: [0.60, 0.65, 0.70, 0.75]
entry_size: 5 shares
entry_order_type: FOK
post_fill_cooldown: 2s
failed_attempt_cooldown: 200ms
chop_window: 1s
chop_cross_limit: 5
take_profit_enabled: true
stop_enabled: true
max_active_position_per_coin_round: 1
```

---

## v2 expected advantage

v1 asks:

```text
Can 0.65 fills exist after aggTrade cross?
```

v2 asks a more profitable question:

```text
At what exact signal strength and price cap does direct immediate execution produce positive EV?
```

This allows the strategy to learn whether the true optimal action is:

```text
no trade
0.60 only
0.65 normal
0.70 strong
0.75 very strong
hold to settlement
take profit quickly
stop after confirmation
```

---

## Safety notes

- No missing response may be counted as a fill.
- No local orderbook simulation may be counted as real live execution.
- Every cap bucket must be evaluated separately.
- If using FAK, partial fill accounting is mandatory.
- If live trading, start with tiny size and hard daily loss limits.
- If no actual order/fill exists, dashboard must show empty/cancelled state, not inferred progress.
