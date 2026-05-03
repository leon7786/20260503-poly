#!/usr/bin/env python3
"""Dry-run health check for poly_v1_trader.

Checks imports, config, py-clob-client order option construction, and public CLOB/Gamma reachability.
Does not place orders and does not require wallet envs unless LIVE_TRADING=1 is explicitly set.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from poly_v1_trader import GAMMA, HOST, PolymarketExecutor, StrategyConfig, JsonLogger, round_window  # noqa: E402


def get_json(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, headers={"User-Agent": "poly-v1-health-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    log_path = ROOT / "logs" / "v1_health_check.jsonl"
    cfg = StrategyConfig(live_trading=False, log_path=log_path)
    logger = JsonLogger(log_path)
    executor = PolymarketExecutor(cfg, logger)
    opt = executor._order_options()
    start, end, secs_left = round_window()
    checks = {
        "root": str(ROOT),
        "live_trading": cfg.live_trading,
        "round_start": start,
        "round_end": end,
        "secs_left": secs_left,
        "order_options": {"tick_size": getattr(opt, "tick_size", None), "neg_risk": getattr(opt, "neg_risk", None)} if opt else None,
    }
    try:
        checks["clob_ok"] = get_json(HOST + "/")
    except Exception as e:
        checks["clob_error"] = str(e)
    try:
        checks["gamma_sample_len"] = len(get_json(GAMMA + "/markets?limit=1"))
    except Exception as e:
        checks["gamma_error"] = str(e)
    logger.write({"event": "health_check", **checks})
    print(json.dumps(checks, indent=2, sort_keys=True, default=str))
    if "clob_error" in checks or "gamma_error" in checks:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
