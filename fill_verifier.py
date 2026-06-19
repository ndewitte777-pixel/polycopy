"""
Order Fill Verifier
===================
Verifies that orders placed on Kalshi actually filled.
Resting limit orders may never fill — we track them and cancel if not filled
within a reasonable time.

States:
- "resting": order placed but not yet filled
- "executed": order confirmed filled  
- "cancelled": we cancelled it (didn't fill in time)
"""

import logging
import time

log = logging.getLogger("polycopy.fill_verifier")

# How long to wait for a resting order before cancelling (seconds)
FILL_TIMEOUT_SECONDS = 120  # 2 minutes


def verify_order(resp: dict, ticker: str, side: str,
                 size_usdc: float, api) -> dict:
    """
    Check the order response and verify fill status.
    Returns enriched order info with fill_status.
    """
    if not resp or resp.get("dry_run") or resp.get("skipped"):
        return resp

    order = resp.get("order", resp)
    status = order.get("status", "unknown")
    order_id = order.get("order_id", "")

    if status == "executed":
        log.info("Order FILLED immediately: %s %s $%.2f", side, ticker, size_usdc)
        return {**resp, "fill_status": "filled", "filled_immediately": True}

    elif status == "resting":
        log.info("Order RESTING (limit order in book): %s %s $%.2f order_id=%s",
                 side, ticker, size_usdc, order_id[:8])
        return {
            **resp,
            "fill_status": "resting",
            "order_id": order_id,
            "resting_since": time.time(),
            "ticker": ticker,
            "size_usdc": size_usdc,
        }

    else:
        log.warning("Order status unknown: %s", status)
        return {**resp, "fill_status": "unknown"}


def check_pending_orders(state: dict, api) -> list:
    """
    Check all pending (resting) orders and cancel timed-out ones.
    Returns list of tickers where orders were cancelled.
    """
    pending = state.get("pending_orders", {})
    cancelled = []
    now = time.time()

    for order_id, order_info in list(pending.items()):
        resting_since = order_info.get("resting_since", now)
        elapsed = now - resting_since

        if elapsed > FILL_TIMEOUT_SECONDS:
            ticker = order_info.get("ticker", "?")
            log.info("Order %s timed out after %.0fs — cancelling resting order for %s",
                     order_id[:8], elapsed, ticker)
            try:
                if api and hasattr(api, "cancel_order"):
                    api.cancel_order(order_id)
                    log.info("Cancelled order %s", order_id[:8])
            except Exception as e:
                log.warning("Could not cancel order %s: %s", order_id[:8], e)

            # Remove from state
            del pending[order_id]

            # Also remove from open_lots if it was never filled
            open_lots = state.get("open_lots", {})
            if ticker in open_lots:
                # Remove lots that match this order
                open_lots[ticker] = [
                    lot for lot in open_lots.get(ticker, [])
                    if lot.get("order_id") != order_id
                ]
                if not open_lots[ticker]:
                    del open_lots[ticker]
                state["open_positions"] = max(0, state.get("open_positions", 0) - 1)
                # Refund at-risk amount
                size = order_info.get("size_usdc", 0)
                state["total_at_risk"] = max(0, state.get("total_at_risk", 0) - size)

            cancelled.append(ticker)

    return cancelled


def track_order(state: dict, order_resp: dict, ticker: str,
                size_usdc: float, lot_entry: dict):
    """
    Track a resting order in state so we can check fill status later.
    """
    fill_status = order_resp.get("fill_status", "")
    if fill_status != "resting":
        return  # only track resting orders

    order_id = order_resp.get("order_id", "")
    if not order_id:
        return

    pending = state.setdefault("pending_orders", {})
    pending[order_id] = {
        "ticker": ticker,
        "size_usdc": size_usdc,
        "resting_since": time.time(),
        "lot_entry": lot_entry,
    }

    # Also tag the lot with the order_id so we can remove it if cancelled
    lot_entry["order_id"] = order_id
    lot_entry["fill_status"] = "resting"

    log.info("Tracking resting order %s for %s", order_id[:8], ticker)
