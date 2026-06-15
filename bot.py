"""
Polymarket Copy-Trading Bot
===========================

Monitors a list of target wallet addresses for new on-chain trades
and replicates them (proportionally) on your own account.

Run modes:
- DRY_RUN = True (default, in config.py): logs what it WOULD do, no real orders.
- DRY_RUN = False: places real orders via py-clob-client. Requires PRIVATE_KEY.

Usage:
    python bot.py
"""

import time
import logging
import sys

from config import (
    TARGET_WALLETS,
    POLL_INTERVAL_SECONDS,
    ACTIVITY_LOOKBACK_SECONDS,
    MIN_TRADE_USDC,
    ONLY_COPY_BUYS,
    COPY_SCALE_FACTOR,
    MAX_TRADE_USDC,
    MAX_DAILY_LOSS_USDC,
    MAX_OPEN_POSITIONS,
    SAME_TOKEN_COOLDOWN_SECONDS,
    YOUR_BANKROLL_USDC,
    HEARTBEAT_SILENCE_SECONDS,
    ERROR_ALERT_THRESHOLD,
    DRY_RUN,
    LOG_FILE,
)
from data_api import DataAPI
from executor import Executor
import state as st
import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("polycopy.bot")


def estimate_trader_bankroll(data_api: DataAPI, wallet: str) -> float:
    """
    Rough estimate of a trader's total bankroll based on their open positions'
    current value. Used to compute what % of their bankroll a trade represents.
    Falls back to a default if positions can't be fetched.
    """
    positions = data_api.get_positions(wallet)
    total = 0.0
    if isinstance(positions, list):
        for p in positions:
            # Field names vary; try common ones defensively
            val = p.get("currentValue") or p.get("value") or 0
            try:
                total += float(val)
            except (TypeError, ValueError):
                pass
    return total if total > 0 else 1000.0  # fallback assumption


def process_activity_item(data_api: DataAPI, executor: Executor, state: dict,
                           wallet: str, item: dict, your_bankroll: float,
                           trader_bankroll: float):
    tx_hash = item.get("transactionHash")
    if not tx_hash or st.already_seen(state, tx_hash):
        return

    side = item.get("side", "").upper()
    if ONLY_COPY_BUYS and side != "BUY":
        st.mark_seen(state, tx_hash)
        return

    usdc_size = float(item.get("usdcSize", 0) or 0)
    if usdc_size < MIN_TRADE_USDC:
        st.mark_seen(state, tx_hash)
        return

    token_id = item.get("asset") or item.get("tokenId")
    price = float(item.get("price", 0) or 0)
    condition_id = item.get("conditionId")

    if not token_id or price <= 0:
        log.warning("Skipping malformed activity item: %s", item)
        st.mark_seen(state, tx_hash)
        return

    # --- Risk checks ---
    st.reset_daily_if_needed(state)
    if state["daily_loss"] >= MAX_DAILY_LOSS_USDC:
        log.warning("Daily loss limit reached (%.2f). Skipping trade.", state["daily_loss"])
        st.mark_seen(state, tx_hash)
        return

    if state.get("open_positions", 0) >= MAX_OPEN_POSITIONS and side == "BUY":
        log.warning("Max open positions reached. Skipping BUY.")
        st.mark_seen(state, tx_hash)
        return

    # --- Cooldown: skip if we already hold/recently opened this exact token ---
    open_lots = state.setdefault("open_lots", {})
    lots_for_token = open_lots.get(token_id, [])
    if side == "BUY" and lots_for_token:
        last_ts = lots_for_token[-1].get("opened_at", 0)
        if time.time() - last_ts < SAME_TOKEN_COOLDOWN_SECONDS:
            log.info("Skipping repeat BUY on token already held (cooldown): %s", token_id)
            st.mark_seen(state, tx_hash)
            return

    # --- Position sizing ---
    trader_fraction = usdc_size / trader_bankroll if trader_bankroll else 0
    your_size = your_bankroll * trader_fraction * COPY_SCALE_FACTOR
    your_size = min(your_size, MAX_TRADE_USDC)
    your_size = max(your_size, 1.0)  # Polymarket has small minimums; adjust if needed

    # --- Resolve human-readable market info ---
    market_info = data_api.get_market_info(condition_id, token_id) if condition_id else {
        "question": "Unknown market",
        "outcome": "Unknown outcome",
        "url": "",
        "end_date": "",
        "liquidity": 0,
    }

    log.info(
        "COPY SIGNAL | wallet=%s side=%s\n"
        "  Market:  %s\n"
        "  Betting: %s @ %.3f (%s)\n"
        "  Their size: $%.2f (%.2f%% of bankroll) -> Your size: $%.2f\n"
        "  Closes: %s | Liquidity: $%.0f\n"
        "  %s",
        wallet, side,
        market_info["question"],
        market_info["outcome"], price, "YES/NO implied" if price < 1 else "",
        usdc_size, trader_fraction * 100, your_size,
        market_info["end_date"] or "unknown",
        market_info["liquidity"],
        market_info["url"],
    )

    if side == "BUY":
        resp = executor.place_order(token_id=token_id, side=side, price=price, size_usdc=your_size)
        st.record_trade(state, {
            "tx_hash": tx_hash,
            "wallet": wallet,
            "side": side,
            "token_id": token_id,
            "condition_id": condition_id,
            "price": price,
            "size_usdc": your_size,
            "timestamp": item.get("timestamp"),
            "response": resp,
        })

        state["open_positions"] = state.get("open_positions", 0) + 1
        # Track this lot so we can compute P&L when it's sold
        lots_for_token.append({
            "entry_price": price,
            "size_usdc": your_size,
            "wallet": wallet,
            "condition_id": condition_id,
            "market_info": market_info,
            "opened_at": time.time(),
        })
        open_lots[token_id] = lots_for_token
        notifier.notify_trade_opened(
            wallet, side, market_info, price, your_size, DRY_RUN
        )

    elif side == "SELL":
        if not lots_for_token:
            # We have no record of opening this position (e.g. bot restarted,
            # or this SELL just closes a position we never copied the BUY for).
            log.info("SELL signal for token with no tracked open lots: %s", token_id)
            st.mark_seen(state, tx_hash)
            return

        # Determine what fraction of THEIR position this sell represents,
        # so we close the same fraction of OUR lots (handles partial sells).
        sold_qty = float(item.get("size", 0) or 0)
        remaining_positions = data_api.get_positions(wallet)
        their_remaining_qty = 0.0
        if isinstance(remaining_positions, list):
            for p in remaining_positions:
                if (p.get("asset") or p.get("tokenId")) == token_id:
                    try:
                        their_remaining_qty = float(p.get("size", 0) or 0)
                    except (TypeError, ValueError):
                        their_remaining_qty = 0.0
                    break

        # Fraction of their pre-sell position that this trade sold
        their_pre_sell_qty = their_remaining_qty + sold_qty
        sell_fraction = (sold_qty / their_pre_sell_qty) if their_pre_sell_qty > 0 else 1.0
        sell_fraction = min(max(sell_fraction, 0.0), 1.0)

        total_lot_size = sum(l["size_usdc"] for l in lots_for_token)
        total_pnl = 0.0
        remaining_lots = []

        for lot in lots_for_token:
            entry_price = lot["entry_price"]
            lot_market_info = lot.get("market_info", market_info)
            close_size_usdc = lot["size_usdc"] * sell_fraction
            remaining_size_usdc = lot["size_usdc"] - close_size_usdc

            if close_size_usdc > 0:
                token_qty = close_size_usdc / entry_price if entry_price else 0
                exit_value = token_qty * price
                pnl_usdc = exit_value - close_size_usdc
                total_pnl += pnl_usdc
                notifier.notify_trade_closed(
                    wallet, lot_market_info, entry_price, price,
                    close_size_usdc, pnl_usdc, DRY_RUN,
                )

            if remaining_size_usdc > 0.01:  # keep lot open if meaningfully remains
                lot["size_usdc"] = remaining_size_usdc
                remaining_lots.append(lot)

        if remaining_lots:
            open_lots[token_id] = remaining_lots
        else:
            open_lots.pop(token_id, None)
            state["open_positions"] = max(0, state.get("open_positions", 0) - 1)

        # Track realized losses against the daily loss limit
        if total_pnl < 0:
            state["daily_loss"] = state.get("daily_loss", 0.0) + abs(total_pnl)

        log.info(
            "SELL processed | wallet=%s token=%s sell_fraction=%.3f total_pnl=%.2f "
            "remaining_lots=%d",
            wallet, token_id, sell_fraction, total_pnl, len(remaining_lots),
        )

        # Execute the proportional sell on your side too
        your_sell_size = total_lot_size * sell_fraction
        if your_sell_size > 0:
            resp = executor.place_order(
                token_id=token_id, side="SELL", price=price, size_usdc=your_sell_size
            )
            st.record_trade(state, {
                "tx_hash": tx_hash,
                "wallet": wallet,
                "side": side,
                "token_id": token_id,
                "condition_id": condition_id,
                "price": price,
                "size_usdc": your_sell_size,
                "timestamp": item.get("timestamp"),
                "response": resp,
            })

    st.mark_seen(state, tx_hash)


def run():
    if not TARGET_WALLETS:
        log.error("No TARGET_WALLETS configured in config.py. Add at least one wallet address.")
        return

    log.info("Starting Polymarket copy bot. DRY_RUN=%s, targets=%s", DRY_RUN, TARGET_WALLETS)

    data_api = DataAPI()
    executor = Executor()
    state = st.load_state()

    your_bankroll = YOUR_BANKROLL_USDC
    log.info("Using bankroll: $%.2f", your_bankroll)

    while True:
        try:
            for wallet in TARGET_WALLETS:
                wallet = wallet.lower()
                activity = data_api.get_activity(wallet, limit=20, types=("TRADE",))
                if not activity:
                    continue

                trader_bankroll = estimate_trader_bankroll(data_api, wallet)
                now = time.time()

                for item in activity:
                    ts = item.get("timestamp", 0)
                    if now - ts > ACTIVITY_LOOKBACK_SECONDS:
                        continue
                    process_activity_item(
                        data_api, executor, state, wallet, item,
                        your_bankroll, trader_bankroll,
                    )

            st.save_state(state)

        except Exception as e:
            log.exception("Error in main loop: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
