#!/usr/bin/env python3
"""Polymarket v1 live/dry-run trader.

V1 aggressive source-relative strategy:
- Lock each exchange/source's own 15m round-open price by timestamp.
- In final 10s, fastest quote source crossing its own source_open triggers entry.
- Sources: Binance @trade, Coinbase market_trades, Bybit orderbook.1 mid, OKX books5 mid.
- Immediately submit protected Polymarket BUY 5 shares @ 0.65, OrderType FAK/FOK.

Safety:
- LIVE_TRADING defaults to false. Dry-run logs intended orders but does not submit.
- One active position per coin+round.
- Failed attempt cooldown prevents WSS tick spam.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import websockets  # type: ignore
except Exception:  # pragma: no cover
    websockets = None

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception:  # pragma: no cover
    ClobClient = None
    OrderArgs = None
    OrderType = None
    PartialCreateOrderOptions = None
    BalanceAllowanceParams = None
    AssetType = None
    BUY = "BUY"
    SELL = "SELL"

HOST = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
COINS = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
COIN_MAP = {
    "btc": {"binance": "btcusdt", "coinbase": "BTC-USD", "bybit": "BTCUSDT", "okx": "BTC-USDT"},
    "eth": {"binance": "ethusdt", "coinbase": "ETH-USD", "bybit": "ETHUSDT", "okx": "ETH-USDT"},
    "sol": {"binance": "solusdt", "coinbase": "SOL-USD", "bybit": "SOLUSDT", "okx": "SOL-USDT"},
    "xrp": {"binance": "xrpusdt", "coinbase": "XRP-USD", "bybit": "XRPUSDT", "okx": "XRP-USDT"},
    "doge": {"binance": "dogeusdt", "coinbase": "DOGE-USD", "bybit": "DOGEUSDT", "okx": "DOGE-USDT"},
    "bnb": {"binance": "bnbusdt", "coinbase": None, "bybit": "BNBUSDT", "okx": "BNB-USDT"},
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def round_window(ts: Optional[float] = None) -> tuple[int, int, float]:
    now = time.time() if ts is None else float(ts)
    start = int(now // 900) * 900
    end = start + 900
    return start, end, end - now


def side_vs_open(price: float, open_price: float, epsilon_pct: float = 0.0) -> str:
    if not open_price:
        return "FLAT"
    eps = abs(open_price) * epsilon_pct
    if price > open_price + eps:
        return "UP"
    if price < open_price - eps:
        return "DOWN"
    return "FLAT"


def mid_from_levels(bids: list[Any], asks: list[Any]) -> Optional[float]:
    if not bids or not asks:
        return None
    try:
        bid = float(bids[0][0])
        ask = float(asks[0][0])
    except Exception:
        return None
    if bid <= 0 or ask <= 0 or not math.isfinite(bid) or not math.isfinite(ask):
        return None
    return (bid + ask) / 2.0


@dataclass
class StrategyConfig:
    live_trading: bool = False
    entry_window_sec: float = 10.0
    no_entry_after_sec: float = 0.25
    entry_price_cap: float = 0.65
    entry_shares: float = 5.0
    entry_order_type: str = "FAK"
    post_fill_cooldown_sec: float = 2.0
    failed_attempt_cooldown_sec: float = 0.2
    max_one_position_per_coin_round: bool = True
    stop_sell_floor: float = 0.01
    stop_order_type: str = "FAK"
    epsilon_pct: float = 0.0
    dry_run_fill_mode: str = "no_fill"  # no_fill | pretend_full
    source_open_max_delay_sec: float = 5.0
    log_path: Path = Path("logs/v1_live_trader_events.jsonl")


@dataclass
class Position:
    coin: str
    round_start: int
    direction: str
    token_id: str
    shares: float
    avg_price: float
    opened_at: float
    cooldown_until: float
    entry_source: str
    entry_order_result: dict[str, Any]
    closed: bool = False


@dataclass
class CoinState:
    open_price: Optional[float] = None  # audit/fallback; source-relative trigger uses source_opens
    current_round: Optional[int] = None
    prices: dict[str, float] = field(default_factory=dict)
    sides: dict[str, str] = field(default_factory=dict)
    source_opens: dict[str, float] = field(default_factory=dict)
    open_delays_ms: dict[str, float] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)  # UP/DOWN -> token_id
    active_position: Optional[Position] = None
    last_attempt_ts: dict[str, float] = field(default_factory=dict)
    traded_rounds: set[int] = field(default_factory=set)


class JsonLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, obj: dict[str, Any]):
        row = {"ts": iso_now(), **obj}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


class PolymarketExecutor:
    def __init__(self, cfg: StrategyConfig, logger: JsonLogger):
        self.cfg = cfg
        self.logger = logger
        self.client = None
        if cfg.live_trading:
            if ClobClient is None:
                raise RuntimeError("py-clob-client is not installed")
            private_key = os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
            funder = os.getenv("POLY_FUNDER_ADDRESS") or os.getenv("FUNDER_ADDRESS")
            signature_type = int(os.getenv("POLY_SIGNATURE_TYPE") or os.getenv("SIGNATURE_TYPE") or "1")
            if not private_key or not funder:
                raise RuntimeError("LIVE_TRADING=1 requires POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS")
            self.client = ClobClient(HOST, key=private_key, chain_id=137, signature_type=signature_type, funder=funder)
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self._check_collateral_ready()

    def _check_collateral_ready(self):
        if not self.client or BalanceAllowanceParams is None or AssetType is None:
            return
        try:
            bal = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            self.logger.write({"event": "balance_allowance_check", "collateral": bal})
        except Exception as e:
            self.logger.write({"event": "balance_allowance_check_error", "error": str(e)})
            raise

    def _order_options(self):
        if PartialCreateOrderOptions is None:
            return None
        return PartialCreateOrderOptions(tick_size=os.getenv("POLY_TICK_SIZE", "0.01"), neg_risk=env_bool("POLY_NEG_RISK", False))

    def _order_type(self, text: str):
        if OrderType is None:
            return text
        return getattr(OrderType, text)

    def _parse_post_resp(self, resp: Any) -> dict[str, Any]:
        if isinstance(resp, dict):
            return resp
        return {"raw": str(resp)}

    def _filled_from_resp(self, resp: dict[str, Any], requested: float, price: float) -> tuple[float, float]:
        for key in ("filled_size", "filledSize", "size_matched", "matched_size", "filled"):
            if key in resp:
                try:
                    shares = float(resp[key])
                    return shares, float(resp.get("avg_price") or resp.get("avgPrice") or price)
                except Exception:
                    pass
        status = str(resp.get("status") or resp.get("result") or "").lower()
        if "fill" in status and not any(x in status for x in ("unfilled", "no_fill")):
            return requested, float(resp.get("avg_price") or resp.get("avgPrice") or price)
        return 0.0, 0.0

    async def buy(self, *, token_id: str, price: float, shares: float, meta: dict[str, Any]) -> dict[str, Any]:
        submit_ts = time.time()
        if not self.cfg.live_trading:
            filled = shares if self.cfg.dry_run_fill_mode == "pretend_full" else 0.0
            resp = {"dry_run": True, "would_submit": "BUY", "order_type": self.cfg.entry_order_type, "token_id": token_id, "price": price, "shares": shares, "filled_shares": filled, "avg_fill_price": price if filled else None}
            self.logger.write({"event": "dry_run_buy", "submit_ts": submit_ts, **meta, **resp})
            return resp
        order = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed = self.client.create_order(order, self._order_options())
        raw = self.client.post_order(signed, self._order_type(self.cfg.entry_order_type))
        resp = self._parse_post_resp(raw)
        filled, avg = self._filled_from_resp(resp, shares, price)
        out = {"raw": resp, "filled_shares": filled, "avg_fill_price": avg or None, "submit_ts": submit_ts, "response_ts": time.time()}
        self.logger.write({"event": "live_buy_response", **meta, **out})
        return out

    async def sell(self, *, token_id: str, price: float, shares: float, meta: dict[str, Any]) -> dict[str, Any]:
        submit_ts = time.time()
        if not self.cfg.live_trading:
            resp = {"dry_run": True, "would_submit": "SELL", "order_type": self.cfg.stop_order_type, "token_id": token_id, "price": price, "shares": shares, "filled_shares": 0.0}
            self.logger.write({"event": "dry_run_sell", "submit_ts": submit_ts, **meta, **resp})
            return resp
        order = OrderArgs(token_id=token_id, price=price, size=shares, side=SELL)
        signed = self.client.create_order(order, self._order_options())
        raw = self.client.post_order(signed, self._order_type(self.cfg.stop_order_type))
        resp = self._parse_post_resp(raw)
        filled, avg = self._filled_from_resp(resp, shares, price)
        out = {"raw": resp, "filled_shares": filled, "avg_fill_price": avg or None, "submit_ts": submit_ts, "response_ts": time.time()}
        self.logger.write({"event": "live_sell_response", **meta, **out})
        return out


class V1Trader:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.logger = JsonLogger(cfg.log_path)
        self.executor = PolymarketExecutor(cfg, self.logger)
        self.state: dict[str, CoinState] = {c: CoinState() for c in COINS}
        self.running = True

    def fetch_tokens(self, coin: str, round_start: int) -> dict[str, str]:
        slug = f"{coin}-updown-15m-{round_start}"
        url = f"{GAMMA}/markets?" + urllib.parse.urlencode({"slug": slug})
        req = urllib.request.Request(url, headers={"User-Agent": "poly-v1-live-trader", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            arr = json.loads(resp.read().decode("utf-8"))
        if not arr:
            raise RuntimeError(f"no market for {slug}")
        toks = arr[0].get("clobTokenIds") or "[]"
        if isinstance(toks, str):
            toks = json.loads(toks)
        return {"UP": str(toks[0]), "DOWN": str(toks[1])}

    def reset_round_if_needed(self, coin: str, now_ts: float):
        start, _, _ = round_window(now_ts)
        st = self.state[coin]
        if st.current_round != start:
            st.current_round = start
            st.open_price = None
            st.prices.clear()
            st.sides.clear()
            st.source_opens.clear()
            st.open_delays_ms.clear()
            st.tokens.clear()
            st.active_position = None
            st.last_attempt_ts.clear()
            self.logger.write({"event": "round_reset", "coin": coin, "round_start": start})

    def update_open(self, coin: str, price: float):
        st = self.state[coin]
        if st.open_price is None:
            st.open_price = price
            try:
                st.tokens = self.fetch_tokens(coin, st.current_round)
            except Exception as e:
                self.logger.write({"event": "token_fetch_error", "coin": coin, "round_start": st.current_round, "error": str(e)})
            self.logger.write({"event": "open_set", "coin": coin, "round_start": st.current_round, "open": price, "tokens_ready": bool(st.tokens), "open_source": "first_source_tick_audit"})

    def lock_source_open_if_needed(self, coin: str, source: str, price: float, now_ts: float) -> bool:
        st = self.state[coin]
        if source in st.source_opens:
            return False
        round_start = st.current_round if st.current_round is not None else round_window(now_ts)[0]
        delay_ms = max(0.0, (now_ts - round_start) * 1000.0)
        st.source_opens[source] = price
        st.open_delays_ms[source] = delay_ms
        self.logger.write({"event": "source_open_locked", "coin": coin, "source": source, "round_start": round_start, "source_open": price, "open_delay_ms": delay_ms})
        return True

    def should_attempt_entry(self, coin: str, source: str, direction: str, now_ts: float) -> tuple[bool, str]:
        st = self.state[coin]
        if direction not in ("UP", "DOWN"):
            return False, "flat_direction"
        if not st.tokens.get(direction):
            return False, "no_token"
        if source not in st.source_opens:
            return False, "no_source_open"
        if st.open_delays_ms.get(source, 0.0) > self.cfg.source_open_max_delay_sec * 1000:
            return False, "source_open_too_late"
        _, _, secs_left = round_window(now_ts)
        if secs_left > self.cfg.entry_window_sec:
            return False, "outside_final_window"
        if secs_left < self.cfg.no_entry_after_sec:
            return False, "too_late"
        if st.active_position and not st.active_position.closed:
            return False, "position_open"
        if self.cfg.max_one_position_per_coin_round and st.current_round in st.traded_rounds:
            return False, "already_traded_round"
        last = st.last_attempt_ts.get(direction, 0.0)
        if now_ts - last < self.cfg.failed_attempt_cooldown_sec:
            return False, "attempt_cooldown"
        return True, "ok"

    async def on_cex_price(self, coin: str, source: str, price: float, exchange_ts: float):
        if price <= 0 or not math.isfinite(price):
            return
        now_ts = time.time()
        self.reset_round_if_needed(coin, now_ts)
        self.update_open(coin, price)
        locked = self.lock_source_open_if_needed(coin, source, price, now_ts)
        st = self.state[coin]
        source_open = st.source_opens[source]
        prev_side = st.sides.get(source)
        cur_side = side_vs_open(price, source_open, self.cfg.epsilon_pct)
        st.prices[source] = price
        st.sides[source] = cur_side
        self.logger.write({"event": "source_tick", "coin": coin, "source": source, "price": price, "source_open": source_open, "side": cur_side, "round_start": st.current_round, "exchange_ts": exchange_ts, "receive_ts": now_ts})
        if not locked and prev_side and cur_side != prev_side and cur_side in ("UP", "DOWN") and prev_side != "FLAT":
            await self.handle_cross(coin, source, cur_side, price, exchange_ts, now_ts, prev_side)
        await self.check_stop(coin, source, price, cur_side, now_ts)

    async def handle_cross(self, coin: str, source: str, direction: str, price: float, exchange_ts: float, now_ts: float, prev_side: str):
        ok, reason = self.should_attempt_entry(coin, source, direction, now_ts)
        st = self.state[coin]
        source_open = st.source_opens.get(source)
        event = {"coin": coin, "source": source, "direction": direction, "prev_side": prev_side, "price": price, "open": st.open_price, "source_open": source_open, "source_delta": None if source_open is None else price - source_open, "round_start": st.current_round, "exchange_ts": exchange_ts, "receive_ts": now_ts, "secs_left": round_window(now_ts)[2], "reason": reason}
        self.logger.write({"event": "cross", **event})
        if not ok:
            return
        st.last_attempt_ts[direction] = now_ts
        token = st.tokens[direction]
        resp = await self.executor.buy(token_id=token, price=self.cfg.entry_price_cap, shares=self.cfg.entry_shares, meta=event)
        filled = float(resp.get("filled_shares") or 0.0)
        if filled > 0:
            pos = Position(coin=coin, round_start=st.current_round, direction=direction, token_id=token, shares=filled, avg_price=float(resp.get("avg_fill_price") or self.cfg.entry_price_cap), opened_at=time.time(), cooldown_until=time.time() + self.cfg.post_fill_cooldown_sec, entry_source=source, entry_order_result=resp)
            st.active_position = pos
            st.traded_rounds.add(st.current_round)
            self.logger.write({"event": "position_open", **asdict(pos)})
        else:
            self.logger.write({"event": "entry_no_fill", **event, "response": resp})

    async def check_stop(self, coin: str, source: str, price: float, cur_side: str, now_ts: float):
        st = self.state[coin]
        pos = st.active_position
        if not pos or pos.closed:
            return
        if now_ts < pos.cooldown_until:
            return
        if cur_side == "FLAT" or cur_side == pos.direction:
            return
        meta = {"coin": coin, "source": source, "round_start": st.current_round, "position_direction": pos.direction, "reverse_side": cur_side, "price": price, "source_open": st.source_opens.get(source), "open": st.open_price, "shares": pos.shares}
        resp = await self.executor.sell(token_id=pos.token_id, price=self.cfg.stop_sell_floor, shares=pos.shares, meta=meta)
        sold = float(resp.get("filled_shares") or 0.0)
        if sold > 0 or self.cfg.live_trading:
            pos.closed = True
            self.logger.write({"event": "position_stop_attempt", "sold_shares": sold, "response": resp, **meta})

    async def binance_loop(self):
        streams = "/".join([COIN_MAP[c]["binance"] + "@trade" for c in COINS if COIN_MAP[c].get("binance")])
        url = "wss://stream.binance.com:9443/stream?streams=" + streams
        while self.running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self.logger.write({"event": "wss_connected", "source": "binance_trade"})
                    async for msg in ws:
                        d = json.loads(msg).get("data", {})
                        sym = d.get("s", "").lower()
                        coin = next((c for c in COINS if COIN_MAP[c].get("binance") == sym), None)
                        if coin:
                            await self.on_cex_price(coin, "binance_trade", float(d.get("p", 0)), (d.get("T") or d.get("E") or 0) / 1000)
            except Exception as e:
                self.logger.write({"event": "wss_error", "source": "binance_trade", "error": str(e)})
                await asyncio.sleep(2)

    async def coinbase_loop(self):
        product_ids = [COIN_MAP[c]["coinbase"] for c in COINS if COIN_MAP[c].get("coinbase")]
        while self.running:
            try:
                async with websockets.connect("wss://advanced-trade-ws.coinbase.com", ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": product_ids, "channel": "market_trades"}))
                    self.logger.write({"event": "wss_connected", "source": "coinbase_market_trades"})
                    async for msg in ws:
                        j = json.loads(msg)
                        for ev in j.get("events", []) or []:
                            for tr in ev.get("trades", []) or []:
                                pid = tr.get("product_id")
                                coin = next((c for c in COINS if COIN_MAP[c].get("coinbase") == pid), None)
                                if coin:
                                    await self.on_cex_price(coin, "coinbase_market_trades", float(tr.get("price", 0)), time.time())
            except Exception as e:
                self.logger.write({"event": "wss_error", "source": "coinbase_market_trades", "error": str(e)})
                await asyncio.sleep(2)

    async def bybit_loop(self):
        args = ["orderbook.1." + COIN_MAP[c]["bybit"] for c in COINS if COIN_MAP[c].get("bybit")]
        while self.running:
            try:
                async with websockets.connect("wss://stream.bybit.com/v5/public/spot", ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    self.logger.write({"event": "wss_connected", "source": "bybit_orderbook1_mid"})
                    async for msg in ws:
                        j = json.loads(msg)
                        topic = j.get("topic", "")
                        sym = topic.split(".")[-1]
                        coin = next((c for c in COINS if COIN_MAP[c].get("bybit") == sym), None)
                        data = j.get("data") or {}
                        mid = mid_from_levels(data.get("b") or [], data.get("a") or [])
                        if coin and mid:
                            await self.on_cex_price(coin, "bybit_orderbook1_mid", mid, float(j.get("ts") or data.get("ts") or 0) / 1000)
            except Exception as e:
                self.logger.write({"event": "wss_error", "source": "bybit_orderbook1_mid", "error": str(e)})
                await asyncio.sleep(2)

    async def okx_loop(self):
        args = [{"channel": "books5", "instId": COIN_MAP[c]["okx"]} for c in COINS if COIN_MAP[c].get("okx")]
        while self.running:
            try:
                async with websockets.connect("wss://ws.okx.com:8443/ws/v5/public", ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    self.logger.write({"event": "wss_connected", "source": "okx_books5_mid"})
                    async for msg in ws:
                        j = json.loads(msg)
                        arg = j.get("arg") or {}
                        inst = arg.get("instId")
                        coin = next((c for c in COINS if COIN_MAP[c].get("okx") == inst), None)
                        for item in j.get("data", []) or []:
                            mid = mid_from_levels(item.get("bids") or [], item.get("asks") or [])
                            if coin and mid:
                                await self.on_cex_price(coin, "okx_books5_mid", mid, float(item.get("ts", 0)) / 1000)
            except Exception as e:
                self.logger.write({"event": "wss_error", "source": "okx_books5_mid", "error": str(e)})
                await asyncio.sleep(2)

    async def run(self):
        if websockets is None:
            raise RuntimeError("websockets package not installed")
        self.logger.write({"event": "service_started", "live_trading": self.cfg.live_trading, "config": asdict(self.cfg) | {"log_path": str(self.cfg.log_path)}})
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: setattr(self, "running", False))
            except NotImplementedError:
                pass
        tasks = [asyncio.create_task(self.binance_loop()), asyncio.create_task(self.coinbase_loop()), asyncio.create_task(self.bybit_loop()), asyncio.create_task(self.okx_loop())]
        while self.running:
            await asyncio.sleep(0.2)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def load_config() -> StrategyConfig:
    return StrategyConfig(
        live_trading=env_bool("LIVE_TRADING", False),
        entry_window_sec=float(os.getenv("ENTRY_WINDOW_SEC", "10")),
        no_entry_after_sec=float(os.getenv("NO_ENTRY_AFTER_SEC", "0.25")),
        entry_price_cap=float(os.getenv("ENTRY_PRICE_CAP", "0.65")),
        entry_shares=float(os.getenv("ENTRY_SHARES", "5")),
        entry_order_type=os.getenv("ENTRY_ORDER_TYPE", "FAK"),
        post_fill_cooldown_sec=float(os.getenv("POST_FILL_COOLDOWN_SEC", "2")),
        failed_attempt_cooldown_sec=float(os.getenv("FAILED_ATTEMPT_COOLDOWN_SEC", "0.2")),
        stop_sell_floor=float(os.getenv("STOP_SELL_FLOOR", "0.01")),
        stop_order_type=os.getenv("STOP_ORDER_TYPE", "FAK"),
        epsilon_pct=float(os.getenv("ENTRY_EPSILON_PCT", "0")),
        dry_run_fill_mode=os.getenv("DRY_RUN_FILL_MODE", "no_fill"),
        source_open_max_delay_sec=float(os.getenv("SOURCE_OPEN_MAX_DELAY_SEC", "5")),
        log_path=Path(os.getenv("V1_LOG_PATH", "/root/projects/20260503-poly/logs/v1_live_trader_events.jsonl")),
    )


def main():
    cfg = load_config()
    trader = V1Trader(cfg)
    asyncio.run(trader.run())


if __name__ == "__main__":
    main()
