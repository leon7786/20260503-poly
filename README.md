# 20260503-poly v4 — Polymarket 15m Up/Down Dry-Run Trader

> **Status:** v4 dry-run first. Live trading is intentionally disabled by default.
>
> Repository: <https://github.com/leon7786/20260503-poly>

## 1. What v4 does

v4 is a Polymarket 15-minute crypto **Up/Down** strategy runner focused on the final seconds of each round.

The core idea is:

1. Find the current Polymarket 15m market tokens by slug, e.g. `btc-updown-15m-<round_start>`.
2. For each quote source, lock that source's own price at the round timestamp as `source_open`.
3. During the final 10 seconds, monitor several fast exchange feeds at the same time.
4. If any source crosses its own `source_open`, the fastest valid cross wins.
5. Immediately submit a protected Polymarket order in dry-run mode:
   - side: `UP` or `DOWN`
   - size: 5 shares
   - max price: 0.65
   - order type: `FAK` by default

This version is designed to test the production wiring safely before live trading.

## 2. Why source-relative open is used

Polymarket BTC/USD rounds resolve from Chainlink BTC/USD, not directly from Binance, Coinbase, OKX, or Bybit.

A naive trigger like this is unsafe:

```text
Binance current price > Polymarket / Chainlink open
```

Different venues naturally have basis differences. Instead, v4 uses timestamp-aligned source opens:

```text
Binance current price  vs Binance round open
Coinbase current price vs Coinbase round open
Bybit mid price        vs Bybit round open
OKX mid price          vs OKX round open
```

Mathematically, this is equivalent to mapping each exchange's open to the Polymarket round open by timestamp. It avoids false signals caused by venue basis.

## 3. Quote sources

v4 currently monitors these feeds concurrently:

| Source name | Exchange feed | Price used | Purpose |
|---|---|---:|---|
| `binance_trade` | Binance Spot `@trade` | last trade price | fast trade prints |
| `coinbase_market_trades` | Coinbase Advanced Trade `market_trades` | last trade price | high-frequency USD trade prints |
| `bybit_orderbook1_mid` | Bybit Spot `orderbook.1` | `(best_bid + best_ask) / 2` | app-like fast level-1 quote |
| `okx_books5_mid` | OKX Spot `books5` | `(best_bid + best_ask) / 2` | app-like fast book quote |

The strategy does **not** wait for consensus. The first valid final-window cross triggers the dry-run order attempt.

## 4. Files

```text
src/poly_v1_trader.py              Main dry-run/live-capable service
tests/test_v1_trader.py            Unit tests for source-relative opens and execution rules
scripts/poly_v1_health_check.py    Import/config/public API health check
deploy/poly-v1-trader.env.example  Safe environment template, LIVE_TRADING=0
deploy/poly-v1-trader.service      systemd service template
requirements.txt                   Python dependencies
```

## 5. Safety model

### Live trading is disabled by default

The default env file contains:

```text
LIVE_TRADING=0
```

With `LIVE_TRADING=0`, the service logs `dry_run_buy` / `dry_run_sell` events and does not submit real orders.

### Explicit live confirmation required

Do not set `LIVE_TRADING=1` until the dry-run logs have been reviewed and wallet/funder/signature settings are confirmed.

Required for live mode:

```text
POLY_PRIVATE_KEY=0x...
POLY_FUNDER_ADDRESS=0x...
POLY_SIGNATURE_TYPE=1
LIVE_TRADING=1
```

### Source-open freshness guard

The strategy records when each source open was locked:

```text
SOURCE_OPEN_MAX_DELAY_SEC=5
```

If a source's round open is locked too late, entry is skipped with:

```text
source_open_too_late
```

This prevents a service restart mid-round from creating a wrong open anchor.

## 6. Configuration

Copy the example environment file:

```bash
sudo cp deploy/poly-v1-trader.env.example /etc/poly-v1-trader.env
sudo chmod 600 /etc/poly-v1-trader.env
```

Important settings:

```text
LIVE_TRADING=0
ENTRY_WINDOW_SEC=10
NO_ENTRY_AFTER_SEC=0.25
ENTRY_PRICE_CAP=0.65
ENTRY_SHARES=5
ENTRY_ORDER_TYPE=FAK
POST_FILL_COOLDOWN_SEC=2
FAILED_ATTEMPT_COOLDOWN_SEC=0.2
STOP_SELL_FLOOR=0.01
STOP_ORDER_TYPE=FAK
ENTRY_EPSILON_PCT=0
DRY_RUN_FILL_MODE=no_fill
SOURCE_OPEN_MAX_DELAY_SEC=5
V1_LOG_PATH=/root/projects/20260503-poly/logs/v1_live_trader_events.jsonl
POLY_TICK_SIZE=0.01
POLY_NEG_RISK=0
```

## 7. Install

```bash
cd /root/projects/20260503-poly
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 8. Test and smoke check

Run unit tests:

```bash
.venv/bin/pytest tests/test_v1_trader.py -q
```

Run syntax checks:

```bash
.venv/bin/python -m py_compile src/poly_v1_trader.py scripts/poly_v1_health_check.py
```

Run public API health check:

```bash
.venv/bin/python scripts/poly_v1_health_check.py
```

Expected health output includes:

```text
clob_ok: OK
gamma_sample_len: 1
live_trading: false
```

## 9. Run dry-run manually

```bash
cd /root/projects/20260503-poly
LIVE_TRADING=0 timeout 30s .venv/bin/python src/poly_v1_trader.py
```

Then inspect logs:

```bash
tail -n 100 logs/v1_live_trader_events.jsonl
```

Useful event types:

| Event | Meaning |
|---|---|
| `service_started` | service booted and config logged |
| `wss_connected` | a quote WebSocket source connected |
| `round_reset` | new 15m round detected |
| `open_set` | audit/fallback open set from first source tick |
| `source_open_locked` | a source's timestamp-aligned round open locked |
| `source_tick` | quote update processed |
| `cross` | source crossed its own open |
| `dry_run_buy` | would submit Polymarket buy order |
| `entry_no_fill` | dry-run/live order did not fill |
| `dry_run_sell` | would submit stop/reverse sell |

Example `source_open_locked`:

```json
{
  "event": "source_open_locked",
  "coin": "btc",
  "source": "bybit_orderbook1_mid",
  "round_start": 1777834800,
  "source_open": 123456.7,
  "open_delay_ms": 42.0
}
```

Example final-window cross:

```json
{
  "event": "cross",
  "coin": "btc",
  "source": "coinbase_market_trades",
  "direction": "UP",
  "price": 123460.0,
  "source_open": 123456.7,
  "source_delta": 3.3,
  "secs_left": 7.8,
  "reason": "ok"
}
```

## 10. systemd dry-run deployment

Install the service file:

```bash
sudo cp deploy/poly-v1-trader.service /etc/systemd/system/poly-v1-trader.service
sudo systemctl daemon-reload
sudo systemctl enable --now poly-v1-trader.service
```

Check status:

```bash
systemctl status poly-v1-trader.service --no-pager
```

Inspect logs:

```bash
tail -f /root/projects/20260503-poly/logs/v1_live_trader_events.jsonl
```

Stop service:

```bash
sudo systemctl stop poly-v1-trader.service
```

## 11. Execution details

When live mode is enabled, the executor uses official `py-clob-client` with:

```python
OrderArgs(token_id=token_id, price=cap, size=shares, side=BUY)
client.create_order(order_args, PartialCreateOrderOptions(...))
client.post_order(signed, OrderType.FAK)
```

Important: v4 intentionally uses `OrderArgs`, not `MarketOrderArgs`, because `MarketOrderArgs` BUY uses dollar amount and can trigger extra REST orderbook calculation if price is not set.

## 12. Current tested status

At the time this v4 README was written, local verification passed:

```text
pytest: 7 passed
py_compile: OK
health_check: CLOB OK, Gamma OK
short dry-run: Binance, Coinbase, Bybit, OKX connected
```

A short dry-run produced source ticks and source-open locks across all connected sources.

## 13. Important limitations

- Dry-run fills default to `no_fill`; it tests signal and submit intent, not actual Polymarket fill quality.
- The audit `open_price` is still the first seen source tick. The live trigger uses `source_open`, not this audit open.
- Chainlink official open/close should still be logged later for post-trade evaluation and source-quality analysis.
- Live trading requires wallet, funder, signature type, allowance/balance checks, and explicit confirmation.
- Geographic and Polymarket trading restrictions may apply.

## 14. Version notes

v4 differs from earlier versions by adding:

- source-relative open locking
- multi-source fastest quote monitoring
- Coinbase `market_trades`
- Binance `@trade`
- Bybit `orderbook.1` mid
- OKX `books5` mid
- dry-run-safe deployment defaults
- tests for source-relative crossing behavior
