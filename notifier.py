"""
Pushover notification helper.

Set these env vars on Railway:
- PUSHOVER_TOKEN  (your Application API Token)
- PUSHOVER_USER   (your User Key)

If either is missing, notifications are silently skipped (logged only).
"""

import os
import logging
import requests

log = logging.getLogger("polycopy.notify")

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def send(title: str, message: str, priority: int = 0):
    """Send a Pushover notification. Safe no-op if not configured."""
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log.debug("Pushover not configured, skipping: %s | %s", title, message)
        return
    try:
        resp = requests.post(
            PUSHOVER_URL,
            data={
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "title": title,
                "message": message,
                "priority": priority,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("Pushover failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.warning("Pushover error: %s", e)


def notify_trade_opened(wallet, side, market_info: dict, price, size_usdc,
                        dry_run, conviction: int = 1):
    prefix = "[DRY RUN] " if dry_run else ""
    question = market_info.get("question", "Unknown market")
    outcome = market_info.get("outcome", "?")
    end_date = market_info.get("end_date", "")
    url = market_info.get("url", "")
    conv_str = f" 🔥 {conviction} traders agree!" if conviction >= 2 else ""
    send(
        title=f"{prefix}{'📈' if side == 'BUY' else '📉'} {side}: {outcome}{conv_str}",
        message=(
            f"{question}\n\n"
            f"Bet: {outcome} @ {price:.3f} (implied {price*100:.1f}%)\n"
            f"Size: ${size_usdc:.2f}\n"
            f"Trader: {wallet[:8]}...\n"
            f"Closes: {end_date or 'unknown'}\n"
            f"{url}"
        ),
    )


def notify_trade_closed(wallet, market_info: dict, entry_price, exit_price,
                        size_usdc, pnl_usdc, dry_run, reason: str = ""):
    prefix = "[DRY RUN] " if dry_run else ""
    result = "WIN" if pnl_usdc > 0 else ("LOSS" if pnl_usdc < 0 else "BREAKEVEN")
    emoji = "✅" if pnl_usdc > 0 else ("❌" if pnl_usdc < 0 else "➖")
    question = market_info.get("question", "Unknown market")
    outcome = market_info.get("outcome", "?")
    url = market_info.get("url", "")
    reason_str = f"\nReason: {reason}" if reason else ""
    send(
        title=f"{prefix}{emoji} {result}: {outcome}",
        message=(
            f"{question}\n\n"
            f"Outcome: {outcome}\n"
            f"Entry: {entry_price:.3f} → Exit: {exit_price:.3f}\n"
            f"Size: ${size_usdc:.2f} | P&L: ${pnl_usdc:+.2f}\n"
            f"Trader: {wallet[:8]}...{reason_str}\n"
            f"{url}"
        ),
        priority=0,
    )


def notify_high_conviction(token_id, market_info: dict, wallets: list, price: float):
    question = market_info.get("question", "Unknown market")
    outcome = market_info.get("outcome", "?")
    url = market_info.get("url", "")
    send(
        title=f"🔥 HIGH CONVICTION: {outcome} ({len(wallets)} traders)",
        message=(
            f"{question}\n\n"
            f"Outcome: {outcome} @ {price:.3f}\n"
            f"{len(wallets)} target wallets all buying the same side:\n"
            f"{', '.join(w[:8]+'...' for w in wallets)}\n"
            f"{url}"
        ),
        priority=1,
    )


def notify_no_activity(hours: float, wallets: list):
    send(
        title="😴 No copy signals recently",
        message=(
            f"No qualifying trades from any target in ~{hours:.1f}h.\n"
            f"Wallets: {', '.join(w[:8]+'...' for w in wallets)}\n"
            f"Bot is still running — just quiet."
        ),
    )


def notify_error(message: str):
    send(
        title="⚠️ Polycopy bot error",
        message=message,
        priority=1,
    )


def notify_weekly_report(report: dict):
    """Send a weekly P&L summary."""
    total_pnl = report.get("total_pnl", 0)
    emoji = "📈" if total_pnl >= 0 else "📉"
    send(
        title=f"{emoji} Weekly Report | P&L: ${total_pnl:+.2f}",
        message=(
            f"Trades this week: {report.get('trade_count', 0)}\n"
            f"Wins: {report.get('wins', 0)} | Losses: {report.get('losses', 0)}\n"
            f"Win rate: {report.get('win_rate_pct', 0):.1f}%\n"
            f"Best trade: ${report.get('best_trade', 0):+.2f}\n"
            f"Worst trade: ${report.get('worst_trade', 0):+.2f}\n"
            f"Best wallet: {report.get('best_wallet', 'N/A')}\n"
            f"Open positions: {report.get('open_positions', 0)}"
        ),
    )
