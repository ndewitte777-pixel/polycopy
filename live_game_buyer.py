"""
Live Game Momentum Buyer
========================
Watches live games every 20 seconds and looks for betting opportunities
based on in-game momentum, not just copy trading or scheduled scans.

Covers:
- WIN markets: team dominating but market hasn't caught up yet
- SPREAD markets: team covering/not covering based on performance
- TOTALS markets: over/under based on pace of scoring

Supported sports: Soccer, NBA, NFL, MLB, NHL, UFC

Strategy:
- Pull live game stats (score, possession, shots, xG, pace)
- Match to open Kalshi markets for that game
- Ask Claude: "Given what's happening in this game, is there a bet?"
- If Claude sees edge > 8%, enter a position sized by confidence
- Scalper handles the exit

Claude gets full game context including:
- Current score and time remaining
- Possession %, shots on target, xG (soccer)
- Field goal %, turnovers, pace (NBA)
- Yards per play, red zone trips (NFL)
- Batting avg, ERA, pitch count (MLB)
- Strikes landed, takedown % (UFC)
"""

import json
import time
import logging
import requests
from datetime import datetime, timezone

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    MAX_TRADE_USDC,
    KELLY_FRACTION,
    DRY_RUN,
)

log = logging.getLogger("polycopy.live_buyer")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Min seconds between buying on the same game (avoid chasing)
GAME_COOLDOWN_SECONDS = 300  # 5 minutes
# Min edge for Claude to trigger a buy
MIN_EDGE = 0.08

# Track which games we've recently bought to avoid overtrading
_game_cooldowns: dict = {}  # game_id -> timestamp of last buy


SYSTEM_PROMPT = """You are an expert live sports betting analyst with deep knowledge of all major sports.

You are watching a LIVE game and must identify if there is a profitable betting opportunity RIGHT NOW based on what is actually happening in the game — not just the score, but the momentum, statistics, and how the game is being played.

You can recommend bets on:
1. WIN/MONEYLINE: Which team will win
2. SPREAD: Will the favorite cover the spread (win by more than X)
3. TOTAL: Will total score be OVER or UNDER the line

Key principles:
- A dominant team that hasn't scored yet is often mispriced — markets react to scores, not possession/shots/xG
- A team that just scored often sees their win probability overstated — momentum can shift
- Late-game situations: large leads are usually safe, but sports are unpredictable in final minutes
- Consider fatigue, key player situations, historical patterns for this sport
- Spreads are harder to cover when protecting a lead (teams play conservatively)
- Totals: consider pace of play, whether defenses are tiring, weather (outdoor sports)

You must respond ONLY with valid JSON:
{
  "has_edge": true or false,
  "market_type": "WIN" or "SPREAD" or "TOTAL" or null,
  "bet_side": "YES" or "NO" or "OVER" or "UNDER" or "HOME" or "AWAY" or null,
  "my_probability": <float 0-1, your estimate of this outcome>,
  "market_price": <float 0-1, the current market implied probability>,
  "edge": <float, your_probability minus market_price>,
  "confidence": <integer 0-100>,
  "reasoning": "<2-3 sentences explaining what you see in the game data>",
  "suggested_size_pct": <integer 0-100>,
  "urgency": "high" or "medium" or "low"
}

If no edge: set has_edge=false, all other fields null/0.
Be conservative — only flag genuine edges, not hunches."""


def build_game_prompt(game: dict, kalshi_markets: list) -> str:
    """Build a concise prompt giving Claude game context and available markets."""
    sport = game.get("sport", "unknown").upper()
    teams = game.get("teams", [])
    clock = game.get("clock", "")
    period = game.get("period", "")
    status = game.get("status", "")

    # Compact score line
    score_parts = []
    for t in teams:
        score_parts.append(f"{t.get('name','?')} {t.get('score','0')} ({t.get('home_away','')})")
    score_line = " vs ".join(score_parts)

    # Recent plays (max 2)
    plays = game.get("recent_plays", [])[-2:]
    plays_str = "; ".join(f"{p.get('period','')} {p.get('clock','')}: {p.get('text','')}" for p in plays)
    if not plays_str:
        plays_str = "None"

    # Markets (max 4, keep short)
    market_lines = []
    for m in kalshi_markets[:4]:
        q = m.get("question", "?")[:60]
        yes = m.get("yes_price", 0.5)
        ticker = m.get("ticker", "?")
        market_lines.append(f"- {q} | YES={yes:.2f} | {ticker}")
    markets_str = "\n".join(market_lines) if market_lines else "No matching markets"

    return f"""LIVE {sport} GAME:
{score_line}
Time: {clock} | Period: {period} | {status}
Recent scoring: {plays_str}

AVAILABLE KALSHI MARKETS:
{markets_str}

Is there a profitable bet right now? Respond with JSON only."""

    # Get extended stats if available
    teams = game.get("teams", [])
    team_names = [t.get("name", "?") for t in teams]
    scores = [t.get("score", "0") for t in teams]

    # Build stats section based on sport
    stats_lines = []

    # Soccer-specific
    if sport in ("SOCCER", "WORLD_CUP", "EPL"):
        stats_lines.extend([
            f"Possession: {game.get('possession', 'N/A')}",
            f"Shots on target: {game.get('shots_on_target', 'N/A')}",
            f"Total shots: {game.get('total_shots', 'N/A')}",
            f"xG (expected goals): {game.get('xg', 'N/A')}",
            f"Corners: {game.get('corners', 'N/A')}",
        ])

    # NBA-specific
    elif sport == "NBA":
        stats_lines.extend([
            f"FG%: {game.get('fg_pct', 'N/A')}",
            f"3P%: {game.get('three_pt_pct', 'N/A')}",
            f"Turnovers: {game.get('turnovers', 'N/A')}",
            f"Rebounds: {game.get('rebounds', 'N/A')}",
            f"Pace (pts per 100 poss): {game.get('pace', 'N/A')}",
        ])

def _format_recent_plays(game: dict) -> str:
    plays = game.get("recent_plays", [])
    if not plays:
        return "No recent scoring plays"
    return "\n".join(
        f"- {p.get('period','')} {p.get('clock','')}: {p.get('text','')}"
        for p in plays
    )


def fetch_extended_stats(game_id: str, sport: str,
                         session: requests.Session) -> dict:
    """
    Fetch extended in-game statistics from ESPN.
    Returns a dict of stats to augment the basic game data.
    """
    stats = {}
    try:
        from sports_data import ESPN_BASE
        sport_path = {
            "soccer": "soccer/all",
            "world_cup": "soccer/fifa.world",
            "nba": "basketball/nba",
            "nfl": "football/nfl",
            "mlb": "baseball/mlb",
            "nhl": "hockey/nhl",
            "ufc": "mma/ufc",
        }.get(sport, "soccer/all")

        url = f"{ESPN_BASE}/{sport_path}/summary"
        r = session.get(url, params={"event": game_id},
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Extract team stats
        box_score = data.get("boxscore", {})
        teams_data = box_score.get("teams", [])

        for team_data in teams_data:
            stats_list = team_data.get("statistics", [])
            for stat in stats_list:
                name = stat.get("name", "").lower().replace(" ", "_")
                val = stat.get("displayValue", stat.get("value", ""))
                stats[name] = val

        # Soccer xG from advanced stats
        adv = data.get("advancedStats", {})
        if adv:
            stats["xg"] = adv.get("expectedGoals", "N/A")

    except Exception as e:
        log.debug("Extended stats fetch failed for %s: %s", game_id, e)

    return stats


def match_kalshi_markets(game: dict, all_kalshi_markets: list) -> list:
    """
    Find Kalshi markets that match this live game.
    Uses team names and sport keywords to match.
    """
    from sports_data import match_game_to_market
    matched = []
    for market in all_kalshi_markets:
        question = market.get("question", "")
        score = match_game_to_market(game, question)
        if score >= 0.3:
            matched.append((score, market))

    # Sort by match score, return top matches
    matched.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in matched[:8]]


def ask_claude_live(game: dict, kalshi_markets: list) -> dict | None:
    """Ask Claude if there's a live betting opportunity."""
    if not ANTHROPIC_API_KEY:
        return None

    prompt = build_game_prompt(game, kalshi_markets)

    for attempt in range(2):
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
                    "max_tokens": 350,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=20,
            )
            resp.raise_for_status()
            content = resp.json().get("content", [])
            if not content or not content[0].get("text", "").strip():
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None

            raw = content[0]["text"].strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]

            result = json.loads(raw.strip())
            return result

        except Exception as e:
            log.warning("Live buyer Claude call failed (attempt %d): %s", attempt + 1, e)
            if attempt == 0:
                time.sleep(2)
                continue
            return None
    return None


def run_live_buyer(live_games: list, all_kalshi_markets: list,
                   executor, state: dict,
                   session: requests.Session, notifier) -> int:
    """
    Main entry — called from bot.py alongside the scalper.
    Looks for momentum-based entry opportunities in live games.
    Returns number of positions opened.
    """
    if not live_games or not all_kalshi_markets:
        return 0

    entries = 0
    now = time.time()

    for game in live_games:
        if not game.get("is_live"):
            continue

        game_id = game.get("raw_name", "") or game.get("short_name", "")

        # Cooldown check — don't keep buying into the same game
        last_buy = _game_cooldowns.get(game_id, 0)
        if now - last_buy < GAME_COOLDOWN_SECONDS:
            continue

        # Check position limits
        if state.get("open_positions", 0) >= 10:
            break
        if state.get("daily_loss", 0) >= state.get("max_daily_loss", 100):
            break

        # Find matching Kalshi markets
        matched_markets = match_kalshi_markets(game, all_kalshi_markets)
        if not matched_markets:
            continue

        # Fetch extended stats for better analysis
        sport = game.get("sport", "soccer")
        game_with_stats = {**game}
        if hasattr(session, "get"):
            extra_stats = fetch_extended_stats(game_id, sport, session)
            game_with_stats.update(extra_stats)

        # Ask Claude
        analysis = ask_claude_live(game_with_stats, matched_markets)
        if not analysis:
            continue

        if not analysis.get("has_edge"):
            log.info(
                "Live buyer: no edge in %s | %s",
                game.get("short_name", "?"),
                analysis.get("reasoning", "")[:100],
            )
            continue

        edge = float(analysis.get("edge", 0))
        confidence = int(analysis.get("confidence", 0))
        market_type = analysis.get("market_type", "WIN")
        bet_side = analysis.get("bet_side", "YES")
        my_prob = float(analysis.get("my_probability", 0.5))
        market_price = float(analysis.get("market_price", 0.5))
        size_pct = int(analysis.get("suggested_size_pct", 50))
        urgency = analysis.get("urgency", "medium")
        reasoning = analysis.get("reasoning", "")

        if abs(edge) < MIN_EDGE or confidence < 55:
            log.info(
                "Live buyer: edge too small (%.3f) or low confidence (%d) | %s",
                edge, confidence, game.get("short_name", "?"),
            )
            continue

        # Find the best matching market for this bet type
        target_market = None
        for m in matched_markets:
            q = m.get("question", "").lower()
            mt = (market_type or "").lower()
            if mt == "spread" and "spread" in q:
                target_market = m
                break
            elif mt == "total" and any(w in q for w in ["over", "under", "total", "goals", "points", "runs"]):
                target_market = m
                break
            elif mt == "win" and not any(w in q for w in ["spread", "over", "under", "total"]):
                target_market = m
                break

        if not target_market:
            target_market = matched_markets[0]  # fallback to best match

        ticker = target_market.get("ticker", "")
        if not ticker:
            continue

        # Size by Kelly + confidence
        your_bankroll = state.get("bankroll", 25.0)
        b = (1 - market_price) / market_price if market_price > 0 else 1
        kelly_f = max(0, (my_prob * (b + 1) - 1) / b) * KELLY_FRACTION
        base_size = your_bankroll * kelly_f * (size_pct / 100)
        size_usdc = min(max(base_size, 1.0), MAX_TRADE_USDC)

        # Urgency multiplier
        if urgency == "high":
            size_usdc = min(size_usdc * 1.3, MAX_TRADE_USDC)

        kalshi_side = "YES" if bet_side in ("YES", "HOME", "OVER") else "NO"

        log.info(
            "LIVE BUY | %s | %s %s @ %.3f | edge=%.3f conf=%d size=$%.2f\n"
            "  Market: %s\n  Reasoning: %s",
            game.get("short_name", "?"), market_type, bet_side,
            market_price, edge, confidence, size_usdc,
            target_market.get("question", "?")[:70],
            reasoning,
        )

        resp = executor.place_order(
            token_id=ticker,
            side=kalshi_side,
            price=market_price,
            size_usdc=size_usdc,
        )

        # Track position
        open_lots = state.setdefault("open_lots", {})
        lots = open_lots.get(ticker, [])
        lots.append({
            "entry_price": market_price,
            "size_usdc": size_usdc,
            "peak_price": market_price,
            "wallet": "live_buyer",
            "condition_id": ticker,
            "market_info": {
                "question": target_market.get("question", ""),
                "outcome": kalshi_side,
                "end_date": target_market.get("endDate", ""),
                "liquidity": target_market.get("liquidity", 0),
                "category": "SPORTS",
                "url": f"https://kalshi.com/markets/{ticker}",
            },
            "opened_at": now,
            "took_profit": False,
            "source": "live_buyer",
            "market_type": market_type,
        })
        open_lots[ticker] = lots
        state["open_positions"] = state.get("open_positions", 0) + 1
        _game_cooldowns[game_id] = now
        entries += 1

        # Notify
        from sports_data import format_game_context
        game_summary = format_game_context(game)
        notifier.send(
            title=f"⚡ LIVE {market_type} | {bet_side} | {game.get('short_name','?')}",
            message=(
                f"{target_market.get('question','?')}\n\n"
                f"Bet: {kalshi_side} @ {market_price:.3f} ({market_price*100:.0f}%)\n"
                f"My estimate: {my_prob*100:.0f}% | Edge: {edge:+.1%}\n"
                f"Size: ${size_usdc:.2f} | Confidence: {confidence}% | {urgency.upper()}\n\n"
                f"{reasoning}\n\n"
                f"{game_summary}"
            ),
        )

    return entries
