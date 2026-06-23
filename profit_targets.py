"""
Daily Profit Target Manager
============================
Smarter profit target logic:

- Target is a FLOOR, not a ceiling — hitting $5 doesn't stop trading
- Bot keeps going as long as it's making money
- Only locks when:
  1. You've hit 2x the daily target AND given back >30% of peak profit
  2. OR it's after 8pm UTC (end of day) and you've hit the target
  3. OR daily loss limit hit

Phases:
- HUNTING    : below target, trade normally
- BUILDING   : hit target, keep trading aggressively (target was the floor)
- PROTECTING : hit 2x target OR late in day, be more conservative
- LOCKED     : gave back too much after big gains, stop
- LOSS_LIMIT : daily loss limit hit, stop
"""

import logging
from datetime import datetime, timezone

from config import (
    WEEKDAY_PROFIT_TARGET,
    WEEKEND_PROFIT_TARGET,
    CONSERVATIVE_MODE_AFTER_TARGET,
    PROFIT_PROTECTION_PCT,
    MAX_DAILY_LOSS_USDC,
)
import state as st

log = logging.getLogger("polycopy.targets")

# How much bigger than the target before we start being more careful
TARGET_MULTIPLIER_FOR_PROTECTING = 2.0   # e.g. $10 on a $5 day → protecting
# How late in the day (UTC hour) before we switch to protecting mode if target hit
END_OF_DAY_HOUR_UTC = 22  # 10pm UTC = ~6pm EST
# How much of peak profit to give back before locking (only applies in PROTECTING)
GIVEBACK_TO_LOCK_PCT = 30.0


def get_daily_target() -> float:
    day = datetime.now(timezone.utc).weekday()
    return WEEKEND_PROFIT_TARGET if day >= 5 else WEEKDAY_PROFIT_TARGET


def get_phase(state: dict) -> str:
    st.reset_daily_if_needed(state)

    daily_loss = state.get("daily_loss", 0.0)
    daily_profit = state.get("daily_profit", 0.0)
    peak_profit = state.get("peak_daily_profit", 0.0)
    net = st.net_daily_pnl(state)
    target = get_daily_target()
    now_hour = datetime.now(timezone.utc).hour

    # Always stop on loss limit
    if daily_loss >= MAX_DAILY_LOSS_USDC:
        return "LOSS_LIMIT"

    # Below target — keep hunting
    if peak_profit < target:
        return "HUNTING"

    # Hit target — keep BUILDING (don't stop just because we hit $5)
    big_target = target * TARGET_MULTIPLIER_FOR_PROTECTING
    is_late = now_hour >= END_OF_DAY_HOUR_UTC

    # Only switch to PROTECTING if we've made 2x the target OR it's late
    if peak_profit >= big_target or is_late:
        # Check if we've given back too much
        if peak_profit > 0:
            giveback_pct = ((peak_profit - net) / peak_profit) * 100
            if giveback_pct >= GIVEBACK_TO_LOCK_PCT:
                return "LOCKED"
        return "PROTECTING"

    # Between target and 2x target, and not late — keep going!
    return "BUILDING"


def get_size_multiplier(phase: str) -> float:
    return {
        "HUNTING":    1.0,
        "BUILDING":   1.2,   # slightly bigger sizes when on a roll
        "PROTECTING": 0.6,   # pull back a bit when protecting big gains
        "LOCKED":     0.0,
        "LOSS_LIMIT": 0.0,
    }.get(phase, 1.0)


def get_min_confidence(phase: str, base: int = 60) -> int:
    return {
        "HUNTING":    base,
        "BUILDING":   base,   # same bar — keep taking good trades
        "PROTECTING": 75,     # higher bar when protecting gains
        "LOCKED":     100,
        "LOSS_LIMIT": 100,
    }.get(phase, base)


def should_trade(phase: str) -> bool:
    return phase in ("HUNTING", "BUILDING", "PROTECTING")


def max_risk_this_trade(state: dict, default_size: float) -> float:
    """
    House-money rule: once the daily profit target is reached, the bot
    locks the target amount and may only risk profit EARNED ABOVE it.

    Example: target is $5. Bot has made $6.50 net today.
    - $5.00 is locked (protected, never risked)
    - $1.50 overflow can be bet
    - So this trade is capped at min(default_size, $1.50)

    Below target: normal sizing (the daily-loss limit is the protection).
    Above target with no overflow left: returns 0 (stop).
    """
    target = get_daily_target()
    net = st.net_daily_pnl(state)

    # Haven't hit the goal yet — trade normally, daily-loss limit protects us
    if net < target:
        return default_size

    # Goal reached — only the overflow above target is riskable
    overflow = net - target
    if overflow <= 0:
        return 0.0  # exactly at goal, nothing extra to risk — stop for the day

    # Risk at most the overflow, never more than the normal size
    return min(default_size, overflow)


def status_line(state: dict) -> str:
    st.reset_daily_if_needed(state)
    target = get_daily_target()
    net = st.net_daily_pnl(state)
    peak = state.get("peak_daily_profit", 0.0)
    phase = get_phase(state)
    day = datetime.now(timezone.utc).weekday()
    day_type = "Weekend" if day >= 5 else "Weekday"
    big_target = target * TARGET_MULTIPLIER_FOR_PROTECTING
    trades = state.get("daily_trades", 0)
    at_risk = state.get("total_at_risk", 0.0)
    return (
        f"[{phase}] {day_type} | Target: ${target:.2f} | Net: ${net:+.2f} | "
        f"Trades: {trades}/10 | At risk: ${at_risk:.2f} | "
        f"Profit: ${state.get('daily_profit', 0):.2f} | Loss: ${state.get('daily_loss', 0):.2f}"
    )


def notify_milestone(state: dict, notifier, previous_phase: str):
    phase = get_phase(state)
    if phase == previous_phase:
        return

    target = get_daily_target()
    net = st.net_daily_pnl(state)
    peak = state.get("peak_daily_profit", 0.0)
    big_target = target * TARGET_MULTIPLIER_FOR_PROTECTING

    if phase == "BUILDING" and previous_phase == "HUNTING":
        notifier.send(
            title=f"✅ Target hit! ${net:+.2f} — keep going!",
            message=(
                f"Daily target of ${target:.2f} reached!\n"
                f"Continuing to trade — next milestone: ${big_target:.2f}\n"
                f"Sizes bumped up slightly. Let's build on this."
            ),
        )
    elif phase == "PROTECTING" and previous_phase in ("HUNTING", "BUILDING"):
        notifier.send(
            title=f"🛡️ Big day! ${net:+.2f} — protecting gains",
            message=(
                f"Hit ${peak:.2f} today (target was ${target:.2f})!\n"
                f"Switching to conservative mode — "
                f"will lock if we give back {GIVEBACK_TO_LOCK_PCT:.0f}% of peak.\n"
                f"Still trading, just more selective."
            ),
        )
    elif phase == "LOCKED":
        notifier.send(
            title="🔒 Gains protected — done for today",
            message=(
                f"Gave back {GIVEBACK_TO_LOCK_PCT:.0f}% of peak profit.\n"
                f"Net P&L: ${net:+.2f} | Peak was: ${peak:.2f}\n"
                f"Stopping to lock in today's gains."
            ),
            priority=1,
        )
    elif phase == "LOSS_LIMIT":
        notifier.send(
            title="🛑 Daily loss limit — stopping",
            message=(
                f"Daily loss limit of ${MAX_DAILY_LOSS_USDC:.2f} reached.\n"
                f"Net P&L: ${net:+.2f} | Resuming tomorrow."
            ),
            priority=1,
        )
