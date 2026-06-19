"""
Kalshi-Native Copy Trading Engine
===================================
Instead of copying Polymarket wallets and trying to find Kalshi equivalents,
this watches Kalshi's own public trade feed for large, informed trades and
copies them directly on the same market.

Strategy:
- Poll recent trades on all active single-game markets every 30 seconds
- Flag trades that are unusually large (top 5% by size)
- When multiple large trades go the same direction within 60 seconds → signal
- Send through Claude filter → place on the same Kalshi ticker

Advantages over Polymarket copy:
- Same market — no translation needed, no matching errors
- Instant — trade happens on the exact same market
- Better fills — we're on Kalshi already
- Informed flow — large Kalshi trades often come from sharp bettors
"""

import logging
import time
import requests
import base64
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger("polycopy.kalshi_copy")

# Minimum trade size in dollars to consider "smart money"
MIN_SMART_MONEY_SIZE = 50.0   # $50+ trades are meaningful on Kalshi

# How many large trades in same direction = signal
MIN_LARGE_TRADES_FOR_SIGNAL = 2

# Time window to cluster trades (seconds)
CLUSTER_WINDOW = 90

# How often to poll (seconds)
POLL_INTERVAL = 30

# Cache of recent trades per ticker
_trade_cache: dict = {}   # ticker → list of recent trades
_last_poll: float = 0.0
_seen_trade_ids: set = set()


def _make_auth_headers(path: str) -> dict:
    """Build Kalshi auth headers for GET request."""
    import os
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_pem = os.environ.get("KALSHI_PRIVATE_KEY", "")

    if not api_key_id or not private_key_pem:
        return {}

    try:
        pem = private_key_pem.strip()
        if not pem.startswith("-----"):
            pem = f"-----BEGIN PRIVATE KEY-----\n{pem}\n-----END PRIVATE KEY-----"
        elif "\\n" in pem:
            pem = pem.replace("\\n", "\n")
        key = load_pem_private_key(pem.encode(), password=None)
        ts = str(int(time.time() * 1000))
        sig = key.sign(
            (ts + "GET" + path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
        }
    except Exception as e:
        log.debug("Auth header error: %s", e)
        return {}


def fetch_recent_trades(ticker: str, session: requests.Session,
                        limit: int = 50) -> list:
    """
    Fetch recent trades for a Kalshi market ticker.
    Returns list of trade dicts.
    """
    path = f"/trade-api/v2/markets/{ticker}/trades"
    base = "https://api.elections.kalshi.com"
    try:
        headers = _make_auth_headers(path)
        r = session.get(
            f"{base}{path}",
            params={"limit": limit},
            headers=headers,
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("trades", [])
        elif r.status_code == 404:
            return []
        else:
            log.debug("Trade fetch for %s: %d", ticker, r.status_code)
            return []
    except Exception as e:
        log.debug("Trade fetch error for %s: %s", ticker, e)
        return []


def _parse_trade_size(trade: dict) -> float:
    """
    Calculate dollar size of a trade from Kalshi trade data.
    trade has: count (contracts), yes_price or no_price (cents)
    """
    count = float(trade.get("count", 0) or 0)
    yes_price = float(trade.get("yes_price", 0) or 0)  # in cents
    no_price = float(trade.get("no_price", 0) or 0)
    price_cents = yes_price if yes_price > 0 else no_price
    price_dollars = price_cents / 100.0
    return count * price_dollars


def detect_smart_money_signals(markets: list,
                                session: requests.Session) -> list:
    """
    Poll recent trades on active markets and detect large coordinated buys.
    Returns list of copy signals.
    """
    global _last_poll, _seen_trade_ids

    now = time.time()
    if now - _last_poll < POLL_INTERVAL:
        return []

    _last_poll = now
    signals = []

    # Only check same/next-day markets — those have the most actionable info
    today_markets = [m for m in markets
                     if m.get("_days_left", 99) <= 3
                     and not any(kw in m.get("ticker", "").upper()
                                 for kw in ["MULTIGAME", "KXMVE", "EXTENDED"])]

    if not today_markets:
        return []

    # Sample up to 20 markets to avoid rate limits
    sample = today_markets[:20]
    ticker_flows: dict = defaultdict(lambda: {"yes_size": 0, "no_size": 0,
                                               "yes_count": 0, "no_count": 0,
                                               "trades": []})

    for market in sample:
        ticker = market.get("ticker", "")
        if not ticker:
            continue

        trades = fetch_recent_trades(ticker, session, limit=30)
        if not trades:
            continue

        for trade in trades:
            trade_id = trade.get("trade_id", "")
            if not trade_id or trade_id in _seen_trade_ids:
                continue

            size = _parse_trade_size(trade)
            if size < MIN_SMART_MONEY_SIZE:
                continue

            # Check if trade is recent (within cluster window)
            created = trade.get("created_time", "")
            if created:
                try:
                    ts = datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    ).timestamp()
                    if now - ts > CLUSTER_WINDOW:
                        continue
                except Exception:
                    pass

            side = trade.get("taker_side", "").lower()  # "yes" or "no"
            _seen_trade_ids.add(trade_id)

            flow = ticker_flows[ticker]
            if side == "yes":
                flow["yes_size"] += size
                flow["yes_count"] += 1
            elif side == "no":
                flow["no_size"] += size
                flow["no_count"] += 1
            flow["trades"].append({
                "trade_id": trade_id,
                "size": size,
                "side": side,
                "price": (float(trade.get("yes_price", 50) or 50)) / 100,
            })

    # Keep _seen_trade_ids from growing forever
    if len(_seen_trade_ids) > 5000:
        _seen_trade_ids = set(list(_seen_trade_ids)[-2000:])

    # Evaluate each ticker for signals
    for ticker, flow in ticker_flows.items():
        yes_size = flow["yes_size"]
        no_size = flow["no_size"]
        yes_count = flow["yes_count"]
        no_count = flow["no_count"]

        # Need at least 2 large trades in same direction
        if yes_count >= MIN_LARGE_TRADES_FOR_SIGNAL and yes_size > no_size * 2:
            market = next((m for m in sample if m.get("ticker") == ticker), {})
            avg_price = (yes_size / yes_count) / yes_count if yes_count else 0.5
            # Compute actual avg price from trades
            yes_trades = [t for t in flow["trades"] if t["side"] == "yes"]
            avg_price = (sum(t["price"] for t in yes_trades) /
                         len(yes_trades)) if yes_trades else 0.5

            signals.append({
                "type": "kalshi_copy",
                "source": "kalshi_flow",
                "ticker": ticker,
                "side": "YES",
                "price": avg_price,
                "total_size": yes_size,
                "trade_count": yes_count,
                "market": market,
                "question": market.get("question", market.get("title", ticker)),
                "reason": f"{yes_count} large YES trades totalling ${yes_size:.0f} "
                          f"in last {CLUSTER_WINDOW}s",
                "confidence": min(80, 55 + yes_count * 5),
            })
            log.info("Smart money signal: YES %s | $%.0f across %d trades",
                     ticker, yes_size, yes_count)

        elif no_count >= MIN_LARGE_TRADES_FOR_SIGNAL and no_size > yes_size * 2:
            market = next((m for m in sample if m.get("ticker") == ticker), {})
            no_trades = [t for t in flow["trades"] if t["side"] == "no"]
            avg_price = (sum(t["price"] for t in no_trades) /
                         len(no_trades)) if no_trades else 0.5

            signals.append({
                "type": "kalshi_copy",
                "source": "kalshi_flow",
                "ticker": ticker,
                "side": "NO",
                "price": 1 - avg_price,
                "total_size": no_size,
                "trade_count": no_count,
                "market": market,
                "question": market.get("question", market.get("title", ticker)),
                "reason": f"{no_count} large NO trades totalling ${no_size:.0f} "
                          f"in last {CLUSTER_WINDOW}s",
                "confidence": min(80, 55 + no_count * 5),
            })
            log.info("Smart money signal: NO %s | $%.0f across %d trades",
                     ticker, no_size, no_count)

    return signals


def run_kalshi_copy(all_markets: list, executor, state: dict,
                    session: requests.Session, notifier,
                    claude_filter_fn=None) -> int:
    """
    Main entry — called from bot.py every LIVE_POLL_INTERVAL seconds.
    Returns number of positions opened.
    """
    import state as _st
    from config import (MAX_TRADE_USDC, MAX_DAILY_TRADES,
                        CASH_RESERVE_PCT, MAX_OPEN_POSITIONS,
                        MAX_DAILY_LOSS_USDC, DRY_RUN)

    _st.reset_daily_if_needed(state)

    # Daily limits
    if state.get("daily_trades", 0) >= MAX_DAILY_TRADES:
        return 0
    if state.get("daily_loss", 0) >= MAX_DAILY_LOSS_USDC:
        return 0

    # Cash reserve check
    bankroll = state.get("bankroll", 35.0)
    max_at_risk = bankroll * (1 - CASH_RESERVE_PCT)
    if state.get("total_at_risk", 0) >= max_at_risk:
        return 0

    signals = detect_smart_money_signals(all_markets, session)
    if not signals:
        return 0

    opened = 0
    for signal in signals:
        ticker = signal["ticker"]
        side = signal["side"]
        price = signal["price"]
        question = signal["question"]
        confidence = signal["confidence"]

        # Skip extreme prices
        if price < 0.20 or price > 0.80:
            log.info("Kalshi copy: skipping extreme price %.2f for %s", price, ticker)
            continue

        # Skip if already in this market
        if ticker in state.get("open_lots", {}):
            continue

        size_usdc = min(MAX_TRADE_USDC, max(1.50, MAX_TRADE_USDC))

        # Claude filter
        if claude_filter_fn:
            market_info = {
                "question": question,
                "outcome": side,
                "end_date": signal.get("market", {}).get("endDate", ""),
                "liquidity": signal.get("total_size", 0) * 10,
                "category": "SPORTS",
                "url": f"https://kalshi.com/markets/{ticker}",
                "extra_context": signal.get("reason", ""),
            }
            try:
                decision = claude_filter_fn(
                    market_info=market_info,
                    price=price,
                    your_size=size_usdc,
                    conviction=signal["trade_count"],
                    num_wallets=signal["trade_count"],
                )
                if decision.get("decision") == "SKIP":
                    log.info("Claude filtered Kalshi copy signal: %s | %s",
                             question[:50], decision.get("reason", ""))
                    continue
            except Exception as e:
                log.debug("Claude filter error: %s", e)

        # Place order
        log.info(
            "KALSHI COPY | %s %s @ %.2f | $%.2f | %s",
            side, ticker, price, size_usdc, signal["reason"],
        )

        if not DRY_RUN:
            resp = executor.place_order(
                token_id=ticker, side=side,
                price=price, size_usdc=size_usdc,
            )
        else:
            resp = {"dry_run": True, "order": {"status": "dry_run"}}
            log.info("[DRY RUN] Would place: %s %s @ %.2f $%.2f",
                     side, ticker, price, size_usdc)

        # Track position
        open_lots = state.setdefault("open_lots", {})
        lot_entry = {
            "entry_price": price,
            "size_usdc": size_usdc,
            "peak_price": price,
            "source": "kalshi_copy",
            "market_info": {"question": question, "outcome": side},
            "opened_at": time.time(),
            "took_profit": False,
        }
        open_lots.setdefault(ticker, []).append(lot_entry)

        state["open_positions"] = state.get("open_positions", 0) + 1
        state["daily_trades"] = state.get("daily_trades", 0) + 1
        state["total_at_risk"] = state.get("total_at_risk", 0.0) + size_usdc

        notifier.send(
            title=f"🔥 Kalshi Copy | {side} {ticker[:20]}",
            message=(
                f"{question}\n\n"
                f"Smart money: {signal['trade_count']} trades "
                f"totalling ${signal['total_size']:.0f}\n"
                f"Price: {price:.2f} | Size: ${size_usdc:.2f}\n"
                f"{signal['reason']}"
            ),
        )
        opened += 1

        if state.get("daily_trades", 0) >= MAX_DAILY_TRADES:
            break
        if state.get("open_positions", 0) >= MAX_OPEN_POSITIONS:
            break

    return opened
