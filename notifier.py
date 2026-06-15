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
        log.debug("Pushover not configured, skipping notification: %s | %s", title, message)
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
            log.warning("Pushover notification failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.warning("Pushover notification error: %s", e)


def notify_trade_opened(wallet, side, market_info: dict, price, size_usdc, dry_run):
    prefix = "[DRY RUN] " if dry_run else ""
    question = market_info.get("question", "Unknown market")
    outcome = market_info.get("outcome", "?")
    end_date = market_info.get("end_date", "")
    url = market_info.get("url", "")
    send(
        title=f"{prefix}{'📈' if side == 'BUY' else '📉'} {side}: {outcome}",
        message=(
            f"{question}\n\n"
            f"Bet: {outcome} @ {price:.3f} (implied {price*100:.1f}%)\n"
            f"Size: ${size_usdc:.2f}\n"
            f"Trader: {wallet[:8]}...\n"
            f"Closes: {end_date or 'unknown'}\n"
            f"{url}"
        ),
    )


def notify_no_activity(hours: float, wallets: list):
    send(
        title="No copy signals recently",
        message=(
            f"No qualifying trades detected from any target wallet in "
            f"~{hours:.1f}h.\nWallets: {', '.join(w[:8] + '...' for w in wallets)}\n"
            f"Bot is still running — this just means it's been quiet."
        ),
    )


def notify_error(message: str):
    send(
        title="⚠️ Polycopy bot error",
        message=message,
        priority=1,
    )


def notify_trade_closed(wallet, market_info: dict, entry_price, exit_price,
                         size_usdc, pnl_usdc, dry_run):
    prefix = "[DRY RUN] " if dry_run else ""
    result = "WIN" if pnl_usdc > 0 else ("LOSS" if pnl_usdc < 0 else "BREAKEVEN")
    emoji = "✅" if pnl_usdc > 0 else ("❌" if pnl_usdc < 0 else "➖")
    question = market_info.get("question", "Unknown market")
    outcome = market_info.get("outcome", "?")
    url = market_info.get("url", "")
    send(
        title=f"{prefix}{emoji} {result}: {outcome}",
        message=(
            f"{question}\n\n"
            f"Outcome: {outcome}\n"
            f"Entry: {entry_price:.3f} → Exit: {exit_price:.3f}\n"
            f"Size: ${size_usdc:.2f} | P&L: ${pnl_usdc:+.2f}\n"
            f"Trader: {wallet[:8]}...\n"
            f"{url}"
        ),
        priority=0,
    )
