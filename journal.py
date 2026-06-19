"""
Trade Journal
=============
Logs every signal, placement, and outcome to a persistent journal.
After a week of data, you can see:
- Which of the 8 wallets has the best hit rate
- Which market types are most profitable
- Copy vs rule trader performance
- Claude filter accuracy (what it skipped vs what would have been profitable)

Journal stored in state under "journal" key.
Weekly summary sent every Sunday.
"""

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("polycopy.journal")


def record_signal(state: dict, signal: dict):
    """
    Record a copy/rule signal with all context.
    signal = {
        type: "copy" | "rule",
        source: wallet address or "rule_trader",
        market: question string,
        ticker: Kalshi ticker,
        side: "YES" | "NO",
        price: float,
        size: float,
        action: "placed" | "skipped" | "filtered" | "no_match",
        skip_reason: str (if skipped),
        confidence: int (if filtered),
        timestamp: float,
    }
    """
    journal = state.setdefault("journal", [])
    entry = {
        **signal,
        "timestamp": signal.get("timestamp", time.time()),
        "outcome": None,   # filled in when position closes
        "pnl": None,
        "resolved": False,
    }
    journal.append(entry)

    # Keep only last 500 entries to avoid state bloat
    if len(journal) > 500:
        state["journal"] = journal[-500:]

    log.debug("Journal: recorded %s signal for %s",
              signal.get("action"), signal.get("market", "?")[:40])


def record_outcome(state: dict, ticker: str, pnl: float, exit_price: float):
    """Update journal entries for a ticker when position closes."""
    journal = state.get("journal", [])
    updated = 0
    for entry in journal:
        if entry.get("ticker") == ticker and not entry.get("resolved"):
            entry["outcome"] = "win" if pnl > 0 else "loss"
            entry["pnl"] = pnl
            entry["exit_price"] = exit_price
            entry["resolved"] = True
            entry["hold_time_mins"] = (time.time() - entry.get("timestamp", time.time())) / 60
            updated += 1
    if updated:
        log.info("Journal: updated %d entries for %s (pnl=$%.2f)", updated, ticker, pnl)


def get_summary(state: dict) -> dict:
    """Generate performance summary from journal."""
    journal = state.get("journal", [])
    if not journal:
        return {"total": 0}

    placed = [e for e in journal if e.get("action") == "placed"]
    resolved = [e for e in placed if e.get("resolved")]
    skipped = [e for e in journal if e.get("action") in ("skipped", "filtered")]

    wins = [e for e in resolved if e.get("outcome") == "win"]
    losses = [e for e in resolved if e.get("outcome") == "loss"]

    total_pnl = sum(e.get("pnl", 0) for e in resolved)
    win_rate = len(wins) / len(resolved) * 100 if resolved else 0

    # Per-wallet performance
    wallet_stats = {}
    for entry in resolved:
        source = entry.get("source", "unknown")
        if source not in wallet_stats:
            wallet_stats[source] = {"trades": 0, "wins": 0, "pnl": 0.0}
        wallet_stats[source]["trades"] += 1
        wallet_stats[source]["pnl"] += entry.get("pnl", 0)
        if entry.get("outcome") == "win":
            wallet_stats[source]["wins"] += 1

    # Best/worst wallet
    best_wallet = max(wallet_stats.items(),
                      key=lambda x: x[1]["pnl"], default=(None, {}))[0] if wallet_stats else None

    # Copy vs rule trader
    copy_resolved = [e for e in resolved if e.get("type") == "copy"]
    rule_resolved = [e for e in resolved if e.get("type") == "rule"]
    copy_pnl = sum(e.get("pnl", 0) for e in copy_resolved)
    rule_pnl = sum(e.get("pnl", 0) for e in rule_resolved)

    # Claude filter accuracy — what it skipped that resolved as win/loss
    filtered = [e for e in journal if e.get("action") == "filtered" and e.get("resolved")]
    filter_would_have_won = [e for e in filtered if e.get("outcome") == "win"]

    return {
        "total_signals": len(journal),
        "placed": len(placed),
        "resolved": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "skipped": len(skipped),
        "copy_pnl": copy_pnl,
        "rule_pnl": rule_pnl,
        "best_wallet": best_wallet,
        "wallet_stats": wallet_stats,
        "filter_skip_count": len([e for e in journal if e.get("action") == "filtered"]),
        "filter_would_have_won": len(filter_would_have_won),
    }


def format_weekly_report(state: dict) -> str:
    """Format a human-readable weekly performance report."""
    s = get_summary(state)
    if s.get("total", 0) == 0 and s.get("total_signals", 0) == 0:
        return "No trades recorded yet."

    lines = [
        "📊 WEEKLY PERFORMANCE REPORT",
        "=" * 35,
        f"Total signals: {s.get('total_signals', 0)}",
        f"Trades placed: {s.get('placed', 0)}",
        f"Resolved: {s.get('resolved', 0)} ({s.get('wins', 0)}W / {s.get('losses', 0)}L)",
        f"Win rate: {s.get('win_rate', 0):.1f}%",
        f"Total P&L: ${s.get('total_pnl', 0):+.2f}",
        "",
        "BY SOURCE:",
        f"  Copy trading: ${s.get('copy_pnl', 0):+.2f}",
        f"  Rule trader:  ${s.get('rule_pnl', 0):+.2f}",
        "",
        f"Signals skipped: {s.get('skipped', 0)}",
        f"Claude filtered: {s.get('filter_skip_count', 0)}",
        f"  (would have won: {s.get('filter_would_have_won', 0)})",
        "",
    ]

    wallet_stats = s.get("wallet_stats", {})
    if wallet_stats:
        lines.append("WALLET PERFORMANCE:")
        for wallet, stats in sorted(wallet_stats.items(),
                                    key=lambda x: x[1]["pnl"], reverse=True):
            wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
            short_wallet = wallet[:8] + "..." if len(wallet) > 8 else wallet
            lines.append(
                f"  {short_wallet}: {stats['trades']}T "
                f"{wr:.0f}% WR ${stats['pnl']:+.2f}"
            )

    return "\n".join(lines)
