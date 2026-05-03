import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "poly_v1_trader.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("poly_v1_trader", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["poly_v1_trader"] = mod
    spec.loader.exec_module(mod)
    return mod


class DummyExecutor:
    def __init__(self, filled=0.0):
        self.buys = []
        self.sells = []
        self.filled = filled

    async def buy(self, *, token_id, price, shares, meta):
        self.buys.append((token_id, price, shares, meta))
        return {"dry_run": True, "filled_shares": self.filled, "avg_fill_price": price}

    async def sell(self, *, token_id, price, shares, meta):
        self.sells.append((token_id, price, shares, meta))
        return {"dry_run": True, "filled_shares": shares, "avg_fill_price": price}


def make_trader(tmp_path, filled=0.0):
    m = load_mod()
    cfg = m.StrategyConfig(log_path=tmp_path / "events.jsonl", dry_run_fill_mode="no_fill")
    t = m.V1Trader(cfg)
    t.executor = DummyExecutor(filled=filled)
    return m, t


def setup_coin(m, t, coin="btc", now=1000.0):
    st = t.state[coin]
    start, _, _ = m.round_window(now)
    st.current_round = start
    st.open_price = 100.0
    st.source_opens = {"binance_trade": 100.0}
    st.open_delays_ms = {"binance_trade": 0.0}
    st.source_open_status = {"binance_trade": "locked"}
    st.tokens = {"UP": "up-token", "DOWN": "down-token"}
    return st


def test_final_window_cross_triggers_buy_fak_order_args(tmp_path, monkeypatch):
    m, t = make_trader(tmp_path, filled=0.0)
    setup_coin(m, t, now=2692.0)
    monkeypatch.setattr(m.time, "time", lambda: 2692.0)  # 8s left in 900s window
    asyncio.run(t.handle_cross("btc", "binance_trade", "UP", 101.0, 2692.0, 2692.0, "DOWN"))
    assert len(t.executor.buys) == 1
    token, price, shares, meta = t.executor.buys[0]
    assert token == "up-token"
    assert price == 0.65
    assert shares == 5.0
    assert meta["direction"] == "UP"


def test_outside_final_window_does_not_buy(tmp_path, monkeypatch):
    m, t = make_trader(tmp_path)
    setup_coin(m, t, now=1000.0)
    monkeypatch.setattr(m.time, "time", lambda: 1000.0)
    asyncio.run(t.handle_cross("btc", "binance_trade", "UP", 101.0, 1000.0, 1000.0, "DOWN"))
    assert t.executor.buys == []


def test_partial_fill_opens_position_and_cooldown_blocks_stop(tmp_path, monkeypatch):
    m, t = make_trader(tmp_path, filled=2.5)
    setup_coin(m, t, now=2692.0)
    monkeypatch.setattr(m.time, "time", lambda: 2692.0)
    asyncio.run(t.handle_cross("btc", "binance_trade", "UP", 101.0, 2692.0, 2692.0, "DOWN"))
    pos = t.state["btc"].active_position
    assert pos is not None
    assert pos.shares == 2.5
    assert pos.cooldown_until == 2694.0
    asyncio.run(t.check_stop("btc", "binance_trade", 99.0, "DOWN", 2693.0))
    assert t.executor.sells == []


def test_after_cooldown_reverse_side_sells_actual_filled_shares(tmp_path, monkeypatch):
    m, t = make_trader(tmp_path, filled=2.5)
    setup_coin(m, t, now=2692.0)
    monkeypatch.setattr(m.time, "time", lambda: 2692.0)
    asyncio.run(t.handle_cross("btc", "binance_trade", "UP", 101.0, 2692.0, 2692.0, "DOWN"))
    asyncio.run(t.check_stop("btc", "binance_trade", 99.0, "DOWN", 2694.1))
    assert len(t.executor.sells) == 1
    token, price, shares, meta = t.executor.sells[0]
    assert token == "up-token"
    assert price == 0.01
    assert shares == 2.5


def test_source_relative_open_cross_triggers_even_if_polymarket_open_differs(tmp_path, monkeypatch):
    m, t = make_trader(tmp_path, filled=0.0)
    st = setup_coin(m, t, now=2692.0)
    st.open_price = 1000.0  # audit-only/Polymarket open can differ materially
    st.source_opens = {"coinbase_market_trades": 995.0}
    st.open_delays_ms = {"coinbase_market_trades": 12.0}
    st.source_open_status = {"coinbase_market_trades": "locked"}
    st.sides = {"coinbase_market_trades": "DOWN"}
    monkeypatch.setattr(m.time, "time", lambda: 2692.0)
    asyncio.run(t.on_cex_price("btc", "coinbase_market_trades", 996.0, 2692.0))
    assert len(t.executor.buys) == 1
    assert t.executor.buys[0][3]["source_open"] == 995.0
    assert t.executor.buys[0][3]["open"] == 1000.0


def test_first_tick_in_mid_round_buffers_but_does_not_lock_source_open(tmp_path, monkeypatch):
    m, t = make_trader(tmp_path, filled=0.0)
    st = setup_coin(m, t, now=2692.0)
    st.source_opens = {}
    st.open_delays_ms = {}
    st.source_open_status = {}
    st.sides = {}
    monkeypatch.setattr(m.time, "time", lambda: 2692.0)
    asyncio.run(t.on_cex_price("btc", "bybit_orderbook1_mid", 123.4, 2692.0))
    assert "bybit_orderbook1_mid" not in st.source_opens
    assert st.source_open_status["bybit_orderbook1_mid"] == "unavailable"
    assert t.executor.buys == []


def test_select_open_tick_prefers_first_tick_after_round_start():
    m = load_mod()
    ticks = [
        m.SourceTick(price=99.0, exchange_ts=999.8, receive_ts=999.81),
        m.SourceTick(price=100.0, exchange_ts=1000.012, receive_ts=1000.02),
        m.SourceTick(price=101.0, exchange_ts=1000.2, receive_ts=1000.21),
    ]
    selected = m.select_open_tick(ticks, 1000.0, first_after_max_delay_ms=500, closest_max_abs_ms=250)
    assert selected is not None
    assert selected.price == 100.0
    assert selected.method == "first_tick_after"
    assert round(selected.delay_ms, 1) == 12.0


def test_select_open_tick_uses_closest_fallback_when_first_after_too_late():
    m = load_mod()
    ticks = [
        m.SourceTick(price=99.5, exchange_ts=999.9, receive_ts=999.91),
        m.SourceTick(price=100.5, exchange_ts=1000.8, receive_ts=1000.81),
    ]
    selected = m.select_open_tick(ticks, 1000.0, first_after_max_delay_ms=500, closest_max_abs_ms=250)
    assert selected is not None
    assert selected.price == 99.5
    assert selected.method == "closest_tick"
    assert round(selected.distance_ms, 1) == -100.0


def test_select_open_tick_returns_none_when_no_tick_near_boundary():
    m = load_mod()
    ticks = [m.SourceTick(price=99.0, exchange_ts=998.0, receive_ts=998.01), m.SourceTick(price=101.0, exchange_ts=1001.0, receive_ts=1001.01)]
    assert m.select_open_tick(ticks, 1000.0, first_after_max_delay_ms=500, closest_max_abs_ms=250) is None


def test_parse_bybit_orderbook_mid_and_okx_books5_mid():
    m = load_mod()
    assert m.mid_from_levels([["99", "1"]], [["101", "2"]]) == 100.0
    assert m.mid_from_levels([], [["101", "2"]]) is None
