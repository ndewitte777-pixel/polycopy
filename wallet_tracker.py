"""
Wallet Performance Tracker
===========================
Tracks hit rate and P&L for each of the 8 target wallets.
Uses this to:
1. Weight copy signal conviction by wallet track record
2. Skip wallets on cold streaks
3. Notify when a wallet is performing exceptionally well
"""

import logging
import time

log = logging.getLogger("polycopy.wallet_tracker")

# Minimum trades before we use performance data
MIN_TRADES_FOR_STATS = 5

# Cold streak threshold — skip wallet if win rate drops below this
MIN_WIN_RATE = 0.35  # 35% minimum win rate

# Hot streak — boost size if wallet is hot
HOT_WIN_RATE = 0.65  # 65%+ = hot wallet, boost conviction


def get_wallet_stats(state: dict, wallet: str) -> dict:
    """Get performance stats for a specific wallet."""
    wallet_data = state.get("wallet_stats", {}).get(wallet, {})
    trades = wallet_data.get("trades", 0)
    wins = wallet_data.get("wins", 0)
    pnl = wallet_data.get("pnl", 0.0)
    recent_trades = wallet_data.get("recent", [])  # last 10 outcomes

    win_rate = wins / trades if trades > 0 else 0.5
    recent_win_rate = (sum(1 for r in recent_trades[-10:] if r == "win") /
                       len(recent_trades[-10:])) if recent_trades else 0.5

    return {
        "trades": trades,
        "wins": wins,
        "pnl": pnl,
        "win_rate": win_rate,
        "recent_win_rate": recent_win_rate,
        "is_hot": recent_win_rate >= HOT_WIN_RATE and len(recent_trades) >= 3,
        "is_cold": recent_win_rate < MIN_WIN_RATE and len(recent_trades) >= MIN_TRADES_FOR_STATS,
        "has_data": trades >= MIN_TRADES_FOR_STATS,
    }


def should_copy_wallet(state: dict, wallet: str) -> tuple[bool, str]:
    """
    Returns (should_copy, reason).
    Used to skip wallets on cold streaks.
    """
    stats = get_wallet_stats(state, wallet)

    if not stats["has_data"]:
        return True, "insufficient data — copying"

    if stats["is_cold"]:
        return False, f"cold streak ({stats['recent_win_rate']:.0%} recent WR)"

    return True, f"{stats['win_rate']:.0%} WR over {stats['trades']} trades"


def get_conviction_multiplier(state: dict, wallet: str,
                               base_conviction: int) -> float:
    """
    Adjust position size based on wallet track record.
    Returns a multiplier (0.5 to 1.5).
    """
    stats = get_wallet_stats(state, wallet)

    if not stats["has_data"]:
        return 1.0  # no data, use base size

    if stats["is_hot"]:
        log.info("Wallet %s is HOT (%.0f%% recent WR) — boosting size",
                 wallet[:8], stats["recent_win_rate"] * 100)
        return 1.3

    if stats["is_cold"]:
        return 0.7  # reduce size on cold wallet

    # Linear scale between 0.7 and 1.3 based on win rate
    multiplier = 0.7 + (stats["recent_win_rate"] / 1.0) * 0.6
    return round(min(1.3, max(0.7, multiplier)), 2)


def record_trade_placed(state: dict, wallet: str, ticker: str,
                         price: float, size: float):
    """Record that we placed a trade following this wallet."""
    wallet_data = state.setdefault("wallet_stats", {}).setdefault(wallet, {
        "trades": 0, "wins": 0, "pnl": 0.0, "recent": []
    })
    wallet_data["trades"] = wallet_data.get("trades", 0) + 1
    wallet_data["last_trade"] = time.time()

    # Track pending outcome
    pending = state.setdefault("wallet_pending", {})
    pending[ticker] = {"wallet": wallet, "price": price, "size": size}


def record_trade_outcome(state: dict, ticker: str, pnl: float):
    """Record the outcome of a trade for the wallet that triggered it."""
    pending = state.get("wallet_pending", {})
    info = pending.pop(ticker, None)
    if not info:
        return

    wallet = info.get("wallet", "")
    if not wallet:
        return

    wallet_data = state.setdefault("wallet_stats", {}).setdefault(wallet, {
        "trades": 0, "wins": 0, "pnl": 0.0, "recent": []
    })

    wallet_data["pnl"] = wallet_data.get("pnl", 0.0) + pnl
    outcome = "win" if pnl > 0 else "loss"
    if pnl > 0:
        wallet_data["wins"] = wallet_data.get("wins", 0) + 1

    recent = wallet_data.setdefault("recent", [])
    recent.append(outcome)
    if len(recent) > 20:
        wallet_data["recent"] = recent[-20:]

    log.info("Wallet %s: %s $%.2f (WR: %d/%d)",
             wallet[:8], outcome.upper(), pnl,
             wallet_data["wins"], wallet_data["trades"])


def format_leaderboard(state: dict) -> str:
    """Format wallet leaderboard for weekly report."""
    wallet_data = state.get("wallet_stats", {})
    if not wallet_data:
        return "No wallet data yet."

    lines = ["🏆 WALLET LEADERBOARD"]
    entries = []
    for wallet, data in wallet_data.items():
        trades = data.get("trades", 0)
        if trades == 0:
            continue
        wins = data.get("wins", 0)
        pnl = data.get("pnl", 0.0)
        wr = wins / trades * 100
        entries.append((pnl, wallet, trades, wins, wr))

    for pnl, wallet, trades, wins, wr in sorted(entries, reverse=True):
        status = "🔥" if wr >= 65 else "❄️" if wr < 35 else "  "
        lines.append(f"{status} {wallet[:8]}... | {trades}T {wr:.0f}%WR ${pnl:+.2f}")

    return "\n".join(lines)
