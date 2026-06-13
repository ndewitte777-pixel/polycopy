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
    DRY_RUN,
    LOG_FILE,
)
from data_api import DataAPI
from executor import Executor
import state as st

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

    # --- Position sizing ---
    trader_fraction = usdc_size / trader_bankroll if trader_bankroll else 0
    your_size = your_bankroll * trader_fraction * COPY_SCALE_FACTOR
    your_size = min(your_size, MAX_TRADE_USDC)
    your_size = max(your_size, 1.0)  # Polymarket has small minimums; adjust if needed

    log.info(
        "COPY SIGNAL | wallet=%s side=%s market=%s token=%s price=%.3f "
        "their_usdc=%.2f their_fraction=%.4f -> your_size=%.2f",
        wallet, side, condition_id, token_id, price, usdc_size, trader_fraction, your_size,
    )

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

    if side == "BUY":
        state["open_positions"] = state.get("open_positions", 0) + 1
    elif side == "SELL":
        state["open_positions"] = max(0, state.get("open_positions", 0) - 1)

    st.mark_seen(state, tx_hash)


def run():
    if not TARGET_WALLETS:
        log.error("No TARGET_WALLETS configured in config.py. Add at least one wallet address.")
        return

    log.info("Starting Polymarket copy bot. DRY_RUN=%s, targets=%s", DRY_RUN, TARGET_WALLETS)

    data_api = DataAPI()
    executor = Executor()
    state = st.load_state()

    # TODO: replace with real bankroll lookup for your own account
    your_bankroll = 100.0

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
