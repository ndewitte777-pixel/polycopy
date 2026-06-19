"""
Live Scalping Engine
====================
Monitors open positions during live sports events and decides whether to:
1. SCALP — exit quickly at small profit (game is risky/uncertain)
2. RIDE  — hold the position (Claude is confident team will win)
3. HOLD  — not enough movement yet, keep monitoring

Claude makes the ride/scalp decision when price has moved enough to
consider exiting, using live game context (score, clock, momentum).
"""

import time
import logging
import requests
import json

from config import (
    CLOB_API_URL,
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    DRY_RUN,
)

log = logging.getLogger("polycopy.scalper")

SCALP_MIN_CENTS     = 0.15   # fallback minimum cents move
SCALP_PROFIT_PCT    = 25.0   # % gain on price that triggers ride/scalp decision
SCALP_MAX_HOLD_MINS = 120    # hard force-exit after this long
HARD_STOP_PCT       = -30.0  # stop loss at -30%

# Target profit as % of the original bet size (not price move %)
# e.g. $1 bet → exit consideration at $0.25 profit (25%)
# e.g. $2 bet → exit consideration at $0.50 profit (25%)
SCALP_TARGET_RETURN_PCT = 25.0  # 25% return on bet size
MIN_PROFIT_DOLLARS  = 0.20      # never exit for less than this regardless

RIDE_SYSTEM_PROMPT = """You are an expert live sports prediction market trader.

A position is currently profitable. You must decide: SCALP (sell now) or RIDE (hold for bigger gain).

Consider:
- Current game score and time remaining
- How dominant is the leading team?
- Is there realistic risk of a comeback?
- How much profit is already locked in vs how much more is realistically available?
- Is the market price already close to 0.90+ (most value already captured)?

Respond ONLY with valid JSON:
{
  "decision": "SCALP" or "RIDE",
  "confidence": <integer 0-100>,
  "reason": "<one sentence>",
  "ride_target_price": <float, if RIDE: what price to sell at later, e.g. 0.85>
}

Guidelines:
- RIDE if: team is clearly dominant, significant time left, market price still has room to grow
- SCALP if: game is close, late in match, comeback is realistic, or market already near ceiling
- If confidence < 60 on RIDE, default to SCALP — protect the profit"""


def get_current_price(token_id: str, session: requests.Session) -> float:
    """Fetch current price for a Kalshi market ticker."""
    try:
        from kalshi_data import get_market_price
        yes_price, _ = get_market_price(token_id)
        return yes_price
    except Exception as e:
        log.warning("Price fetch failed for %s: %s", token_id[:12], e)
        return 0.0


def ask_claude_ride_or_scalp(lot: dict, current_price: float,
                              game_context: str) -> dict:
    """Ask Claude whether to ride or scalp a profitable position."""
    if not ANTHROPIC_API_KEY:
        return {"decision": "SCALP", "confidence": 100,
                "reason": "No API key — defaulting to scalp", "ride_target_price": current_price}

    entry_price = lot.get("entry_price", current_price)
    size_usdc = lot.get("size_usdc", 0)
    market_info = lot.get("market_info", {})
    question = market_info.get("question", "Unknown")
    outcome = market_info.get("outcome", "?")
    end_date = market_info.get("end_date", "")

    pct_gain = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
    token_qty = size_usdc / entry_price if entry_price else 0
    unrealized_pnl = (token_qty * current_price) - size_usdc

    prompt = f"""POSITION STATUS:
Market: {question}
Our bet: {outcome}
Entry price: {entry_price:.3f} | Current price: {current_price:.3f}
Gain: +{pct_gain:.1f}% (${unrealized_pnl:+.2f} unrealized)
Position size: ${size_usdc:.2f}
Market closes: {end_date or 'unknown'}

{f'LIVE GAME DATA:{chr(10)}{game_context}' if game_context else 'No live game data available.'}

Should we SCALP now (take the {pct_gain:.1f}% gain) or RIDE for more?
Respond with JSON only."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 200,
                "system": RIDE_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        decision = json.loads(raw.strip())
        decision.setdefault("decision", "SCALP")
        decision.setdefault("confidence", 50)
        decision.setdefault("reason", "")
        decision.setdefault("ride_target_price", current_price * 1.1)

        log.info(
            "Claude ride/scalp: %s (conf=%d, target=%.3f) | %s",
            decision["decision"], decision["confidence"],
            decision.get("ride_target_price", 0), decision["reason"],
        )
        return decision

    except Exception as e:
        log.warning("Claude ride/scalp failed (%s) — defaulting to SCALP", e)
        return {"decision": "SCALP", "confidence": 100,
                "reason": f"API error: {e}", "ride_target_price": current_price}


def should_exit(lot: dict, current_price: float, game_context: str) -> tuple[bool, str]:
    """
    Decide whether to exit now.
    Returns (should_sell, reason).

    Logic:
    1. Hard stop loss — always exit
    2. Force exit on max hold time
    3. If already riding, check ride target
    4. If price moved enough → ask Claude ride vs scalp
    """
    entry_price = lot.get("entry_price", 0)
    if entry_price <= 0 or current_price <= 0:
        return False, ""

    price_change = current_price - entry_price
    pct_change = (price_change / entry_price) * 100
    size_usdc = lot.get("size_usdc", 1.0)

    # Actual dollar profit: (price move) × (contracts held)
    # contracts = size_usdc / entry_price
    contracts = size_usdc / entry_price if entry_price > 0 else 1
    dollar_profit = price_change * contracts

    # 25% return target on original bet size
    # $1 bet → need $0.25 profit; $2 bet → need $0.50 profit
    profit_target = size_usdc * (SCALP_TARGET_RETURN_PCT / 100)

    # 1. Hard stop loss
    if pct_change <= HARD_STOP_PCT:
        return True, f"Hard stop loss: {pct_change:.1f}% | ${dollar_profit:.2f}"

    # 2. Near resolution (>0.88) — just hold for full payout
    if current_price >= 0.88:
        return False, "Near resolution — holding to collect full payout"

    # 3. Force exit after max hold time while profitable
    opened_at = lot.get("opened_at", time.time())
    mins_held = (time.time() - opened_at) / 60
    if mins_held > SCALP_MAX_HOLD_MINS and dollar_profit > 0:
        return True, f"Max hold ({SCALP_MAX_HOLD_MINS}min) | ${dollar_profit:.2f} profit"

    # 4. Already riding — check if hit ride target
    ride_target = lot.get("ride_target_price")
    if ride_target and lot.get("riding"):
        if current_price >= ride_target:
            return True, f"🏇 Ride target hit: {current_price:.3f} | ${dollar_profit:.2f}"
        return False, ""

    # 5. Haven't hit 25% return yet — don't exit
    if dollar_profit < profit_target:
        return False, ""

    # 6. Hit 25% return — ask Claude: scalp now or ride for more?
    if dollar_profit >= profit_target:
        decision = ask_claude_ride_or_scalp(lot, current_price, game_context)

        if decision["decision"] == "RIDE" and decision["confidence"] >= 60:
            lot["riding"] = True
            lot["ride_target_price"] = decision.get("ride_target_price",
                                                     current_price * 1.20)
            lot["ride_reason"] = decision["reason"]
            log.info(
                "RIDING | target=%.3f | return=%.0f%% ($%.2f/$%.2f) | %s",
                lot["ride_target_price"],
                (dollar_profit / size_usdc) * 100,
                dollar_profit,
                size_usdc,
                decision["reason"],
            )
            return False, ""
        else:
            return True, (
                f"Taking {(dollar_profit/size_usdc)*100:.0f}% return "
                f"(${dollar_profit:.2f} on ${size_usdc:.2f}) | {decision['reason']}"
            )

    return False, ""


def run_scalper(open_lots: dict, executor, state: dict,
                session: requests.Session, live_games: list,
                notifier) -> int:
    """Called from bot.py every LIVE_POLL_INTERVAL seconds."""
    if not open_lots:
        return 0

    exits = 0

    for token_id, lots in list(open_lots.items()):
        if not lots:
            continue

        # Skip and remove stale Polymarket token IDs
        if (token_id.isdigit() and len(token_id) > 20) or \
           (not token_id.upper().startswith("KX") and len(token_id) > 15):
            log.info("Removing stale lot: %s", token_id[:20])
            open_lots.pop(token_id, None)
            continue

        current_price = get_current_price(token_id, session)
        if current_price <= 0:
            continue

        remaining_lots = []
        for lot in lots:
            source = lot.get("source", "")
            entry_price = lot.get("entry_price", 0)
            market_info = lot.get("market_info", {})
            question = market_info.get("question", "")
            if not entry_price:
                remaining_lots.append(lot)
                continue

            pct_change = (current_price - entry_price) / entry_price * 100

            # Match live game context
            game_context = ""
            best_score = 0.0
            for game in live_games:
                from sports_data import match_game_to_market, format_game_context
                score = match_game_to_market(game, question)
                if score > best_score:
                    best_score = score
                    if score >= 0.4:
                        game_context = format_game_context(game)

            # Exit logic — copy trades use simple stop/take-profit
            if source in ("kalshi_copy", "copy"):
                if pct_change >= 40:
                    sell, reason = True, f"Take profit: +{pct_change:.1f}%"
                elif pct_change <= -30:
                    sell, reason = True, f"Stop loss: {pct_change:.1f}%"
                elif (time.time() - lot.get("opened_at", time.time())) > 7200 and abs(pct_change) < 5:
                    sell, reason = True, "Time stop: 2hrs held, no movement"
                else:
                    sell, reason = should_exit(lot, current_price, game_context)
            else:
                sell, reason = should_exit(lot, current_price, game_context)

            if sell:
                entry_price = lot["entry_price"]
                size_usdc = lot["size_usdc"]
                token_qty = size_usdc / entry_price if entry_price else 0
                pnl_usdc = (token_qty * current_price) - size_usdc
                was_riding = lot.get("riding", False)

                log.info(
                    "%s | %s | entry=%.3f exit=%.3f pnl=$%.3f | %s",
                    "RIDE→SELL" if was_riding else "SCALP",
                    question[:60], entry_price, current_price, pnl_usdc, reason,
                )

                executor.place_order(
                    token_id=token_id, side="SELL",
                    price=current_price, size_usdc=size_usdc,
                )

                if pnl_usdc < 0:
                    state["daily_loss"] = state.get("daily_loss", 0.0) + abs(pnl_usdc)

                import state as _st
                _st.record_pnl(state, pnl_usdc)

                # Record outcome in journal and wallet tracker
                import journal as _jnl
                import wallet_tracker as _wt
                _jnl.record_outcome(state, token_id, pnl_usdc, current_price)
                _wt.record_trade_outcome(state, token_id, pnl_usdc)

                # Reduce at-risk tracking
                state["total_at_risk"] = max(
                    0, state.get("total_at_risk", 0) - lot.get("size_usdc", 0)
                )

                result = "WIN" if pnl_usdc > 0 else "LOSS"
                emoji = "✅" if pnl_usdc > 0 else "❌"
                mode = "🏇 Ride complete" if was_riding else "⚡ Scalp"
                ride_info = (
                    f"\nRode to target {lot.get('ride_target_price', '?'):.3f}"
                    if was_riding else ""
                )

                notifier.send(
                    title=f"{mode} {result} {emoji} | ${pnl_usdc:+.3f}",
                    message=(
                        f"{question}\n\n"
                        f"Entry: {entry_price:.3f} → Exit: {current_price:.3f}\n"
                        f"P&L: ${pnl_usdc:+.3f} | Size: ${size_usdc:.2f}\n"
                        f"Reason: {reason}{ride_info}"
                        f"{chr(10) + game_context if game_context else ''}"
                    ),
                )
                exits += 1
                state["open_positions"] = max(0, state.get("open_positions", 0) - 1)
            else:
                remaining_lots.append(lot)

        if remaining_lots:
            open_lots[token_id] = remaining_lots
        else:
            open_lots.pop(token_id, None)

    return exits
