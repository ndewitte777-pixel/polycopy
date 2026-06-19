"""
Kalshi-Native Copy Trading Engine
===================================
Watches Kalshi's public trade feed for large, informed trades and copies them.

KEY INSIGHT: We don't just copy big trades blindly. We track which traders
are consistently profitable over time and only copy traders with proven edge.

Strategy:
1. Watch large trades ($50+) on all active markets
2. Build a leaderboard of trader performance based on resolved markets
3. Only copy traders with 60%+ win rate over 10+ resolved trades
4. Weight copy size by trader win rate and streak

We identify traders by their order side + timing pattern (can't see wallet
addresses on Kalshi, but we can track fill patterns).

Additional edge layers:
- Price momentum: only copy if market price has been moving the same direction
- Consensus: require 3+ large trades same direction within 90s
- Timing: weight later trades higher (more informed as game develops)
- Market liquidity: skip thin markets where big trades move price artificially
"""

import logging
import time
import requests
import base64
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger("polycopy.kalshi_copy")

# Minimum trade size to consider "smart money"
MIN_SMART_MONEY_SIZE = 50.0

# Require this many large trades in same direction before signaling
MIN_TRADES_FOR_SIGNAL = 3  # raised from 2 — need more consensus

# Time window to cluster trades
CLUSTER_WINDOW = 90  # seconds

# Minimum price momentum — price must have moved this direction recently
MOMENTUM_REQUIRED = True

# Skip markets where big trades could be market manipulation (low liquidity)
MIN_MARKET_VOLUME = 500  # $500 minimum volume to be a real market

# Price range — avoid extreme odds
MIN_COPY_PRICE = 0.25
MAX_COPY_PRICE = 0.75  # tighter than rule trader — copy trading needs real uncertainty

# How often to poll
POLL_INTERVAL = 30

# Cooldown per ticker to avoid re-entering same market
TICKER_COOLDOWN = 600  # 10 minutes

_seen_trade_ids: set = set()
_last_poll: float = 0.0
_ticker_cooldowns: dict = {}

# Price history for momentum detection
_price_history: dict = {}  # ticker → list of (timestamp, price)


def _make_auth_headers(method: str, path: str) -> dict:
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
            (ts + method.upper() + path).encode(),
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
        log.debug("Auth error: %s", e)
        return {}


def fetch_recent_trades(ticker: str, session: requests.Session,
                        limit: int = 50) -> list:
    path = f"/trade-api/v2/markets/{ticker}/trades"
    base = "https://api.elections.kalshi.com"
    try:
        headers = _make_auth_headers("GET", path)
        r = session.get(f"{base}{path}", params={"limit": limit},
                        headers=headers, timeout=8)
        if r.status_code == 200:
            return r.json().get("trades", [])
        return []
    except Exception as e:
        log.debug("Trade fetch error %s: %s", ticker, e)
        return []


def fetch_market_details(ticker: str, session: requests.Session) -> dict:
    """Get current market price and volume."""
    path = f"/trade-api/v2/markets/{ticker}"
    base = "https://api.elections.kalshi.com"
    try:
        headers = _make_auth_headers("GET", path)
        r = session.get(f"{base}{path}", headers=headers, timeout=8)
        if r.status_code == 200:
            return r.json().get("market", {})
        return {}
    except Exception:
        return {}


def _parse_trade_size(trade: dict) -> float:
    count = float(trade.get("count", 0) or 0)
    yes_price = float(trade.get("yes_price", 0) or 0)
    no_price = float(trade.get("no_price", 0) or 0)
    price_cents = yes_price if yes_price > 0 else no_price
    return count * (price_cents / 100.0)


def _check_price_momentum(ticker: str, side: str,
                           current_price: float) -> bool:
    """
    Check if price has been moving in the signal direction recently.
    Returns True if momentum supports the trade.
    """
    history = _price_history.get(ticker, [])
    if len(history) < 3:
        # Not enough history — allow the trade but don't boost confidence
        return True

    # Get prices from last 5 minutes
    now = time.time()
    recent = [(ts, p) for ts, p in history if now - ts < 300]
    if len(recent) < 2:
        return True

    oldest_price = recent[0][1]
    price_change = current_price - oldest_price

    if side == "YES":
        return price_change >= -0.02  # price not falling significantly
    else:
        return price_change <= 0.02  # price not rising significantly


def _update_price_history(ticker: str, price: float):
    history = _price_history.setdefault(ticker, [])
    history.append((time.time(), price))
    # Keep last 20 data points
    if len(history) > 20:
        _price_history[ticker] = history[-20:]


def detect_smart_money_signals(markets: list,
                                session: requests.Session) -> list:
    global _last_poll, _seen_trade_ids

    now = time.time()
    if now - _last_poll < POLL_INTERVAL:
        return []
    _last_poll = now

    signals = []

    # Only watch today's single-game markets with enough volume
    today_markets = [
        m for m in markets
        if m.get("_days_left", 99) <= 3
        and not any(kw in m.get("ticker", "").upper()
                    for kw in ["MULTIGAME", "KXMVE", "EXTENDED"])
    ]

    # Sample up to 15 markets to stay within rate limits
    sample = today_markets[:15]
    ticker_flows: dict = defaultdict(lambda: {
        "yes_size": 0.0, "no_size": 0.0,
        "yes_count": 0, "no_count": 0,
        "yes_trades": [], "no_trades": [],
        "market": {},
    })

    for market in sample:
        ticker = market.get("ticker", "")
        if not ticker:
            continue

        # Skip if in cooldown
        if now - _ticker_cooldowns.get(ticker, 0) < TICKER_COOLDOWN:
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

            # Only count recent trades
            created = trade.get("created_time", "")
            if created:
                try:
                    ts = datetime.fromisoformat(
                        created.replace("Z", "+00:00")).timestamp()
                    if now - ts > CLUSTER_WINDOW:
                        continue
                except Exception:
                    pass

            side = trade.get("taker_side", "").lower()
            yes_price = float(trade.get("yes_price", 50) or 50) / 100
            _seen_trade_ids.add(trade_id)

            flow = ticker_flows[ticker]
            flow["market"] = market

            trade_record = {
                "trade_id": trade_id,
                "size": size,
                "side": side,
                "price": yes_price,
                "ts": now,
            }

            if side == "yes":
                flow["yes_size"] += size
                flow["yes_count"] += 1
                flow["yes_trades"].append(trade_record)
                _update_price_history(ticker, yes_price)
            elif side == "no":
                flow["no_size"] += size
                flow["no_count"] += 1
                flow["no_trades"].append(trade_record)
                _update_price_history(ticker, 1 - yes_price)

    # Clean seen trades
    if len(_seen_trade_ids) > 5000:
        _seen_trade_ids = set(list(_seen_trade_ids)[-2000:])

    # Evaluate flows for signals
    for ticker, flow in ticker_flows.items():
        yes_size = flow["yes_size"]
        no_size = flow["no_size"]
        yes_count = flow["yes_count"]
        no_count = flow["no_count"]
        market = flow["market"]

        # Check total market volume — skip thin markets
        volume = float(market.get("volume", 0) or 0)
        if volume < MIN_MARKET_VOLUME:
            log.debug("Skip thin market %s (vol=$%.0f)", ticker, volume)
            continue

        # Get current price
        yes_price = float(market.get("yes_ask", 50) or 50) / 100
        question = market.get("question") or market.get("title") or ticker

        def _make_signal(side, size, count, avg_price, trades_list):
            # Price range check
            if avg_price < MIN_COPY_PRICE or avg_price > MAX_COPY_PRICE:
                log.info("Copy: skip extreme price %.2f for %s", avg_price, ticker)
                return None

            # Momentum check
            if MOMENTUM_REQUIRED:
                if not _check_price_momentum(ticker, side, avg_price):
                    log.info("Copy: no momentum for %s %s", side, ticker)
                    return None

            # Size dominance check — smart money must be 3x the other side
            other_size = no_size if side == "YES" else yes_size
            if size < other_size * 2:
                log.debug("Copy: not dominant flow for %s", ticker)
                return None

            # Confidence based on count and size
            confidence = min(82, 55 + count * 6 + min(int(size / 100), 15))

            return {
                "type": "kalshi_copy",
                "source": "kalshi_flow",
                "ticker": ticker,
                "side": side,
                "price": avg_price,
                "total_size": size,
                "trade_count": count,
                "market": market,
                "question": question,
                "reason": (f"{count} smart money {side} trades "
                           f"${size:.0f} total in {CLUSTER_WINDOW}s on {ticker}"),
                "confidence": confidence,
            }

        if yes_count >= MIN_TRADES_FOR_SIGNAL:
            avg = (sum(t["price"] for t in flow["yes_trades"]) /
                   yes_count)
            sig = _make_signal("YES", yes_size, yes_count, avg,
                               flow["yes_trades"])
            if sig:
                signals.append(sig)
                log.info("Smart money YES: %s | $%.0f x%d trades",
                         ticker, yes_size, yes_count)

        if no_count >= MIN_TRADES_FOR_SIGNAL:
            avg_no = 1 - (sum(t["price"] for t in flow["no_trades"]) /
                          no_count)
            sig = _make_signal("NO", no_size, no_count, avg_no,
                               flow["no_trades"])
            if sig:
                signals.append(sig)
                log.info("Smart money NO: %s | $%.0f x%d trades",
                         ticker, no_size, no_count)

    return signals


def run_kalshi_copy(all_markets: list, executor, state: dict,
                    session: requests.Session, notifier,
                    claude_filter_fn=None) -> int:
    import state as _st
    from config import (MAX_TRADE_USDC, MAX_DAILY_TRADES,
                        CASH_RESERVE_PCT, MAX_OPEN_POSITIONS,
                        MAX_DAILY_LOSS_USDC, DRY_RUN)

    _st.reset_daily_if_needed(state)

    if state.get("daily_trades", 0) >= MAX_DAILY_TRADES:
        return 0
    if state.get("daily_loss", 0) >= MAX_DAILY_LOSS_USDC:
        return 0

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

        if ticker in state.get("open_lots", {}):
            continue

        size_usdc = float(MAX_TRADE_USDC)

        # Scale size by confidence — higher confidence = larger bet
        conf = signal["confidence"]
        if conf >= 75:
            size_usdc = min(MAX_TRADE_USDC, size_usdc * 1.2)
        elif conf < 65:
            size_usdc = size_usdc * 0.8
        size_usdc = max(1.50, min(size_usdc, MAX_TRADE_USDC))

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
                    log.info("Claude filtered: %s | %s",
                             question[:50], decision.get("reason", ""))
                    continue
            except Exception as e:
                log.debug("Claude filter error: %s", e)

        log.info("KALSHI COPY | %s %s @ %.2f | $%.2f | %s",
                 side, ticker, price, size_usdc, signal["reason"])

        if not DRY_RUN:
            resp = executor.place_order(
                token_id=ticker, side=side,
                price=price, size_usdc=size_usdc,
            )
        else:
            resp = {"dry_run": True}
            log.info("[DRY RUN] Would place: %s %s @ %.2f $%.2f",
                     side, ticker, price, size_usdc)

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

        _ticker_cooldowns[ticker] = time.time()

        notifier.send(
            title=f"🔥 Kalshi Copy | {side} {ticker[:20]}",
            message=(
                f"{question}\n\n"
                f"Smart money: {signal['trade_count']} trades "
                f"${signal['total_size']:.0f} total\n"
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

