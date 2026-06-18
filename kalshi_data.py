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
    """
    Fetch open single-game markets using sport-specific series tickers.
    Uses direct REST calls with auth headers since SDK doesn't support series_ticker.
    Falls back to general endpoint if series fetch returns nothing useful.
    """
    import base64
    import time as _time
    import requests as _requests

    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_pem = os.environ.get("KALSHI_PRIVATE_KEY", "")
    base_url = KALSHI_DEMO_URL if KALSHI_USE_DEMO else KALSHI_LIVE_URL

    def _make_headers(path: str) -> dict:
        if not api_key_id or not private_key_pem:
            return {}
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding

            pem = private_key_pem.strip()
            if not pem.startswith("-----"):
                pem = f"-----BEGIN PRIVATE KEY-----\n{pem}\n-----END PRIVATE KEY-----"
            elif "\\n" in pem:
                pem = pem.replace("\\n", "\n")

            key = load_pem_private_key(pem.encode(), password=None)
            ts = str(int(_time.time() * 1000))
            msg = ts + "GET" + path
            sig = key.sign(
                msg.encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
            return {
                "KALSHI-ACCESS-KEY": api_key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "Content-Type": "application/json",
            }
        except Exception as e:
            log.warning("Auth header error: %s", e)
            return {}

    # Kalshi has two separate base URLs:
    # - api.elections.kalshi.com: politics, economics, crypto (clean single-question markets)
    # - trading-api.kalshi.com / api.elections.kalshi.com: sports picks/parlays
    # Try elections API with explicit category filters to get non-parlay markets

    ELECTIONS_BASE = "https://api.elections.kalshi.com/trade-api/v2"
    all_markets = []
    seen_tickers = set()

    import requests as _rq
    session = _rq.Session()

    for try_base in [ELECTIONS_BASE, base_url]:
        if all_markets:
            break
        try:
            headers = _make_headers("/trade-api/v2/markets")
            r = session.get(
                try_base + "/markets",
                params={"status": status, "limit": limit},
                headers=headers,
                timeout=15,
            )
            host = try_base.split(".")[1]
            log.info("Markets from %s → status %d", host, r.status_code)
            if r.status_code == 200:
                data = r.json()
                raw = data.get("markets", [])
                for m in raw:
                    ticker = m.get("ticker", "")
                    if ticker and ticker not in seen_tickers:
                        seen_tickers.add(ticker)
                        all_markets.append(m)
                if all_markets:
                    log.info("Fetched %d markets from %s", len(all_markets), host)
                    log.info("Sample market keys: %s", list(all_markets[0].keys())[:10])
                    log.info("Sample market data: %s", str(all_markets[0])[:300])
        except Exception as e:
            log.debug("Fetch from %s failed: %s", try_base, e)

    if not all_markets:
        # Last resort: use SDK
        log.info("Direct fetch returned 0, trying SDK")
        api = _get_api()
        if not api:
            return []
        try:
            try:
                resp = api.get_markets(status=status, limit=limit)
            except TypeError:
                resp = api.get_markets()
            if hasattr(resp, "markets"):
                raw = resp.markets or []
            elif isinstance(resp, dict):
                raw = resp.get("markets", resp.get("data", []))
            elif isinstance(resp, list):
                raw = resp
            else:
                raw = []
            for m in raw:
                d = m.to_dict() if hasattr(m, "to_dict") else m
                if isinstance(d, dict):
                    all_markets.append(d)
            if all_markets:
                log.info("Fetched %d markets via SDK", len(all_markets))
                log.info("Sample market keys: %s", list(all_markets[0].keys())[:10])
                log.info("Sample market data: %s", str(all_markets[0])[:300])
        except Exception as e:
            log.error("SDK market fetch failed: %s", e)

    # Try to extract individual game markets from the parlay bundles
    if all_markets:
        single_game = get_single_game_markets(all_markets)
        if single_game:
            log.info("Adding %d single-game markets extracted from parlay bundles", len(single_game))
            return single_game

    return all_markets


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
        # Kalshi prices are in cents (1-99), convert to 0-1 float
        yes_raw = m.get("yes_ask") or m.get("yes_bid") or m.get("last_price") or 50
        no_raw = m.get("no_ask") or m.get("no_bid") or (100 - float(yes_raw))
        yes = float(yes_raw) / 100
        no = float(no_raw) / 100
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


def _extract_event_tickers(markets: list) -> set:
    """Extract individual event tickers from parlay Associated Events fields."""
    tickers = set()
    for m in markets:
        custom_strike = m.get("custom_strike", {})
        if not isinstance(custom_strike, dict):
            continue
        assoc = custom_strike.get("Associated Events", "") or ""
        for ticker in str(assoc).split(","):
            ticker = ticker.strip()
            if ticker and "-" in ticker:
                # Get the base event ticker (remove leg suffix)
                # e.g. KXWCGAME-26JUN19BRAHTI → KXWCGAME-26JUN19BRAHTI
                tickers.add(ticker)
    return tickers


def get_single_game_markets(parlay_markets: list) -> list:
    """
    Fetch individual game markets by extracting tickers from parlay bundles
    and fetching each one directly via the API.
    """
    import requests as _rq
    import base64
    import time as _time

    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_pem = os.environ.get("KALSHI_PRIVATE_KEY", "")
    base_url = KALSHI_DEMO_URL if KALSHI_USE_DEMO else KALSHI_LIVE_URL
    ELECTIONS_BASE = "https://api.elections.kalshi.com/trade-api/v2"

    def _make_headers(path: str) -> dict:
        if not api_key_id or not private_key_pem:
            return {}
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            pem = private_key_pem.strip()
            if not pem.startswith("-----"):
                pem = f"-----BEGIN PRIVATE KEY-----\n{pem}\n-----END PRIVATE KEY-----"
            elif "\\n" in pem:
                pem = pem.replace("\\n", "\n")
            key = load_pem_private_key(pem.encode(), password=None)
            ts = str(int(_time.time() * 1000))
            sig = key.sign(
                (ts + "GET" + path).encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
            return {
                "KALSHI-ACCESS-KEY": api_key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "Content-Type": "application/json",
            }
        except Exception as e:
            log.warning("Auth error: %s", e)
            return {}

    # Extract unique event tickers from parlay bundles
    event_tickers = _extract_event_tickers(parlay_markets)
    if not event_tickers:
        return []

    log.info("Found %d individual event tickers in parlay bundles", len(event_tickers))

    # Get one sample event per series to test which endpoints work
    series_map = {}
    for t in event_tickers:
        series = t.split("-")[0]
        if series not in series_map:
            series_map[series] = t

    log.info("Unique series: %s", sorted(series_map.keys()))

    session = _rq.Session()
    all_markets = []
    seen = set()

    for series, sample_event in list(series_map.items())[:20]:
        for try_base in [ELECTIONS_BASE, base_url]:
            try:
                path = "/trade-api/v2/markets"
                headers = _make_headers(path)
                r = session.get(
                    try_base + path,
                    params={"event_ticker": sample_event, "status": "open", "limit": 20},
                    headers=headers,
                    timeout=8,
                )
                log.info("Event %s @ %s → %d", sample_event, try_base.split(".")[1], r.status_code)
                if r.status_code == 200:
                    data = r.json()
                    mkts = data.get("markets", [])
                    if mkts:
                        for m in mkts:
                            ticker = m.get("ticker", "")
                            if ticker and ticker not in seen:
                                seen.add(ticker)
                                all_markets.append(m)
                        log.info("  Got %d markets for %s", len(mkts), series)
                        break
                elif r.status_code in (404, 400):
                    break
                elif r.status_code == 401:
                    log.warning("Auth failed @ %s", try_base)
                    break
            except Exception as e:
                log.debug("Event %s failed: %s", sample_event, e)
                continue

    log.info("Total single-game markets fetched: %d", len(all_markets))
    return all_markets


def _is_sports_parlay(market: dict) -> bool:
    """Returns True if this is a multi-leg bundle — skip it."""
    ticker = market.get("ticker", "").upper()
    title = market.get("title", "")
    event_ticker = market.get("event_ticker", "").upper()

    # Sports parlay ticker patterns
    sports_parlay_keywords = ["MULTIGAME", "EXTENDED", "CROSSCATEGORY", "KXMVE"]
    for kw in sports_parlay_keywords:
        if kw in ticker or kw in event_ticker:
            return True

    # Custom strike with multiple associated markets = multi-leg bundle
    custom_strike = market.get("custom_strike", {})
    if isinstance(custom_strike, dict):
        assoc = custom_strike.get("Associated Markets", "") or custom_strike.get("Associated Events", "")
        if assoc and "," in str(assoc):
            return True

    # Title with 2+ comma-separated outcomes = multi-leg
    if title and title.count(",") >= 2:
        return True

    return False


def format_markets_for_claude(markets: list) -> tuple[list, list]:
    """
    Split single-game Kalshi markets into short_term and long_term.
    Filters out multi-game parlay bundles entirely.
    """
    now = datetime.now(timezone.utc)
    short_term = []
    long_term = []
    skipped_parlay = 0

    for m in markets:
        if not isinstance(m, dict):
            continue
        if _is_sports_parlay(m):
            skipped_parlay += 1
            continue

        close_time = m.get("close_time") or m.get("expiration_time")
        ticker = m.get("ticker", "")
        # Elections API uses different title fields
        title = (m.get("title") or m.get("subtitle") or
                 m.get("question") or ticker)
        category = (m.get("category") or
                   m.get("event_ticker", "").split("-")[0] if m.get("event_ticker") else "")

        if m.get("status") not in ("active", "open", None, ""):
            continue
        if not close_time:
            continue

        try:
            if isinstance(close_time, datetime):
                end = close_time
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
            elif isinstance(close_time, str):
                end = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            elif isinstance(close_time, (int, float)):
                end = datetime.fromtimestamp(float(close_time), tz=timezone.utc)
            else:
                continue

            hours_left = (end - now).total_seconds() / 3600
            days_left = hours_left / 24

            if hours_left < 0.5 or days_left > 90:
                continue

            yes_price = float(m.get("yes_ask") or m.get("yes_bid") or m.get("last_price") or 50)
            if yes_price > 1:
                yes_price = yes_price / 100

            normalized = {
                "question": title,
                "ticker": ticker,
                "slug": ticker,
                "endDate": end.isoformat(),
                "liquidity": float(m.get("open_interest") or m.get("liquidity") or 0),
                "volume": float(m.get("volume") or 0),
                "yes_price": yes_price,
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

        except Exception as e:
            log.debug("Skipping market %s: %s", ticker, e)
            continue

    short_term.sort(key=lambda m: m["_hours_left"])
    long_term.sort(key=lambda m: m["_hours_left"])
    log.info(
        "Kalshi markets: %d total (%d parlays filtered) → %d same/next-day, %d longer-term",
        len(markets), skipped_parlay, len(short_term), len(long_term),
    )
    if short_term:
        log.info("First short-term market: %s", short_term[0].get("question", short_term[0].get("ticker")))
    if long_term:
        log.info("First long-term market: %s", long_term[0].get("question", long_term[0].get("ticker")))
    return short_term, long_term
