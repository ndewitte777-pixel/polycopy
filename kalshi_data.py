"""
Kalshi Data API
===============
Public market data from Kalshi — no authentication needed for reads.
Used by the Claude trader to find markets and by the copy engine
to monitor top traders (via public trade history).
"""

import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("polycopy.kalshi_data")

BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
session = requests.Session()


def get_markets(limit: int = 200, status: str = "open",
                min_close_ts: int = None) -> list:
    """Fetch open markets, optionally filtered by min close time."""
    try:
        params = {"limit": limit, "status": status}
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        r = session.get(f"{BASE_URL}/markets", params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("markets", [])
    except Exception as e:
        log.error("Failed to fetch Kalshi markets: %s", e)
        return []


def get_events(limit: int = 100, status: str = "open") -> list:
    """Fetch open events (grouped markets)."""
    try:
        r = session.get(f"{BASE_URL}/events",
                        params={"limit": limit, "status": status}, timeout=15)
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception as e:
        log.error("Failed to fetch Kalshi events: %s", e)
        return []


def get_market_price(ticker: str) -> tuple[float, float]:
    """Returns (yes_price, no_price) as 0-1 floats."""
    try:
        r = session.get(f"{BASE_URL}/markets/{ticker}", timeout=10)
        r.raise_for_status()
        m = r.json().get("market", r.json())
        yes = float(m.get("yes_ask") or m.get("yes_bid") or 0.5)
        no = float(m.get("no_ask") or m.get("no_bid") or 0.5)
        return yes, no
    except Exception as e:
        log.warning("Price fetch failed for %s: %s", ticker, e)
        return 0.5, 0.5


def get_public_trades(ticker: str = None, limit: int = 100) -> list:
    """Fetch recent public trades (no auth needed)."""
    try:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        r = session.get(f"{BASE_URL}/markets/trades", params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("trades", [])
    except Exception as e:
        log.warning("Failed to fetch trades: %s", e)
        return []


def format_markets_for_claude(markets: list) -> tuple[list, list]:
    """
    Split markets into short_term (closes within 2 days) and long_term.
    Returns (short_term, long_term) lists with normalized fields.
    """
    now = datetime.now(timezone.utc)
    short_term = []
    long_term = []

    for m in markets:
        close_time = m.get("close_time") or m.get("expiration_time") or ""
        if not close_time:
            continue

        try:
            if isinstance(close_time, (int, float)):
                end = datetime.fromtimestamp(close_time, tz=timezone.utc)
            else:
                end = datetime.fromisoformat(
                    str(close_time).replace("Z", "+00:00")
                )

            hours_left = (end - now).total_seconds() / 3600
            days_left = hours_left / 24

            if hours_left < 1:
                continue  # too close
            if days_left > 90:
                continue  # too far out

            # Normalize to same shape as Polymarket markets
            yes_ask = float(m.get("yes_ask") or m.get("yes_bid") or 0.5)
            liquidity = float(m.get("liquidity") or m.get("open_interest") or 0)

            normalized = {
                "question": m.get("title") or m.get("subtitle") or m.get("ticker", ""),
                "ticker": m.get("ticker", ""),
                "slug": m.get("ticker", ""),
                "endDate": close_time,
                "liquidity": liquidity,
                "volume": float(m.get("volume") or 0),
                "yes_price": yes_ask,
                "_hours_left": hours_left,
                "_days_left": days_left,
                "_source": "kalshi",
                "tags": [{"label": m.get("category", "")}],
                # For compatibility with polymarket code paths
                "clobTokenIds": None,
                "outcomes": '["Yes","No"]',
            }

            if days_left <= 2:
                short_term.append(normalized)
            else:
                long_term.append(normalized)

        except Exception:
            continue

    short_term.sort(key=lambda m: m["_hours_left"])
    long_term.sort(key=lambda m: m["_hours_left"])

    log.info(
        "Kalshi markets: %d total → %d same/next-day, %d longer-term",
        len(markets), len(short_term), len(long_term),
    )
    return short_term, long_term
