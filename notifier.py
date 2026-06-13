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


def notify_trade_opened(wallet, side, condition_id, token_id, price, size_usdc, dry_run):
    prefix = "[DRY RUN] " if dry_run else ""
    send(
        title=f"{prefix}Trade Opened: {side}",
        message=(
            f"Copying {wallet[:8]}...\n"
            f"Side: {side}\n"
            f"Price: {price:.3f}\n"
            f"Size: ${size_usdc:.2f}\n"
            f"Market: {condition_id[:10] if condition_id else 'unknown'}..."
        ),
    )


def notify_trade_closed(wallet, condition_id, token_id, entry_price, exit_price,
                         size_usdc, pnl_usdc, dry_run):
    prefix = "[DRY RUN] " if dry_run else ""
    result = "WIN" if pnl_usdc > 0 else ("LOSS" if pnl_usdc < 0 else "BREAKEVEN")
    emoji = "✅" if pnl_usdc > 0 else ("❌" if pnl_usdc < 0 else "➖")
    send(
        title=f"{prefix}{emoji} Trade Closed: {result}",
        message=(
            f"Following {wallet[:8]}...\n"
            f"Entry: {entry_price:.3f} -> Exit: {exit_price:.3f}\n"
            f"Size: ${size_usdc:.2f}\n"
            f"P&L: ${pnl_usdc:+.2f}\n"
            f"Market: {condition_id[:10] if condition_id else 'unknown'}..."
        ),
        priority=0,
    )
