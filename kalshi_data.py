"""
Kalshi Data API
===============
Uses the official kalshi-python SDK for authentication.
Much simpler than manual RSA signing.

Set in Railway Variables:
- KALSHI_EMAIL    : your Kalshi account email
- KALSHI_PASSWORD : your Kalshi account password
- KALSHI_USE_DEMO : "true" for paper trading
"""

import os
import logging
from datetime import datetime, timezone

log = logging.getLogger("polycopy.kalshi_data")

KALSHI_USE_DEMO = os.environ.get("KALSHI_USE_DEMO", "true").lower() == "true"

_api = None

# Updated API URLs per Kalshi's migration notice
KALSHI_LIVE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"


def _get_api():
    """Get or create authenticated Kalshi API instance."""
    global _api
    if _api is not None:
        return _api

    try:
        from kalshi_python import Configuration, KalshiClient

        api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        private_key_pem = os.environ.get("KALSHI_PRIVATE_KEY", "")

        if not api_key_id or not private_key_pem:
            log.error("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY must be set in Railway Variables")
            return None

        # Fix PEM formatting if newlines were stripped by Railway
        pem = private_key_pem.strip()
        if not pem.startswith("-----"):
            pem = f"-----BEGIN PRIVATE KEY-----\n{pem}\n-----END PRIVATE KEY-----"
        elif "\\n" in pem:
            pem = pem.replace("\\n", "\n")

        config = Configuration(
            host=KALSHI_DEMO_URL if KALSHI_USE_DEMO else KALSHI_LIVE_URL
        )
        config.api_key_id = api_key_id
        config.private_key_pem = pem

        _api = KalshiClient(config)
        log.info("Kalshi API authenticated (%s)", "DEMO" if KALSHI_USE_DEMO else "LIVE")
        return _api
    except Exception as e:
        log.error("Failed to authenticate with Kalshi: %s", e)
        return None


def get_balance() -> float:
    """Get account balance in dollars."""
    api = _get_api()
    if not api:
        return 0.0
    try:
        resp = api.get_balance()
        bal = resp.balance if hasattr(resp, "balance") else 0
        return float(bal) / 100
    except Exception as e:
        log.warning("Failed to get balance: %s", e)
        return 0.0


def get_markets(limit: int = 200, status: str = "open") -> list:
    """Fetch open markets."""
    api = _get_api()
    if not api:
        return []
    try:
        # Try different parameter combinations the SDK might expect
        try:
            resp = api.get_markets(status=status, limit=limit)
        except TypeError:
            try:
                resp = api.get_markets(limit=limit)
            except TypeError:
                resp = api.get_markets()

        # Handle different response shapes
        if hasattr(resp, "markets"):
            markets = resp.markets or []
        elif isinstance(resp, dict):
            markets = resp.get("markets", resp.get("data", []))
        elif isinstance(resp, list):
            markets = resp
        else:
            log.warning("Unexpected get_markets response type: %s", type(resp))
            log.warning("Response: %s", str(resp)[:500])
            markets = []

        result = []
        for m in markets:
            if hasattr(m, "to_dict"):
                result.append(m.to_dict())
            elif isinstance(m, dict):
                result.append(m)

        log.info("Fetched %d markets from Kalshi", len(result))
        if result:
            log.info("Sample market keys: %s", list(result[0].keys())[:10])
            log.info("Sample market data: %s", str(result[0])[:600])
        return result

    except Exception as e:
        log.error("Failed to fetch Kalshi markets: %s", e)
        return []


def get_market_price(ticker: str) -> tuple[float, float]:
    """Returns (yes_price, no_price) as 0-1 floats."""
    api = _get_api()
    if not api:
        return 0.5, 0.5
    try:
        resp = api.get_market(ticker)
        m = resp.market if hasattr(resp, "market") else resp
        if hasattr(m, "to_dict"):
            m = m.to_dict()
        yes = float(m.get("yes_ask") or m.get("yes_bid") or 0.5)
        no = float(m.get("no_ask") or m.get("no_bid") or 0.5)
        return yes, no
    except Exception as e:
        log.warning("Price fetch failed for %s: %s", ticker, e)
        return 0.5, 0.5


def place_order(ticker: str, side: str, count: int, price_cents: int) -> dict:
    """Place an order. price_cents is 1-99 (cents per contract)."""
    import uuid
    api = _get_api()
    if not api:
        return {"error": "Not authenticated"}
    try:
        from kalshi_python.models import CreateOrderRequest
        resp = api.create_order(CreateOrderRequest(
            ticker=ticker,
            action="buy",
            type="limit",
            side=side.lower(),
            yes_price=price_cents if side.upper() == "YES" else None,
            no_price=price_cents if side.upper() == "NO" else None,
            count=count,
            client_order_id=str(uuid.uuid4()),
        ))
        return resp.to_dict() if hasattr(resp, "to_dict") else {"status": "ok"}
    except Exception as e:
        log.error("Order failed: %s", e)
        return {"error": str(e)}


def format_markets_for_claude(markets: list) -> tuple[list, list]:
    """Split markets into short_term and long_term lists."""
    now = datetime.now(timezone.utc)
    short_term = []
    long_term = []

    for m in markets:
        if isinstance(m, dict):
            close_time = m.get("close_time") or m.get("expiration_time") or ""
            ticker = m.get("ticker", "")
            title = m.get("title") or m.get("subtitle") or ticker
            yes_ask = float(m.get("yes_ask") or m.get("yes_bid") or 0.5) / 100  # cents to dollars
            liquidity = float(m.get("open_interest") or m.get("liquidity") or 0)
            volume = float(m.get("volume") or 0)
            category = m.get("category") or m.get("event_ticker", "").split("-")[0]
        else:
            continue

        if not close_time:
            continue

        try:
            if isinstance(close_time, (int, float)):
                end = datetime.fromtimestamp(close_time, tz=timezone.utc)
            else:
                end = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))

            hours_left = (end - now).total_seconds() / 3600
            days_left = hours_left / 24

            # Debug first market
            if len(short_term) + len(long_term) == 0:
                log.info("First market: ticker=%s close_time=%s hours_left=%.1f yes_price=%s",
                         m.get("ticker","?"), close_time, hours_left,
                         m.get("yes_ask") or m.get("yes_bid") or "N/A")

            if hours_left < 1 or days_left > 90:
                continue

            normalized = {
                "question": title,
                "ticker": ticker,
                "slug": ticker,
                "endDate": close_time,
                "liquidity": liquidity,
                "volume": volume,
                "yes_price": yes_ask,
                "_hours_left": hours_left,
                "_days_left": days_left,
                "_source": "kalshi",
                "tags": [{"label": category}],
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



def _get_auth_headers(method: str = "GET", path: str = "/trade-api/v2/markets") -> dict:
    """Build Kalshi auth headers for signed requests."""
    import os
    import base64
    import time as _time
    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_pem = os.environ.get("KALSHI_PRIVATE_KEY", "")

    if not api_key_id or not private_key_pem:
        log.warning("KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY not set in env vars")
        return {}

    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        pem = private_key_pem.strip()
        if not pem.startswith("-----"):
            pem = f"-----BEGIN PRIVATE KEY-----\n{pem}\n-----END PRIVATE KEY-----"

        private_key = load_pem_private_key(pem.encode(), password=None)
        timestamp_ms = str(int(_time.time() * 1000))
        # Kalshi signs: timestamp + method + path (no query string)
        msg = timestamp_ms + method.upper() + path

        signature = private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }
    except Exception as e:
        log.warning("Could not build auth headers: %s", e)
        return {}




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
