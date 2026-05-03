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


def test_config_can_limit_to_btc_and_set_paper_bankroll(tmp_path):
    m = load_mod()
    cfg = m.StrategyConfig(log_path=tmp_path / "events.jsonl", coins=["btc"], paper_bankroll=30.0)
    trader = m.V1Trader(cfg)
    assert list(trader.state.keys()) == ["btc"]
    assert trader.paper.bankroll == 30.0
    assert trader.paper.cash == 30.0


def test_paper_buy_spends_up_to_configured_stake_and_records_position(tmp_path):
    m = load_mod()
    cfg = m.StrategyConfig(log_path=tmp_path / "events.jsonl", coins=["btc"], paper_bankroll=30.0, paper_trade_notional=30.0, entry_price_cap=0.65)
    trader = m.V1Trader(cfg)
    result = trader.paper.record_buy(
        coin="btc",
        source="coinbase_market_trades",
        direction="UP",
        token_id="up-token",
        price=0.65,
        max_notional=30.0,
        round_start=1800,
        ts=2692.0,
        meta={"reason": "ok"},
    )
    assert result["paper_status"] == "filled"
    assert result["shares"] == 30.0 / 0.65
    assert trader.paper.cash == 0.0
    assert trader.paper.positions[0].direction == "UP"


def test_dashboard_state_contains_quotes_opens_paper_and_config(tmp_path):
    m = load_mod()
    cfg = m.StrategyConfig(log_path=tmp_path / "events.jsonl", coins=["btc"], paper_bankroll=30.0)
    trader = m.V1Trader(cfg)
    st = trader.state["btc"]
    st.current_round = 1800
    st.prices["binance_trade"] = 100.5
    st.source_opens["binance_trade"] = 100.0
    st.source_open_status["binance_trade"] = "locked"
    state = trader.dashboard_state()
    assert state["config"]["port"] == 30503
    assert state["config"]["coins"] == ["btc"]
    assert state["paper"]["bankroll"] == 30.0
    assert state["coins"]["btc"]["quotes"]["binance_trade"]["price"] == 100.5
    assert state["coins"]["btc"]["quotes"]["binance_trade"]["source_open"] == 100.0
