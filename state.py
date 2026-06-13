"""
Simple JSON-file state store: tracks which activity items we've already
processed (to avoid double-copying) and daily loss tracking for the kill switch.
"""

import json
import os
import time
import logging
from config import STATE_FILE

log = logging.getLogger("polycopy.state")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "seen_tx_hashes": [],
            "daily_loss": 0.0,
            "daily_loss_date": _today(),
            "open_positions": 0,
            "executed_trades": [],
        }
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def mark_seen(state, tx_hash):
    state["seen_tx_hashes"].append(tx_hash)
    # keep list bounded
    state["seen_tx_hashes"] = state["seen_tx_hashes"][-2000:]


def already_seen(state, tx_hash):
    return tx_hash in state["seen_tx_hashes"]


def reset_daily_if_needed(state):
    today = _today()
    if state.get("daily_loss_date") != today:
        state["daily_loss_date"] = today
        state["daily_loss"] = 0.0


def record_trade(state, trade_record):
    state["executed_trades"].append(trade_record)
    state["executed_trades"] = state["executed_trades"][-500:]
