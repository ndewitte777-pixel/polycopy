"""
Simple JSON-file state store.
Tracks seen tx hashes, daily loss/profit, open positions, and executed trades.
"""

import json
import os
import time
import logging
from config import STATE_FILE

log = logging.getLogger("polycopy.state")


def load_state():
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        # Ensure all keys exist for older state files
        defaults = _default_state()
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log.error("Failed to load state, starting fresh: %s", e)
        return _default_state()


def _default_state():
    return {
        "seen_tx_hashes": [],
        "daily_loss": 0.0,
        "daily_profit": 0.0,
        "daily_date": _today(),
        "open_positions": 0,
        "executed_trades": [],
        "open_lots": {},
        "last_weekly_report_date": "",
        "peak_daily_profit": 0.0,
    }


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error("Failed to save state: %s", e)


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def mark_seen(state, tx_hash):
    state["seen_tx_hashes"].append(tx_hash)
    state["seen_tx_hashes"] = state["seen_tx_hashes"][-2000:]


def already_seen(state, tx_hash):
    return tx_hash in state["seen_tx_hashes"]


def reset_daily_if_needed(state):
    today = _today()
    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_loss"] = 0.0
        state["daily_profit"] = 0.0
        state["peak_daily_profit"] = 0.0
        log.info("Daily stats reset for %s", today)


def record_pnl(state, pnl_usdc: float):
    """Call this whenever a trade closes with a P&L."""
    reset_daily_if_needed(state)
    if pnl_usdc > 0:
        state["daily_profit"] = state.get("daily_profit", 0.0) + pnl_usdc
        state["peak_daily_profit"] = max(
            state.get("peak_daily_profit", 0.0),
            state["daily_profit"]
        )
    else:
        state["daily_loss"] = state.get("daily_loss", 0.0) + abs(pnl_usdc)
        # Also reduce daily profit tracking
        state["daily_profit"] = max(0.0, state.get("daily_profit", 0.0) + pnl_usdc)


def net_daily_pnl(state) -> float:
    return state.get("daily_profit", 0.0) - state.get("daily_loss", 0.0)


def record_trade(state, trade_record):
    state["executed_trades"].append(trade_record)
    state["executed_trades"] = state["executed_trades"][-500:]
