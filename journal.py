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

    # Per-SPORT breakdown — which sports are actually winning
    sport_stats = {}
    for entry in resolved:
        sp = entry.get("sport", "unknown")
        if sp not in sport_stats:
            sport_stats[sp] = {"trades": 0, "wins": 0, "pnl": 0.0}
        sport_stats[sp]["trades"] += 1
        sport_stats[sp]["pnl"] += entry.get("pnl", 0)
        if entry.get("outcome") == "win":
            sport_stats[sp]["wins"] += 1

    # Per-MARKET-TYPE breakdown — are WIN bets good but TOTAL/SPREAD/PROP bad?
    mtype_stats = {}
    for entry in resolved:
        mt = entry.get("market_type", "unknown")
        if mt not in mtype_stats:
            mtype_stats[mt] = {"trades": 0, "wins": 0, "pnl": 0.0}
        mtype_stats[mt]["trades"] += 1
        mtype_stats[mt]["pnl"] += entry.get("pnl", 0)
        if entry.get("outcome") == "win":
            mtype_stats[mt]["wins"] += 1

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
        "sport_stats": sport_stats,
        "mtype_stats": mtype_stats,
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

    # Per-sport breakdown — the key learning for the rule trader
    sport_stats = s.get("sport_stats", {})
    if sport_stats:
        lines.append("")
        lines.append("BY SPORT:")
        for sp, st in sorted(sport_stats.items(),
                             key=lambda x: x[1]["pnl"], reverse=True):
            wr = st["wins"] / st["trades"] * 100 if st["trades"] else 0
            lines.append(f"  {sp}: {st['trades']}T {wr:.0f}% WR ${st['pnl']:+.2f}")

    # Per-market-type breakdown — WIN vs TOTAL vs SPREAD vs PROP
    mtype_stats = s.get("mtype_stats", {})
    if mtype_stats:
        lines.append("")
        lines.append("BY MARKET TYPE:")
        for mt, st in sorted(mtype_stats.items(),
                             key=lambda x: x[1]["pnl"], reverse=True):
            wr = st["wins"] / st["trades"] * 100 if st["trades"] else 0
            lines.append(f"  {mt}: {st['trades']}T {wr:.0f}% WR ${st['pnl']:+.2f}")

    return "\n".join(lines)


def settle_finished_games(state: dict, live_games: list, executor=None):
    """
    DRY-RUN SETTLEMENT: when a game finishes, resolve any open paper trades
    on that game as win/loss based on the final result. This is what makes
    the dry run produce a real win/loss record instead of just open entries.

    For each finished game, look at our journaled 'placed' rule trades that
    haven't resolved, determine if the bet won, and record the outcome.
    """
    if not live_games:
        return 0

    journal = state.get("journal", [])
    if not journal:
        return 0

    # Build a map of finished games → final scores
    finished = {}
    for g in live_games:
        if g.get("is_finished") or g.get("status", "").lower() in ("final", "post", "finished"):
            teams = g.get("teams", [])
            if len(teams) >= 2:
                try:
                    s0 = float(teams[0].get("score", 0) or 0)
                    s1 = float(teams[1].get("score", 0) or 0)
                except (ValueError, TypeError):
                    continue
                gid = g.get("game_id") or g.get("short_name", "")
                finished[gid] = {
                    "scores": [s0, s1],
                    "teams": teams,
                    "total": s0 + s1,
                    "short_name": g.get("short_name", ""),
                }

    if not finished:
        return 0

    settled = 0
    for entry in journal:
        if entry.get("resolved") or entry.get("action") != "placed":
            continue
        if entry.get("type") != "rule":
            continue

        # Match the journal entry to a finished game by short_name
        game_name = entry.get("game", "")
        match = None
        for gid, info in finished.items():
            if info["short_name"] == game_name:
                match = info
                break
        if not match:
            continue

        # Determine if the bet won based on market type and final result
        outcome = _judge_outcome(entry, match)
        if outcome is None:
            continue  # can't determine — leave open

        # Paper P&L: win → profit at the entry payout; loss → lose the stake
        price = entry.get("price", 0.5)
        size = entry.get("size", 1.0)
        if outcome == "win":
            pnl = size * ((1 - price) / price) if price > 0 else 0
        else:
            pnl = -size

        entry["outcome"] = outcome
        entry["pnl"] = round(pnl, 2)
        entry["resolved"] = True
        settled += 1
        log.info("Settled paper trade: %s %s on %s → %s ($%+.2f)",
                 entry.get("side"), entry.get("market_type"),
                 game_name, outcome, pnl)

    return settled


def _judge_outcome(entry: dict, game_info: dict):
    """
    Decide if a journaled bet won given the final game result.
    Returns "win", "loss", or None if undeterminable.
    """
    mtype = entry.get("market_type", "")
    side = entry.get("side", "")
    scores = game_info["scores"]
    total = game_info["total"]
    ticker = entry.get("ticker", "")

    if mtype == "WIN":
        # YES = the team in the ticker suffix won. Determine winner.
        winner_idx = 0 if scores[0] > scores[1] else 1
        teams = game_info["teams"]
        winner_abbr = (teams[winner_idx].get("abbreviation", "") or "").upper()
        # ticker suffix is the YES team
        suffix = ticker.split("-")[-1].upper() if "-" in ticker else ""
        yes_won = suffix and suffix in winner_abbr or winner_abbr in suffix
        if side == "YES":
            return "win" if yes_won else "loss"
        else:  # NO
            return "loss" if yes_won else "win"

    elif mtype == "TOTAL":
        # Extract line from ticker (e.g. -1, -8.5)
        import re
        m = re.search(r'-(\d+\.?\d*)$', ticker)
        if not m:
            return None
        line = float(m.group(1))
        if side in ("YES", "OVER"):
            return "win" if total > line else "loss"
        else:  # UNDER / NO
            return "win" if total < line else "loss"

    elif mtype == "SPREAD":
        # Leader must cover the spread margin
        margin = abs(scores[0] - scores[1])
        import re
        # Use the LAST number in the ticker (the line), not the date
        m = re.search(r'-(\d+\.?\d*)$', ticker)
        line = float(m.group(1)) if m else 1.5
        # We bet the leader to cover; win if margin > line
        return "win" if margin > line else "loss"

    return None
