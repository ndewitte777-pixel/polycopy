"""
Live Scalping Engine
====================
Monitors open positions during live sports events and executes quick
profit-taking exits when price moves favorably, rather than holding
to market resolution.

Strategy:
- During a live game, prices move fast on scoring events
- If we're up X% on a position AND the game situation is risky
  (close score, late in game, momentum shifting), sell immediately
- This is better than holding and risking a reversal

Scalp triggers (any one sufficient to sell):
1. Price up SCALP_PROFIT_PCT% from entry (default 5 cents / ~15-25%)
2. Price up SCALP_MIN_CENTS from entry in absolute terms (default $0.05)
3. Game situation turns risky (opponent scores, momentum shifts)
4. Market liquidity drops significantly (sign of informed selling)

Unlike the position_monitor (which runs every 60s), the scalper runs
every LIVE_POLL_INTERVAL seconds (default 20s) for live game lots.
"""

import time
import logging
import requests

from config import (
    CLOB_API_URL,
    DRY_RUN,
)

log = logging.getLogger("polycopy.scalper")

# ── Scalping config (can be moved to config.py if desired) ──────────────────
SCALP_PROFIT_PCT   = 15.0   # sell if up this % from entry
SCALP_MIN_CENTS    = 0.05   # sell if price moved up at least this much (absolute)
SCALP_MAX_HOLD_MINUTES = 45 # force-sell any live position after this many minutes
RISK_SCORE_THRESHOLD = 0.65 # sell if game risk score exceeds this


def get_current_price(token_id: str, session: requests.Session) -> float:
    """Fetch current midpoint price for a token."""
    try:
        r = session.get(
            f"{CLOB_API_URL}/midpoint",
            params={"token_id": token_id},
            timeout=8,
        )
        r.raise_for_status()
        return float(r.json().get("mid", 0) or 0)
    except Exception as e:
        log.warning("Price fetch failed for %s: %s", token_id[:12], e)
        return 0.0


def compute_game_risk(game: dict | None, lot: dict) -> float:
    """
    Returns a risk score 0-1 for a live lot given current game state.
    Higher = more reason to exit quickly.
    0.0 = safe to hold | 1.0 = exit immediately
    """
    if not game:
        return 0.0

    risk = 0.0
    teams = game.get("teams", [])
    period = game.get("period", 0)
    clock = game.get("clock", "")
    sport = game.get("sport", "soccer")
    outcome = lot.get("market_info", {}).get("outcome", "").lower()

    # Parse scores
    scores = {}
    for t in teams:
        scores[t.get("name", "").lower()] = int(t.get("score", 0) or 0)

    score_values = list(scores.values())
    if len(score_values) >= 2:
        score_diff = abs(score_values[0] - score_values[1])
        # Close game = higher risk
        if score_diff == 0:
            risk += 0.3  # tied = uncertain
        elif score_diff == 1:
            risk += 0.1  # one goal/point lead

    # Late in game = higher stakes
    if sport in ("soccer", "world_cup"):
        # Soccer: 2 halves of 45 min each
        if period >= 2:
            risk += 0.2
        # Try to parse clock minutes
        try:
            minutes = int(clock.replace("'", "").split(":")[0])
            if minutes > 75:
                risk += 0.25  # last 15 min
            elif minutes > 60:
                risk += 0.1
        except Exception:
            pass
    elif sport == "nba":
        if period >= 4:
            risk += 0.3
        try:
            parts = clock.split(":")
            mins = int(parts[0])
            if mins < 3:
                risk += 0.25
        except Exception:
            pass
    elif sport in ("nfl",):
        if period >= 4:
            risk += 0.35

    # Time held — long live positions get riskier
    opened_at = lot.get("opened_at", time.time())
    minutes_held = (time.time() - opened_at) / 60
    if minutes_held > SCALP_MAX_HOLD_MINUTES:
        risk += 0.5  # force exit if held too long

    return min(risk, 1.0)


def should_scalp(lot: dict, current_price: float, game: dict | None) -> tuple[bool, str]:
    """
    Decide whether to scalp (quick-sell) a position.
    Returns (should_sell, reason).
    """
    entry_price = lot.get("entry_price", 0)
    if entry_price <= 0 or current_price <= 0:
        return False, ""

    price_change = current_price - entry_price
    pct_change = (price_change / entry_price) * 100

    # 1. Hit profit target in cents
    if price_change >= SCALP_MIN_CENTS:
        return True, f"Scalp target hit: +${price_change:.3f} (+{pct_change:.1f}%)"

    # 2. Hit profit % target
    if pct_change >= SCALP_PROFIT_PCT:
        return True, f"Scalp profit %: +{pct_change:.1f}% from entry"

    # 3. Game risk too high while in profit
    if game and pct_change > 0:
        risk = compute_game_risk(game, lot)
        if risk >= RISK_SCORE_THRESHOLD:
            return True, (
                f"High game risk ({risk:.2f}) while profitable "
                f"(+{pct_change:.1f}%) — locking in gains"
            )

    # 4. Force exit on very long live hold
    opened_at = lot.get("opened_at", time.time())
    minutes_held = (time.time() - opened_at) / 60
    if minutes_held > SCALP_MAX_HOLD_MINUTES and pct_change > 0:
        return True, f"Max hold time ({SCALP_MAX_HOLD_MINUTES}min) reached while profitable"

    return False, ""


def run_scalper(open_lots: dict, executor, state: dict,
                session: requests.Session, live_games: list,
                notifier) -> int:
    """
    Main entry — called from bot.py on each live poll interval.
    Checks all open lots marked as live-game or source=claude_autonomous
    for scalping opportunities.
    Returns number of scalps executed.
    """
    if not open_lots:
        return 0

    scalps = 0

    for token_id, lots in list(open_lots.items()):
        if not lots:
            continue

        current_price = get_current_price(token_id, session)
        if current_price <= 0:
            continue

        remaining_lots = []
        for lot in lots:
            market_info = lot.get("market_info", {})
            question = market_info.get("question", "")

            # Find matching live game for this lot
            matched_game = None
            best_score = 0.0
            for game in live_games:
                from sports_data import match_game_to_market
                score = match_game_to_market(game, question)
                if score > best_score:
                    best_score = score
                    matched_game = game

            # Only use game context if it's a strong match
            game_context = matched_game if best_score >= 0.4 else None

            sell, reason = should_scalp(lot, current_price, game_context)

            if sell:
                entry_price = lot["entry_price"]
                size_usdc = lot["size_usdc"]
                token_qty = size_usdc / entry_price if entry_price else 0
                exit_value = token_qty * current_price
                pnl_usdc = exit_value - size_usdc

                log.info(
                    "SCALP EXIT | %s | entry=%.3f exit=%.3f pnl=$%.3f | %s",
                    question[:60], entry_price, current_price, pnl_usdc, reason,
                )

                executor.place_order(
                    token_id=token_id,
                    side="SELL",
                    price=current_price,
                    size_usdc=size_usdc,
                )

                if pnl_usdc < 0:
                    state["daily_loss"] = state.get("daily_loss", 0.0) + abs(pnl_usdc)

                result = "WIN" if pnl_usdc > 0 else "LOSS"
                emoji = "✅" if pnl_usdc > 0 else "❌"
                game_str = ""
                if game_context:
                    from sports_data import format_game_context
                    game_str = "\n\n" + format_game_context(game_context)

                notifier.send(
                    title=f"⚡ Scalp {result} {emoji} | ${pnl_usdc:+.3f}",
                    message=(
                        f"{question}\n\n"
                        f"Entry: {entry_price:.3f} → Exit: {current_price:.3f}\n"
                        f"P&L: ${pnl_usdc:+.3f} | Size: ${size_usdc:.2f}\n"
                        f"Reason: {reason}"
                        f"{game_str}"
                    ),
                )
                scalps += 1
                state["open_positions"] = max(0, state.get("open_positions", 0) - 1)
                # Don't keep this lot
            else:
                remaining_lots.append(lot)

        if remaining_lots:
            open_lots[token_id] = remaining_lots
        else:
            open_lots.pop(token_id, None)

    return scalps
