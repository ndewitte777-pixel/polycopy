"""
Claude Autonomous Trading Engine
=================================
Independently scans Polymarket for trade opportunities and places bets
based on Claude's own analysis and predictions.

Runs on a separate interval from the copy engine (default every 4 hours).

Strategy:
1. Fetch active markets from Gamma API (high liquidity, closing soon or active)
2. For each market, ask Claude: "What's your estimated true probability?"
3. If Claude's estimate differs significantly from the market price (edge),
   size a bet using Kelly criterion on the edge
4. Place the order and track it separately from copied trades

Claude is given the market question, current price, end date, category,
and any relevant context it can reason about from its training.
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
)

log = logging.getLogger("polycopy.claude_trader")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = """You are an expert prediction market trader with deep knowledge of world events, politics, sports, economics, and current affairs.

You will be shown active Polymarket prediction markets. For each market, you must:
1. Estimate the TRUE probability of the YES outcome based on your knowledge
2. Compare it to the current market price (implied probability)
3. Identify if there is a meaningful edge (your estimate vs market price differs by >8%)
4. Decide whether to BET YES, BET NO, or PASS

Key principles:
- Only bet when you have genuine knowledge-based edge, not just hunches
- Markets are often efficient — be humble, only bet when confident
- Consider: Is this something you have reliable knowledge about? Is your information likely more accurate than the market?
- Short time to close + large edge = strong opportunity
- Never bet on markets you genuinely don't know enough about

You must respond ONLY with a valid JSON object:
{
  "decision": "BET_YES" | "BET_NO" | "PASS",
  "my_probability": <float 0.0-1.0, your estimated true probability of YES>,
  "edge": <float, your_probability minus market_price, positive means YES has edge>,
  "confidence": <integer 0-100>,
  "reason": "<2-3 sentence explanation of your reasoning>",
  "suggested_size_pct": <integer 0-100, % of max trade size to use>
}

If you PASS, set suggested_size_pct to 0.
Be honest about uncertainty. PASS is often the right answer."""


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


def fetch_active_markets(session: requests.Session, limit: int = 50,
                         min_liquidity: float = 5000) -> list:
    """
    Fetch active markets from Gamma API sorted by liquidity.
    Filters to markets closing within the next 30 days with decent liquidity.
    """
    try:
        url = f"{GAMMA_API_URL}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "liquidity",
            "ascending": "false",
        }
        r = session.get(url, params=params, timeout=15)
        r.raise_for_status()
        markets = r.json()

        # Filter by liquidity and reasonable end date
        now = datetime.now(timezone.utc)
        filtered = []
        for m in markets:
            liq = float(m.get("liquidity", 0) or 0)
            if liq < min_liquidity:
                continue
            end_date = m.get("endDate") or m.get("end_date", "")
            if end_date:
                try:
                    end = datetime.fromisoformat(
                        end_date.replace("Z", "+00:00")
                    )
                    days_left = (end - now).total_seconds() / 86400
                    if days_left < 0 or days_left > 30:
                        continue
                    m["_days_left"] = days_left
                except Exception:
                    pass
            filtered.append(m)

        log.info("Fetched %d active markets for Claude analysis", len(filtered))
        return filtered

    except Exception as e:
        log.error("Failed to fetch active markets: %s", e)
        return []


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
    Main entry point called from bot.py on each Claude trading interval.
    Scans markets, asks Claude for opinions, places bets on high-edge opportunities.
    Returns number of trades placed.
    """
    from sports_data import fetch_all_live_games, match_game_to_market, format_game_context

    log.info("Claude trader: scanning markets...")
    markets = fetch_active_markets(session)
    if not markets:
        return 0

    # Fetch all live games for context
    live_games = fetch_all_live_games(session)
    log.info("Found %d live games for context", len(live_games))

    trades_placed = 0
    skipped = 0
    passed = 0

    for market in markets:
        time.sleep(1.5)

        question = market.get("question") or market.get("title", "")
        price = parse_price(market)
        if price <= 0 or price >= 1:
            continue

        # Match live game context to this market
        live_context = ""
        best_match_score = 0.0
        for game in live_games:
            score = match_game_to_market(game, question)
            if score > best_match_score:
                best_match_score = score
                if score >= 0.4:
                    live_context = format_game_context(game)

        decision = ask_claude(market, live_context)
        if not decision:
            continue

        d = decision["decision"].upper()
        confidence = decision.get("confidence", 0)
        my_prob = decision.get("my_probability", price)
        edge = decision.get("edge", 0)
        reason = decision.get("reason", "")
        size_pct = decision.get("suggested_size_pct", 0)

        # Skip PASS or low confidence
        if d == "PASS" or confidence < 60 or abs(edge) < 0.08:
            passed += 1
            log.info(
                "Claude PASS: '%s' | price=%.3f my_prob=%.3f edge=%.3f conf=%d",
                question[:60], price, my_prob, edge, confidence,
            )
            continue

        # Determine side and token
        if d == "BET_YES":
            side = "YES"
            token_id = parse_token_id(market, "YES")
            bet_price = price
        elif d == "BET_NO":
            side = "NO"
            token_id = parse_token_id(market, "NO")
            bet_price = 1 - price  # NO token price
        else:
            passed += 1
            continue

        if not token_id:
            log.warning("Could not get token_id for market: %s", question[:50])
            continue

        # Size using Kelly on the edge
        size_usdc = kelly_size_from_edge(my_prob, price, your_bankroll, side)
        size_usdc = size_usdc * (size_pct / 100) if size_pct else size_usdc
        size_usdc = max(1.0, min(size_usdc, MAX_TRADE_USDC))

        # Check open positions limit
        if state.get("open_positions", 0) >= 10:
            log.warning("Max open positions reached, Claude trader pausing")
            break

        # Check daily loss
        if state.get("daily_loss", 0) >= state.get("max_daily_loss", 100):
            log.warning("Daily loss limit hit, Claude trader pausing")
            break

        log.info(
            "Claude TRADE | %s | BET_%s @ %.3f | my_prob=%.3f edge=%.3f "
            "conf=%d size=$%.2f | %s",
            question[:60], side, bet_price, my_prob, edge,
            confidence, size_usdc, reason,
        )

        resp = executor.place_order(
            token_id=token_id,
            side="BUY",
            price=bet_price,
            size_usdc=size_usdc,
        )

        # Track in state
        end_date = market.get("endDate") or market.get("end_date", "")
        if end_date and "T" in end_date:
            end_date = end_date.split("T")[0]

        slug = market.get("slug") or ""
        market_url = f"https://polymarket.com/event/{slug}" if slug else ""

        market_info = {
            "question": question,
            "outcome": side,
            "end_date": end_date,
            "liquidity": float(market.get("liquidity", 0) or 0),
            "category": "",
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
        })
        open_lots[token_id] = lots
        state["open_positions"] = state.get("open_positions", 0) + 1

        import notifier as _notifier
        _notifier.send(
            title=f"🤖 Claude AI Trade | BET {side}",
            message=(
                f"{question}\n\n"
                f"Betting {side} @ {bet_price:.3f} "
                f"(implied {bet_price*100:.1f}%)\n"
                f"My estimate: {my_prob*100:.1f}% | Edge: {edge:+.1%}\n"
                f"Size: ${size_usdc:.2f} | Confidence: {confidence}%\n"
                f"{reason}\n"
                f"{market_url}"
            ),
            priority=0,
        )

        trades_placed += 1

    log.info(
        "Claude trader done: %d trades placed, %d passed, %d skipped/error",
        trades_placed, passed, skipped,
    )
    return trades_placed
