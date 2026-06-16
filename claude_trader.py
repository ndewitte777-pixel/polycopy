"""
Claude Autonomous Trading Engine
=================================
Independently scans ALL Polymarket categories for trade opportunities:
sports, politics, crypto, economics, pop culture, futures, and more.

Prioritizes same-day and next-day markets (70% of budget) since they
resolve fastest and allow the scalper to profit on price movements.
Longer-term markets get smaller allocations but are still traded.

Strategy:
1. Fetch active markets sorted by time horizon (soonest first)
2. Ask Claude: "What's your estimated true probability?"
3. If edge > 8%, size a bet using Kelly criterion
4. Track separately from copied trades
"""

import json
import time
import logging
import requests
from datetime import datetime, timezone

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLOB_API_URL,
    GAMMA_API_URL,
    DRY_RUN,
    YOUR_BANKROLL_USDC,
    KELLY_FRACTION,
    MAX_TRADE_USDC,
    MAX_DAILY_LOSS_USDC,
    SAME_DAY_SIZE_MULTIPLIER,
    NEXT_DAY_SIZE_MULTIPLIER,
    LONG_TERM_SIZE_MULTIPLIER,
    SHORT_TERM_BUDGET_PCT,
    CLAUDE_TRADER_MIN_HOURS_LEFT,
    CLAUDE_TRADER_MAX_DAYS_OUT,
)

log = logging.getLogger("polycopy.claude_trader")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = """You are an expert prediction market trader with deep knowledge of world events, politics, sports, economics, crypto, pop culture, and current affairs.

You trade ALL categories on Polymarket — sports, politics, crypto prices, economic indicators, entertainment, science, weather, futures, and anything else listed. No category is off limits as long as you have genuine knowledge-based edge.

You will be shown active Polymarket prediction markets. For each market, you must:
1. Estimate the TRUE probability of the YES outcome based on your knowledge
2. Compare it to the current market price (implied probability)
3. Identify if there is a meaningful edge (your estimate vs market price differs by >8%)
4. Decide whether to BET YES, BET NO, or PASS

IMPORTANT — Time horizon priority:
- Same-day markets (closes today): HIGHEST priority — these resolve fast, prices move during events
- Next-day markets (closes tomorrow): HIGH priority — still great for capturing event price moves
- Longer term (3-30 days): Lower priority — only bet if you have very strong knowledge edge
- Very short (< 1 hour left): SKIP unless you're extremely confident

Key principles:
- Only bet when you have genuine knowledge-based edge, not just hunches
- For live sports: consider current score, time remaining, momentum
- For crypto: consider recent price action and market sentiment you know about
- For politics: consider polls, historical patterns, recent developments
- For economics: consider recent data releases and Fed signals
- Short time + large edge = strongest opportunity
- Never bet on things you genuinely know nothing about

You must respond ONLY with a valid JSON object:
{
  "decision": "BET_YES" | "BET_NO" | "PASS",
  "my_probability": <float 0.0-1.0, your estimated true probability of YES>,
  "edge": <float, your_probability minus market_price, positive means YES has edge>,
  "confidence": <integer 0-100>,
  "reason": "<2-3 sentence explanation>",
  "suggested_size_pct": <integer 0-100, % of max trade size to use>,
  "time_horizon": "same_day" | "next_day" | "long_term"
}

If you PASS, set suggested_size_pct to 0. Be honest about uncertainty."""


MARKET_PROMPT_TEMPLATE = """Evaluate this Polymarket prediction market:

Question: {question}
Category: {category}
Current market price (YES): {price:.3f} (implied probability: {implied_pct:.1f}%)
Market closes: {end_date}
Time remaining: {time_remaining}
Liquidity: ${liquidity:,.0f} USDC
Volume: ${volume:,.0f} USDC

Additional context: {description}

What is your estimated true probability for YES? Is there a betting edge here?
Respond with JSON only."""


def fetch_active_markets(session: requests.Session, limit: int = 100,
                         min_liquidity: float = 500) -> tuple[list, list]:
    """
    Fetch active markets sorted by time horizon.
    Returns (short_term_markets, long_term_markets) where:
    - short_term: closes within 2 days (same day or next day)
    - long_term: closes in 2-30 days
    """
    markets = None
    for attempt in range(2):
        try:
            url = f"{GAMMA_API_URL}/markets"
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "endDate",
                "ascending": "true",
            }
            r = session.get(url, params=params, timeout=30)
            r.raise_for_status()
            markets = r.json()
            break
        except Exception as e:
            log.warning("Market fetch attempt %d failed: %s", attempt + 1, e)
            if attempt == 0:
                import time as _time
                _time.sleep(3)
                continue
            log.error("Failed to fetch active markets after retry: %s", e)
            return [], []

    if markets is None:
        return [], []

    now = datetime.now(timezone.utc)
    short_term = []
    long_term = []
    total_seen = 0
    skipped_liq = 0
    skipped_date = 0

    # Handle both list response and nested {"markets": [...]} response
    if isinstance(markets, dict):
        markets = markets.get("markets", markets.get("data", []))

    for m in markets:
        total_seen += 1
        liq = float(m.get("liquidity", 0) or 0)
        if liq < min_liquidity:
            skipped_liq += 1
            continue

        # Try multiple end date field names and formats
        end_date = (
            m.get("endDate") or m.get("end_date") or
            m.get("endDateIso") or m.get("gameStartTime") or ""
        )
        if not end_date:
            skipped_date += 1
            continue

        try:
            # Handle Unix timestamp (integer or string number)
            if isinstance(end_date, (int, float)) or (
                isinstance(end_date, str) and end_date.isdigit()
            ):
                end = datetime.fromtimestamp(int(end_date), tz=timezone.utc)
            else:
                # ISO string
                end = datetime.fromisoformat(
                    str(end_date).replace("Z", "+00:00").replace(" ", "T")
                )

            hours_left = (end - now).total_seconds() / 3600
            days_left = hours_left / 24

            if hours_left < CLAUDE_TRADER_MIN_HOURS_LEFT:
                log.debug("Skipping (too close/past): %s | %.1fh left",
                          m.get("question", "?")[:50], hours_left)
                continue
            if CLAUDE_TRADER_MAX_DAYS_OUT > 0 and days_left > CLAUDE_TRADER_MAX_DAYS_OUT:
                log.debug("Skipping (too far out): %s | %.1fd left",
                          m.get("question", "?")[:50], days_left)
                continue

            m["_hours_left"] = hours_left
            m["_days_left"] = days_left

            if days_left <= 2:
                short_term.append(m)
            else:
                long_term.append(m)
        except Exception as e:
            log.debug("Date parse failed for '%s': %s", end_date, e)
            continue

    log.info(
        "Markets: %d total, %d low-liq, %d no-date → %d same/next-day, %d longer-term",
        total_seen, skipped_liq, skipped_date,
        len(short_term), len(long_term),
    )

    # Log a sample of what was found to help debug
    for m in (short_term + long_term)[:3]:
        log.info("Sample market: '%s' | %.1fh left | liq=$%.0f",
                 m.get("question", "?")[:60],
                 m.get("_hours_left", 0),
                 float(m.get("liquidity", 0) or 0))

    return short_term, long_term


def time_horizon_multiplier(days_left: float) -> float:
    """Return size multiplier based on how soon the market closes."""
    if days_left <= 1:
        return SAME_DAY_SIZE_MULTIPLIER   # same day — biggest size
    elif days_left <= 2:
        return NEXT_DAY_SIZE_MULTIPLIER   # next day — slightly smaller
    else:
        return LONG_TERM_SIZE_MULTIPLIER  # longer term — smaller allocation


def time_horizon_label(days_left: float) -> str:
    if days_left <= 1:
        return "same_day"
    elif days_left <= 2:
        return "next_day"
    return "long_term"


def parse_price(market: dict) -> float:
    """Extract best YES price from market data."""
    # Try outcomePrices first (JSON string)
    raw = market.get("outcomePrices") or ""
    if isinstance(raw, str) and raw:
        try:
            prices = json.loads(raw)
            if isinstance(prices, list) and prices:
                return float(prices[0])
        except Exception:
            pass

    # Try bestBid/bestAsk midpoint
    bid = float(market.get("bestBid", 0) or 0)
    ask = float(market.get("bestAsk", 1) or 1)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2

    return 0.0


def parse_token_id(market: dict, side: str = "YES") -> str:
    """Extract CLOB token ID for YES or NO outcome."""
    raw = market.get("clobTokenIds") or ""
    if isinstance(raw, str):
        try:
            ids = json.loads(raw)
            if isinstance(ids, list):
                return str(ids[0]) if side == "YES" else str(ids[1]) if len(ids) > 1 else ""
        except Exception:
            pass
    return ""


def time_remaining_str(end_date: str) -> str:
    if not end_date:
        return "unknown"
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours = max(0, (end - now).total_seconds() / 3600)
        if hours < 24:
            return f"{hours:.1f} hours"
        return f"{hours/24:.1f} days"
    except Exception:
        return end_date


def ask_claude(market: dict, live_game_context: str = "") -> dict | None:
    """Ask Claude to evaluate a single market. Returns decision dict or None on error."""
    if not ANTHROPIC_API_KEY:
        return None

    question = market.get("question") or market.get("title", "")
    if not question:
        return None

    price = parse_price(market)
    if price <= 0 or price >= 1:
        return None

    end_date = market.get("endDate") or market.get("end_date", "")
    if end_date and "T" in end_date:
        end_date_display = end_date.split("T")[0]
    else:
        end_date_display = end_date

    category = ""
    tags = market.get("tags") or []
    if isinstance(tags, list) and tags:
        first = tags[0]
        if isinstance(first, dict):
            category = first.get("label") or first.get("slug", "")

    description = market.get("description") or ""
    if len(description) > 500:
        description = description[:500] + "..."

    liquidity = float(market.get("liquidity", 0) or 0)
    volume = float(market.get("volume", 0) or 0)

    # Add live game context if available
    live_context_str = ""
    if live_game_context:
        live_context_str = f"\n\nLIVE GAME DATA:\n{live_game_context}"

    prompt = MARKET_PROMPT_TEMPLATE.format(
        question=question,
        category=category or "General",
        price=price,
        implied_pct=price * 100,
        end_date=end_date_display,
        time_remaining=time_remaining_str(end_date),
        liquidity=liquidity,
        volume=volume,
        description=(description or "No additional description.") + live_context_str,
    )

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 300,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data["content"][0]["text"].strip()

        # Strip markdown fences
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        decision = json.loads(raw)
        decision.setdefault("decision", "PASS")
        decision.setdefault("my_probability", price)
        decision.setdefault("edge", 0.0)
        decision.setdefault("confidence", 0)
        decision.setdefault("reason", "")
        decision.setdefault("suggested_size_pct", 0)
        return decision

    except Exception as e:
        log.warning("Claude evaluation failed for '%s': %s", question[:50], e)
        return None


def kelly_size_from_edge(my_prob: float, price: float,
                          bankroll: float, side: str) -> float:
    """
    Kelly criterion sizing based on Claude's probability estimate vs market price.
    side: 'YES' or 'NO'
    """
    if side == "YES":
        p = my_prob
        b = (1 - price) / price  # decimal odds
    else:
        p = 1 - my_prob
        b = price / (1 - price)

    if b <= 0 or p <= 0:
        return 0.0

    f = (p * (b + 1) - 1) / b
    f = max(0.0, f * KELLY_FRACTION)
    return min(bankroll * f, MAX_TRADE_USDC)


def run_claude_trader(executor, state: dict, session: requests.Session,
                      your_bankroll: float, notifier) -> int:
    """
    Scan all Polymarket categories. Process short-term markets first
    with 70% of the trade budget, then longer-term with the remainder.
    """
    from sports_data import fetch_all_live_games, match_game_to_market, format_game_context

    log.info("Claude trader: scanning all Polymarket categories...")
    short_term, long_term = fetch_active_markets(session)

    if not short_term and not long_term:
        return 0

    # Fetch live game context once for all markets
    live_games = fetch_all_live_games(session)
    if live_games:
        log.info("Live game context available for %d games", len(live_games))

    # Budget: 70% of max daily trades go to short-term, 30% to long-term
    max_trades_total = 6  # max Claude-initiated trades per scan
    short_budget = max(1, int(max_trades_total * SHORT_TERM_BUDGET_PCT / 100))
    long_budget = max_trades_total - short_budget

    trades_placed = 0

    def process_market_list(markets: list, budget: int, label: str) -> int:
        nonlocal trades_placed
        placed = 0
        for market in markets:
            if placed >= budget:
                break
            if state.get("open_positions", 0) >= 10:
                log.warning("Max open positions reached")
                break
            if state.get("daily_loss", 0) >= MAX_DAILY_LOSS_USDC:
                log.warning("Daily loss limit hit")
                break

            time.sleep(1.5)  # rate limit Claude API

            question = market.get("question") or market.get("title", "")
            price = parse_price(market)
            if price <= 0 or price >= 1:
                continue

            days_left = market.get("_days_left", 5)
            hours_left = market.get("_hours_left", 99)

            # Live game context
            live_context = ""
            for game in live_games:
                score = match_game_to_market(game, question)
                if score >= 0.4:
                    live_context = format_game_context(game)
                    break

            decision = ask_claude(market, live_context)
            if not decision:
                continue

            d = decision["decision"].upper()
            confidence = decision.get("confidence", 0)
            my_prob = decision.get("my_probability", price)
            edge = decision.get("edge", 0)
            reason = decision.get("reason", "")
            size_pct = decision.get("suggested_size_pct", 0)

            if d == "PASS" or confidence < 60 or abs(edge) < 0.08:
                log.info(
                    "Claude PASS [%s]: '%s' | price=%.3f my_prob=%.3f edge=%.3f conf=%d",
                    label, question[:60], price, my_prob, edge, confidence,
                )
                continue

            if d == "BET_YES":
                side = "YES"
                token_id = parse_token_id(market, "YES")
                bet_price = price
            elif d == "BET_NO":
                side = "NO"
                token_id = parse_token_id(market, "NO")
                bet_price = 1 - price
            else:
                continue

            if not token_id:
                continue

            # Size = Kelly * time horizon multiplier * suggested_size_pct
            base_size = kelly_size_from_edge(my_prob, price, your_bankroll, side)
            horizon_mult = time_horizon_multiplier(days_left)
            size_usdc = base_size * horizon_mult * (size_pct / 100 if size_pct else 1.0)
            size_usdc = max(1.0, min(size_usdc, MAX_TRADE_USDC))

            hours_str = f"{hours_left:.1f}h" if hours_left < 48 else f"{days_left:.1f}d"
            log.info(
                "Claude TRADE [%s | %s] | %s | BET_%s @ %.3f | "
                "my_prob=%.3f edge=%.3f conf=%d size=$%.2f | %s",
                label, hours_str, question[:55], side, bet_price,
                my_prob, edge, confidence, size_usdc, reason,
            )

            resp = executor.place_order(
                token_id=token_id, side="BUY",
                price=bet_price, size_usdc=size_usdc,
            )

            end_date = market.get("endDate") or market.get("end_date", "")
            if end_date and "T" in end_date:
                end_date = end_date.split("T")[0]
            slug = market.get("slug") or ""
            market_url = f"https://polymarket.com/event/{slug}" if slug else ""

            category = ""
            tags = market.get("tags") or []
            if isinstance(tags, list) and tags and isinstance(tags[0], dict):
                category = tags[0].get("label") or tags[0].get("slug", "")

            market_info = {
                "question": question,
                "outcome": side,
                "end_date": end_date,
                "liquidity": float(market.get("liquidity", 0) or 0),
                "category": category,
                "url": market_url,
            }

            open_lots = state.setdefault("open_lots", {})
            lots = open_lots.get(token_id, [])
            lots.append({
                "entry_price": bet_price,
                "size_usdc": size_usdc,
                "peak_price": bet_price,
                "wallet": "claude_ai",
                "condition_id": market.get("conditionId", ""),
                "market_info": market_info,
                "opened_at": time.time(),
                "took_profit": False,
                "source": "claude_autonomous",
                "time_horizon": label,
            })
            open_lots[token_id] = lots
            state["open_positions"] = state.get("open_positions", 0) + 1

            horizon_emoji = "⚡" if label == "same_day" else "📅" if label == "next_day" else "🗓️"
            import notifier as _notifier
            _notifier.send(
                title=f"🤖{horizon_emoji} Claude BET {side} [{label}]",
                message=(
                    f"{question}\n\n"
                    f"Betting {side} @ {bet_price:.3f} "
                    f"(implied {bet_price*100:.1f}%)\n"
                    f"My estimate: {my_prob*100:.1f}% | Edge: {edge:+.1%}\n"
                    f"Size: ${size_usdc:.2f} | Confidence: {confidence}%\n"
                    f"Closes in: {hours_str}\n"
                    f"{reason}\n{market_url}"
                ),
            )

            placed += 1
            trades_placed += 1

        return placed

    # Process short-term first (same day and next day)
    log.info("Processing %d short-term markets (budget: %d trades)", len(short_term), short_budget)
    process_market_list(short_term, short_budget, "same_day/next_day")

    # Then longer-term with remaining budget
    log.info("Processing %d long-term markets (budget: %d trades)", len(long_term), long_budget)
    process_market_list(long_term, long_budget, "long_term")

    log.info("Claude trader done: %d trades placed total", trades_placed)
    return trades_placed
