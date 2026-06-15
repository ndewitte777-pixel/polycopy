"""
Position Monitor
================
Runs on every POSITION_MONITOR_INTERVAL tick and checks all open lots for
autonomous exit conditions:

1. Take-profit    — sell HALF when current price >= entry * TAKE_PROFIT_MULTIPLIER
2. Trailing stop  — sell ALL if price drops TRAILING_STOP_PCT% from its peak
3. Hard stop loss — sell ALL if price drops HARD_STOP_LOSS_PCT% from entry
4. Time decay     — sell ALL if market closes within TIME_DECAY_DAYS_LEFT days
                    AND current price <= TIME_DECAY_MAX_PRICE

Price is fetched from the Polymarket CLOB midpoint endpoint.
"""

import time
import logging
from datetime import datetime, timezone

import requests

from config import (
    TAKE_PROFIT_MULTIPLIER,
    TRAILING_STOP_PCT,
    HARD_STOP_LOSS_PCT,
    TIME_DECAY_DAYS_LEFT,
    TIME_DECAY_MAX_PRICE,
    CLOB_API_URL,
    DRY_RUN,
)
import notifier

log = logging.getLogger("polycopy.monitor")


def get_current_price(token_id: str, session: requests.Session) -> float:
    """Fetch current midpoint price for a token from the CLOB."""
    try:
        url = f"{CLOB_API_URL}/midpoint"
        r = session.get(url, params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        data = r.json()
        return float(data.get("mid", 0) or 0)
    except Exception as e:
        log.warning("Failed to fetch price for token %s: %s", token_id[:12], e)
        return 0.0


def days_until_close(end_date_str: str) -> float:
    """Return days remaining until market end date. Returns 999 if unknown."""
    if not end_date_str:
        return 999.0
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0.0, (end - now).total_seconds() / 86400)
    except Exception:
        return 999.0


def check_and_exit(token_id: str, lots: list, executor, state: dict,
                   session: requests.Session) -> list:
    """
    Check a token's open lots for exit conditions.
    Returns the updated lots list (empty if fully exited).
    Calls executor.place_order() for any triggered exits.
    """
    current_price = get_current_price(token_id, session)
    if current_price <= 0:
        return lots  # can't make decisions without price

    remaining_lots = []

    for lot in lots:
        entry_price = lot.get("entry_price", 0)
        peak_price = lot.get("peak_price", entry_price)
        size_usdc = lot.get("size_usdc", 0)
        market_info = lot.get("market_info", {})
        wallet = lot.get("wallet", "unknown")
        end_date = market_info.get("end_date", "")

        # Update peak price
        if current_price > peak_price:
            lot["peak_price"] = current_price
            peak_price = current_price

        if entry_price <= 0 or size_usdc <= 0:
            remaining_lots.append(lot)
            continue

        # --- Compute current P&L ---
        token_qty = size_usdc / entry_price
        current_value = token_qty * current_price
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        drawdown_from_peak = ((peak_price - current_price) / peak_price) * 100 if peak_price > 0 else 0

        exit_reason = None
        exit_fraction = 1.0  # default: sell everything

        # 1. TAKE PROFIT — sell half at 2x
        if current_price >= entry_price * TAKE_PROFIT_MULTIPLIER:
            if not lot.get("took_profit"):
                exit_reason = f"TAKE PROFIT (+{pnl_pct:.0f}%)"
                exit_fraction = 0.5  # sell half, let rest ride
                lot["took_profit"] = True

        # 2. TRAILING STOP — sell all if dropped X% from peak
        elif drawdown_from_peak >= TRAILING_STOP_PCT:
            exit_reason = f"TRAILING STOP (dropped {drawdown_from_peak:.0f}% from peak of {peak_price:.3f})"

        # 3. HARD STOP LOSS — sell all if down X% from entry
        elif pnl_pct <= -HARD_STOP_LOSS_PCT:
            exit_reason = f"HARD STOP LOSS ({pnl_pct:.0f}% from entry)"

        # 4. TIME DECAY — cut losses close to expiry
        elif (days_until_close(end_date) <= TIME_DECAY_DAYS_LEFT
              and current_price <= TIME_DECAY_MAX_PRICE):
            days_left = days_until_close(end_date)
            exit_reason = (f"TIME DECAY ({days_left:.1f}d left, "
                           f"price={current_price:.3f} <= {TIME_DECAY_MAX_PRICE})")

        if exit_reason:
            close_size_usdc = size_usdc * exit_fraction
            exit_value = (size_usdc / entry_price) * current_price * exit_fraction
            pnl_usdc = exit_value - close_size_usdc

            log.info(
                "AUTO EXIT | %s | token=%s | entry=%.3f current=%.3f "
                "size=$%.2f pnl=$%.2f | reason: %s",
                market_info.get("question", token_id[:12]),
                token_id[:12], entry_price, current_price,
                close_size_usdc, pnl_usdc, exit_reason,
            )

            executor.place_order(
                token_id=token_id,
                side="SELL",
                price=current_price,
                size_usdc=close_size_usdc,
            )

            notifier.notify_trade_closed(
                wallet=wallet,
                market_info=market_info,
                entry_price=entry_price,
                exit_price=current_price,
                size_usdc=close_size_usdc,
                pnl_usdc=pnl_usdc,
                dry_run=DRY_RUN,
                reason=exit_reason,
            )

            # Track realized losses for daily limit
            if pnl_usdc < 0:
                state["daily_loss"] = state.get("daily_loss", 0.0) + abs(pnl_usdc)

            # Keep remaining portion open if partial exit
            remaining_size = size_usdc * (1 - exit_fraction)
            if remaining_size > 0.01:
                lot["size_usdc"] = remaining_size
                remaining_lots.append(lot)
            else:
                state["open_positions"] = max(0, state.get("open_positions", 0) - 1)
        else:
            remaining_lots.append(lot)

    return remaining_lots


def run_monitor(open_lots: dict, executor, state: dict, session: requests.Session):
    """
    Called from the main loop on each POSITION_MONITOR_INTERVAL tick.
    Iterates all open lots and applies exit logic.
    Mutates open_lots in place.
    """
    if not open_lots:
        return

    tokens = list(open_lots.keys())
    for token_id in tokens:
        lots = open_lots.get(token_id, [])
        if not lots:
            open_lots.pop(token_id, None)
            continue

        updated = check_and_exit(token_id, lots, executor, state, session)
        if updated:
            open_lots[token_id] = updated
        else:
            open_lots.pop(token_id, None)
