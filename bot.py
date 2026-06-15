"""
Polymarket Copy-Trading Bot
===========================
Features:
- Copies trades from top leaderboard wallets
- Conviction scoring (multiple wallets = bigger size)
- Price slip filter (skip stale signals)
- Kelly criterion sizing (optional)
- Category filter + portfolio exposure limits
- Auto take-profit, trailing stop, hard stop, time-decay exits
- Heartbeat + error alerts via Pushover
- Weekly P&L report

Usage:
    python bot.py
"""

import time
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone

import requests

from config import (
    TARGET_WALLETS,
    POLL_INTERVAL_SECONDS,
    ACTIVITY_LOOKBACK_SECONDS,
    POSITION_MONITOR_INTERVAL,
    MIN_TRADE_USDC,
    MIN_MARKET_LIQUIDITY,
    ONLY_COPY_BUYS,
    MAX_PRICE_SLIP_PCT,
    SAME_TOKEN_COOLDOWN_SECONDS,
    CONVICTION_THRESHOLD,
    CONVICTION_WINDOW_SECONDS,
    CONVICTION_SIZE_MULTIPLIER,
    COPY_SCALE_FACTOR,
    MAX_TRADE_USDC,
    MAX_DAILY_LOSS_USDC,
    MAX_OPEN_POSITIONS,
    USE_KELLY,
    KELLY_FRACTION,
    USE_CLAUDE_FILTER,
    CLAUDE_MIN_CONFIDENCE,
    USE_CLAUDE_TRADER,
    CLAUDE_TRADER_INTERVAL,
    USE_LIVE_SCALPER,
    LIVE_POLL_INTERVAL,
    KELLY_FRACTION,
    ALLOWED_CATEGORIES,
    MAX_CATEGORY_EXPOSURE_PCT,
    YOUR_BANKROLL_USDC,  # used as fallback in executor.get_balance()
    HEARTBEAT_SILENCE_SECONDS,
    ERROR_ALERT_THRESHOLD,
    WEEKLY_REPORT_DAY,
    WEEKLY_REPORT_HOUR,
    DRY_RUN,
    LOG_FILE,
)
from data_api import DataAPI
from executor import Executor
import position_monitor as pm
import state as st
import notifier
import claude_filter as cf
import claude_trader as ct
import scalper as sc
import sports_data as sd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("polycopy.bot")


# ── Conviction tracker ──────────────────────────────────────────────────────
# Maps token_id -> list of {wallet, timestamp, price}
_conviction_log: dict = defaultdict(list)


def record_conviction(token_id: str, wallet: str, price: float):
    now = time.time()
    _conviction_log[token_id].append({"wallet": wallet, "ts": now, "price": price})
    # Prune old entries
    cutoff = now - CONVICTION_WINDOW_SECONDS
    _conviction_log[token_id] = [
        e for e in _conviction_log[token_id] if e["ts"] >= cutoff
    ]


def get_conviction(token_id: str) -> tuple[int, list]:
    """Returns (count_of_unique_wallets, list_of_wallets) buying this token recently."""
    cutoff = time.time() - CONVICTION_WINDOW_SECONDS
    recent = [e for e in _conviction_log.get(token_id, []) if e["ts"] >= cutoff]
    wallets = list({e["wallet"] for e in recent})
    return len(wallets), wallets


# ── Position sizing ─────────────────────────────────────────────────────────

def kelly_size(price: float, your_bankroll: float) -> float:
    """
    Kelly criterion for binary outcome market.
    Assumes fair probability = current price (i.e. no edge model).
    In practice you'd substitute your own probability estimate.
    b = (1 - price) / price  (decimal odds for YES outcome)
    f = (p*(b+1) - 1) / b
    """
    if price <= 0 or price >= 1:
        return 0.0
    p = price  # our estimate of true prob (just using market price as placeholder)
    b = (1 - price) / price
    f = (p * (b + 1) - 1) / b
    f = max(0.0, f * KELLY_FRACTION)
    return your_bankroll * f


def compute_size(usdc_size: float, trader_bankroll: float,
                 your_bankroll: float, price: float,
                 conviction: int) -> float:
    """Compute your position size."""
    if USE_KELLY:
        base = kelly_size(price, your_bankroll)
    else:
        trader_fraction = usdc_size / trader_bankroll if trader_bankroll else 0
        base = your_bankroll * trader_fraction * COPY_SCALE_FACTOR

    # Conviction multiplier
    if conviction >= CONVICTION_THRESHOLD:
        base *= CONVICTION_SIZE_MULTIPLIER

    return min(max(base, 1.0), MAX_TRADE_USDC)


# ── Category exposure check ─────────────────────────────────────────────────

def category_ok(market_info: dict, open_lots: dict, your_bankroll: float) -> bool:
    """Return False if adding this position would exceed MAX_CATEGORY_EXPOSURE_PCT."""
    category = market_info.get("category", "").upper()

    if ALLOWED_CATEGORIES and category not in [c.upper() for c in ALLOWED_CATEGORIES]:
        log.info("Skipping: category %s not in ALLOWED_CATEGORIES", category)
        return False

    if MAX_CATEGORY_EXPOSURE_PCT <= 0:
        return True

    # Sum current exposure in this category
    total_exposure = 0.0
    for lots in open_lots.values():
        for lot in lots:
            if lot.get("market_info", {}).get("category", "").upper() == category:
                total_exposure += lot.get("size_usdc", 0)

    max_allowed = your_bankroll * (MAX_CATEGORY_EXPOSURE_PCT / 100)
    if total_exposure >= max_allowed:
        log.info(
            "Skipping: category %s already at %.2f / %.2f exposure limit",
            category, total_exposure, max_allowed,
        )
        return False
    return True


# ── Trader bankroll estimate ────────────────────────────────────────────────

def estimate_trader_bankroll(data_api: DataAPI, wallet: str) -> float:
    positions = data_api.get_positions(wallet)
    total = 0.0
    if isinstance(positions, list):
        for p in positions:
            val = p.get("currentValue") or p.get("value") or 0
            try:
                total += float(val)
            except (TypeError, ValueError):
                pass
    return total if total > 0 else 1000.0


# ── Weekly report ───────────────────────────────────────────────────────────

def build_weekly_report(state: dict) -> dict:
    trades = state.get("executed_trades", [])
    week_ago = time.time() - 7 * 86400
    recent = [t for t in trades if (t.get("timestamp") or 0) >= week_ago]

    wins, losses, total_pnl = 0, 0, 0.0
    best, worst = 0.0, 0.0
    wallet_pnl: dict = defaultdict(float)

    for t in recent:
        pnl = t.get("pnl_usdc", 0) or 0
        total_pnl += pnl
        if pnl > 0:
            wins += 1
            best = max(best, pnl)
        elif pnl < 0:
            losses += 1
            worst = min(worst, pnl)
        wallet_pnl[t.get("wallet", "?")] += pnl

    best_wallet = max(wallet_pnl, key=wallet_pnl.get) if wallet_pnl else "N/A"
    win_rate = (wins / len(recent) * 100) if recent else 0

    return {
        "trade_count": len(recent),
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
        "win_rate_pct": win_rate,
        "best_trade": best,
        "worst_trade": worst,
        "best_wallet": best_wallet[:8] + "..." if best_wallet != "N/A" else "N/A",
        "open_positions": state.get("open_positions", 0),
    }


def should_send_weekly_report(state: dict) -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() != WEEKLY_REPORT_DAY:
        return False
    if now.hour != WEEKLY_REPORT_HOUR:
        return False
    last = state.get("last_weekly_report_date", "")
    today = now.strftime("%Y-%m-%d")
    return last != today


# ── Main activity processor ─────────────────────────────────────────────────

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
        st.mark_seen(state, tx_hash)
        return

    # --- Price slip filter ---
    if side == "BUY":
        try:
            session = data_api.session
            current_price = pm.get_current_price(token_id, session)
            if current_price > 0:
                slip_pct = abs(current_price - price) / price * 100
                if slip_pct > MAX_PRICE_SLIP_PCT:
                    log.info(
                        "Skipping: price slipped %.1f%% since their trade "
                        "(their=%.3f current=%.3f)", slip_pct, price, current_price
                    )
                    st.mark_seen(state, tx_hash)
                    return
                price = current_price  # use current price for our order
        except Exception:
            pass  # can't check slip, proceed anyway

    # --- Risk checks ---
    st.reset_daily_if_needed(state)
    if state["daily_loss"] >= MAX_DAILY_LOSS_USDC:
        log.warning("Daily loss limit hit ($%.2f). Skipping.", state["daily_loss"])
        st.mark_seen(state, tx_hash)
        return

    if state.get("open_positions", 0) >= MAX_OPEN_POSITIONS and side == "BUY":
        log.warning("Max open positions reached. Skipping BUY.")
        st.mark_seen(state, tx_hash)
        return

    # --- Cooldown ---
    open_lots = state.setdefault("open_lots", {})
    lots_for_token = open_lots.get(token_id, [])
    if side == "BUY" and lots_for_token:
        last_ts = lots_for_token[-1].get("opened_at", 0)
        if time.time() - last_ts < SAME_TOKEN_COOLDOWN_SECONDS:
            log.info("Cooldown active for token %s", token_id[:12])
            st.mark_seen(state, tx_hash)
            return

    # --- Market info ---
    market_info = data_api.get_market_info(condition_id, token_id) if condition_id else {
        "question": "Unknown market", "outcome": "Unknown", "url": "",
        "end_date": "", "liquidity": 0, "category": "",
    }

    # --- Liquidity filter ---
    if market_info.get("liquidity", 0) < MIN_MARKET_LIQUIDITY:
        log.info("Skipping: low liquidity $%.0f", market_info.get("liquidity", 0))
        st.mark_seen(state, tx_hash)
        return

    # --- Category filter + exposure ---
    if side == "BUY" and not category_ok(market_info, open_lots, your_bankroll):
        st.mark_seen(state, tx_hash)
        return

    # --- Conviction scoring ---
    if side == "BUY":
        record_conviction(token_id, wallet, price)
    conviction, conviction_wallets = get_conviction(token_id)

    if conviction >= CONVICTION_THRESHOLD and side == "BUY":
        log.info("HIGH CONVICTION: %d wallets on %s", conviction, market_info["question"])
        notifier.notify_high_conviction(token_id, market_info, conviction_wallets, price)

    # --- Position sizing ---
    your_size = compute_size(usdc_size, trader_bankroll, your_bankroll, price, conviction)
    trader_fraction = usdc_size / trader_bankroll if trader_bankroll else 0

    log.info(
        "COPY SIGNAL | wallet=%s side=%s conviction=%d\n"
        "  Market:  %s\n"
        "  Betting: %s @ %.3f (implied %.1f%%)\n"
        "  Their size: $%.2f (%.2f%% of bankroll) -> Your size: $%.2f\n"
        "  Closes: %s | Liquidity: $%.0f | Category: %s\n"
        "  %s",
        wallet, side, conviction,
        market_info["question"],
        market_info["outcome"], price, price * 100,
        usdc_size, trader_fraction * 100, your_size,
        market_info.get("end_date") or "unknown",
        market_info.get("liquidity", 0),
        market_info.get("category", "unknown"),
        market_info["url"],
    )

    # --- Execute BUY ---
    if side == "BUY":
        # Ask Claude whether to proceed
        ai_decision = cf.evaluate_trade(
            market_info=market_info,
            price=price,
            your_size=your_size,
            conviction=conviction,
            trader_bankroll=trader_bankroll,
            usdc_size=usdc_size,
        )
        should_trade, your_size = cf.apply_decision(ai_decision, your_size)

        if not should_trade:
            log.info(
                "Claude SKIPPED trade: %s | confidence=%d | %s",
                market_info.get("question", "?"),
                ai_decision.get("confidence", 0),
                ai_decision.get("reason", ""),
            )
            notifier.send(
                title="🤖 Claude skipped a trade",
                message=(
                    f"{market_info.get('question', '?')}\n"
                    f"Outcome: {market_info.get('outcome', '?')} @ {price:.3f}\n"
                    f"Reason: {ai_decision.get('reason', '')}\n"
                    f"Confidence: {ai_decision.get('confidence', 0)}%"
                ),
            )
            st.mark_seen(state, tx_hash)
            return

        resp = executor.place_order(token_id=token_id, side=side,
                                    price=price, size_usdc=your_size)
        st.record_trade(state, {
            "tx_hash": tx_hash, "wallet": wallet, "side": side,
            "token_id": token_id, "condition_id": condition_id,
            "price": price, "size_usdc": your_size,
            "timestamp": item.get("timestamp"), "response": resp,
        })
        state["open_positions"] = state.get("open_positions", 0) + 1
        lots_for_token.append({
            "entry_price": price,
            "size_usdc": your_size,
            "peak_price": price,
            "wallet": wallet,
            "condition_id": condition_id,
            "market_info": market_info,
            "opened_at": time.time(),
            "took_profit": False,
        })
        open_lots[token_id] = lots_for_token
        notifier.notify_trade_opened(
            wallet, side, market_info, price, your_size, DRY_RUN, conviction
        )

    # --- Execute SELL (copied from target) ---
    elif side == "SELL":
        if not lots_for_token:
            log.info("SELL signal but no tracked lots for token %s", token_id[:12])
            st.mark_seen(state, tx_hash)
            return

        sold_qty = float(item.get("size", 0) or 0)
        remaining_positions = data_api.get_positions(wallet)
        their_remaining_qty = 0.0
        if isinstance(remaining_positions, list):
            for p in remaining_positions:
                if (p.get("asset") or p.get("tokenId")) == token_id:
                    try:
                        their_remaining_qty = float(p.get("size", 0) or 0)
                    except (TypeError, ValueError):
                        pass
                    break

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
                    reason="Target wallet sold",
                )

            if remaining_size_usdc > 0.01:
                lot["size_usdc"] = remaining_size_usdc
                remaining_lots.append(lot)

        if remaining_lots:
            open_lots[token_id] = remaining_lots
        else:
            open_lots.pop(token_id, None)
            state["open_positions"] = max(0, state.get("open_positions", 0) - 1)

        if total_pnl < 0:
            state["daily_loss"] = state.get("daily_loss", 0.0) + abs(total_pnl)

        your_sell_size = total_lot_size * sell_fraction
        if your_sell_size > 0:
            resp = executor.place_order(
                token_id=token_id, side="SELL", price=price, size_usdc=your_sell_size
            )
            st.record_trade(state, {
                "tx_hash": tx_hash, "wallet": wallet, "side": side,
                "token_id": token_id, "condition_id": condition_id,
                "price": price, "size_usdc": your_sell_size,
                "timestamp": item.get("timestamp"), "response": resp,
            })

    st.mark_seen(state, tx_hash)


# ── Main loop ───────────────────────────────────────────────────────────────

def run():
    if not TARGET_WALLETS:
        log.error("No TARGET_WALLETS set. Add wallet addresses in Railway Variables.")
        return

    log.info(
        "Starting Polymarket copy bot. DRY_RUN=%s | Claude filter=%s | "
        "Claude trader=%s (every %dh) | Live scalper=%s (every %ds) | targets=%s",
        DRY_RUN, USE_CLAUDE_FILTER, USE_CLAUDE_TRADER,
        CLAUDE_TRADER_INTERVAL // 3600, USE_LIVE_SCALPER,
        LIVE_POLL_INTERVAL, TARGET_WALLETS,
    )

    data_api = DataAPI()
    executor = Executor()
    session = data_api.session
    state = st.load_state()

    your_bankroll = executor.get_balance()
    log.info("Starting bankroll: $%.2f USDC", your_bankroll)

    last_signal_time = time.time()
    last_monitor_time = 0.0
    last_balance_check = time.time()
    last_claude_trader_time = 0.0
    last_live_poll_time = 0.0
    live_games_cache = []
    BALANCE_REFRESH_SECONDS = 3600
    consecutive_errors = 0

    while True:
        try:
            now = time.time()

            # ── Refresh live balance hourly ───────────────────────────
            if now - last_balance_check >= BALANCE_REFRESH_SECONDS:
                new_balance = executor.get_balance()
                if abs(new_balance - your_bankroll) > 0.01:
                    log.info(
                        "Balance updated: $%.2f -> $%.2f USDC",
                        your_bankroll, new_balance,
                    )
                your_bankroll = new_balance
                last_balance_check = now

            # ── Copy trade detection ──────────────────────────────────────
            for wallet in TARGET_WALLETS:
                wallet = wallet.lower()
                activity = data_api.get_activity(wallet, limit=20, types=("TRADE",))
                if not activity:
                    continue

                trader_bankroll = estimate_trader_bankroll(data_api, wallet)

                for item in activity:
                    ts = item.get("timestamp", 0)
                    if now - ts > ACTIVITY_LOOKBACK_SECONDS:
                        continue
                    process_activity_item(
                        data_api, executor, state, wallet, item,
                        your_bankroll, trader_bankroll,
                    )
                    last_signal_time = now

            # ── Live scalper (fast exits on live sports) ──────────────
            if USE_LIVE_SCALPER and now - last_live_poll_time >= LIVE_POLL_INTERVAL:
                live_games_cache = sd.fetch_all_live_games(session)
                open_lots = state.setdefault("open_lots", {})
                if open_lots and live_games_cache:
                    scalps = sc.run_scalper(
                        open_lots=open_lots,
                        executor=executor,
                        state=state,
                        session=session,
                        live_games=live_games_cache,
                        notifier=notifier,
                    )
                    if scalps:
                        log.info("Scalper: %d positions exited", scalps)
                last_live_poll_time = now

            # ── Position monitor (take-profit / stops / time-decay) ───────
            if now - last_monitor_time >= POSITION_MONITOR_INTERVAL:
                open_lots = state.setdefault("open_lots", {})
                if open_lots:
                    pm.run_monitor(open_lots, executor, state, session)
                last_monitor_time = now

            # ── Heartbeat check ───────────────────────────────────────────
            silence = now - last_signal_time
            if (HEARTBEAT_SILENCE_SECONDS > 0
                    and silence >= HEARTBEAT_SILENCE_SECONDS):
                notifier.notify_no_activity(silence / 3600, TARGET_WALLETS)
                last_signal_time = now  # reset so we don't spam

            # ── Claude autonomous trader ──────────────────────────────
            if (USE_CLAUDE_TRADER
                    and now - last_claude_trader_time >= CLAUDE_TRADER_INTERVAL):
                log.info("Running Claude autonomous trader scan...")
                trades = ct.run_claude_trader(
                    executor=executor,
                    state=state,
                    session=session,
                    your_bankroll=your_bankroll,
                    notifier=notifier,
                )
                log.info("Claude autonomous trader: %d trades placed", trades)
                last_claude_trader_time = now

            # ── Weekly report ─────────────────────────────────────────────
            if should_send_weekly_report(state):
                report = build_weekly_report(state)
                notifier.notify_weekly_report(report)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                state["last_weekly_report_date"] = today
                log.info("Weekly report sent: %s", report)

            st.save_state(state)
            consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            log.exception("Error in main loop (#%d): %s", consecutive_errors, e)
            if consecutive_errors >= ERROR_ALERT_THRESHOLD:
                notifier.notify_error(
                    f"{consecutive_errors} consecutive errors:\n{type(e).__name__}: {e}"
                )
                consecutive_errors = 0  # reset after alert

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
