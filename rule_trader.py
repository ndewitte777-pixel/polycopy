"""
Rule-Based Live Trading Engine
================================
Makes live game betting decisions using pure statistical rules —
no Claude API calls, zero additional cost.
"""

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("polycopy.rule_trader")

# Minimum confidence to place a trade (0-100)
MIN_RULE_CONFIDENCE = 65

# Price range — only bet when there's real uncertainty and real upside
MIN_BET_PRICE = 0.20   # don't bet on anything cheaper than 20 cents (too speculative)
MAX_BET_PRICE = 0.80   # don't bet on anything more expensive than 80 cents (too little upside)

# Position limits — quality over quantity
MAX_OPEN_RULE_POSITIONS = 3   # max 3 open rule trader positions at once
MAX_DAILY_RULE_TRADES = 8     # max 8 rule trades per day

# Minimum dollar size per bet — no tiny bets
MIN_BET_SIZE = 1.50

# Cooldown per game to avoid overtrading
GAME_COOLDOWN_SECONDS = 600   # 10 minutes between bets on same game
_game_cooldowns: dict = {}
_daily_rule_trade_count = 0
_daily_count_date = ""


def _parse_score(score_str) -> int:
    try:
        return int(str(score_str).strip())
    except (ValueError, TypeError):
        return 0


def _parse_float(val, default=0.0) -> float:
    try:
        return float(str(val).replace("%", "").strip())
    except (ValueError, TypeError):
        return default


def _parse_clock_minutes(clock_str: str, sport: str) -> float:
    """Convert clock string to elapsed minutes."""
    try:
        clock_str = str(clock_str).strip().replace("'", "")
        if ":" in clock_str:
            parts = clock_str.split(":")
            mins = int(parts[0])
            secs = int(parts[1]) if len(parts) > 1 else 0
            return mins + secs / 60
        return float(clock_str)
    except Exception:
        return 0.0


# ── Sport-specific rule evaluators ─────────────────────────────────────────

def _soccer_rules(game: dict) -> list[dict]:
    """Returns list of {market_type, bet_side, confidence, reason}."""
    signals = []
    teams = game.get("teams", [])
    if len(teams) < 2:
        return signals

    scores = [_parse_score(t.get("score", 0)) for t in teams]
    names = [t.get("name", "?") for t in teams]
    period = int(game.get("period", 1) or 1)
    clock = _parse_clock_minutes(game.get("clock", "0"), "soccer")

    # Approximate elapsed minutes
    elapsed = (period - 1) * 45 + clock
    remaining = max(0, 90 - elapsed)

    score_diff = scores[0] - scores[1]
    total_goals = sum(scores)

    xg_str = game.get("xg", "")
    possession_str = game.get("possession", "")

    # Rule: Large lead late in game → safe WIN bet
    if abs(score_diff) >= 2 and elapsed >= 70:
        leader_idx = 0 if score_diff > 0 else 1
        leader = names[leader_idx]
        signals.append({
            "market_type": "WIN",
            "bet_side": "HOME" if leader_idx == 0 else "AWAY",
            "team": leader,
            "confidence": min(90, 65 + int(elapsed / 5)),
            "reason": f"{leader} leads {scores[leader_idx]}-{scores[1-leader_idx]} "
                      f"with {remaining:.0f}min left",
        })

    # Rule: Dominant possession with 0-0 scoreline → BUY win
    if total_goals == 0 and elapsed >= 55:
        poss = _parse_float(possession_str)
        if poss >= 65:
            # Team 0 has high possession
            signals.append({
                "market_type": "WIN",
                "bet_side": "HOME",
                "team": names[0],
                "confidence": 67,
                "reason": f"{names[0]} has {poss:.0f}% possession, 0-0 after {elapsed:.0f}min",
            })
        elif poss > 0 and poss <= 35:
            signals.append({
                "market_type": "WIN",
                "bet_side": "AWAY",
                "team": names[1],
                "confidence": 67,
                "reason": f"{names[1]} has {100-poss:.0f}% possession, 0-0 after {elapsed:.0f}min",
            })

    # Rule: Late 0-0 → UNDER total goals
    if total_goals == 0 and elapsed >= 80:
        signals.append({
            "market_type": "TOTAL",
            "bet_side": "UNDER",
            "team": None,
            "confidence": 75,
            "reason": f"0-0 with only {remaining:.0f}min left, UNDER 0.5 goals likely",
        })

    # Rule: Already high scoring → OVER (only fire in 2nd half with clear pace)
    if total_goals >= 3 and elapsed >= 60:
        goals_per_90 = total_goals / elapsed * 90
        if goals_per_90 >= 4.5:  # pace must justify the OVER
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "OVER",
                "team": None,
                "confidence": min(78, 60 + int(total_goals * 5)),
                "reason": f"{total_goals} goals in {elapsed:.0f}min ({goals_per_90:.1f}/90 pace) — OVER likely",
            })

    return signals


def _nba_rules(game: dict) -> list[dict]:
    signals = []
    teams = game.get("teams", [])
    if len(teams) < 2:
        return signals

    scores = [_parse_score(t.get("score", 0)) for t in teams]
    names = [t.get("name", "?") for t in teams]
    period = int(game.get("period", 1) or 1)
    clock = _parse_clock_minutes(game.get("clock", "12:00"), "nba")

    score_diff = abs(scores[0] - scores[1])
    total_pts = sum(scores)

    # Rule: Large lead in 4th quarter
    if period >= 4 and score_diff >= 15 and clock <= 6:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "WIN",
            "bet_side": "HOME" if leader_idx == 0 else "AWAY",
            "team": names[leader_idx],
            "confidence": min(92, 70 + score_diff),
            "reason": f"{names[leader_idx]} up {score_diff} pts with {clock:.1f}min left in 4th",
        })

    # Rule: Pace-based over/under
    # Expected: ~105 pts per team per 48 min = 210 total
    elapsed_mins = (period - 1) * 12 + (12 - clock)
    if elapsed_mins > 0 and period <= 3:
        pace = (total_pts / elapsed_mins) * 48
        if pace >= 230:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "OVER",
                "team": None,
                "confidence": 68,
                "reason": f"Scoring pace of {pace:.0f} pts/game suggests OVER",
            })
        elif pace <= 190:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "UNDER",
                "team": None,
                "confidence": 68,
                "reason": f"Scoring pace of {pace:.0f} pts/game suggests UNDER",
            })

    return signals


def _nfl_rules(game: dict) -> list[dict]:
    signals = []
    teams = game.get("teams", [])
    if len(teams) < 2:
        return signals

    scores = [_parse_score(t.get("score", 0)) for t in teams]
    names = [t.get("name", "?") for t in teams]
    period = int(game.get("period", 1) or 1)
    clock = _parse_clock_minutes(game.get("clock", "15:00"), "nfl")

    score_diff = abs(scores[0] - scores[1])
    total_pts = sum(scores)
    elapsed_mins = (period - 1) * 15 + (15 - clock)

    # Rule: Two-score lead in 4th quarter
    if period >= 4 and score_diff >= 14 and clock <= 8:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "WIN",
            "bet_side": "HOME" if leader_idx == 0 else "AWAY",
            "team": names[leader_idx],
            "confidence": min(90, 72 + int(score_diff / 3)),
            "reason": f"{names[leader_idx]} up {score_diff} pts with {clock:.1f}min in 4th",
        })

    # Rule: Pace for over/under (expected ~45 pts total)
    if elapsed_mins > 0 and period <= 3:
        pace = (total_pts / elapsed_mins) * 60
        if pace >= 55:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "OVER",
                "team": None,
                "confidence": 66,
                "reason": f"Scoring pace of {pace:.0f} pts/game suggests OVER",
            })
        elif pace <= 34:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "UNDER",
                "team": None,
                "confidence": 66,
                "reason": f"Scoring pace of {pace:.0f} pts/game suggests UNDER",
            })

    return signals


def _mlb_rules(game: dict) -> list[dict]:
    signals = []
    teams = game.get("teams", [])
    if len(teams) < 2:
        return signals

    scores = [_parse_score(t.get("score", 0)) for t in teams]
    names = [t.get("name", "?") for t in teams]
    period = int(game.get("period", 1) or 1)  # inning

    score_diff = abs(scores[0] - scores[1])
    total_runs = sum(scores)

    # Rule: Large lead after 7th inning (not earlier — games can change)
    if period >= 7 and score_diff >= 4:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "WIN",
            "bet_side": "HOME" if leader_idx == 0 else "AWAY",
            "team": names[leader_idx],
            "confidence": min(88, 65 + score_diff * 4),
            "reason": f"{names[leader_idx]} leads {scores[leader_idx]}-{scores[1-leader_idx]} "
                      f"in inning {period}",
        })

    # Rule: High run pace → OVER (only fire after inning 5)
    if period >= 5 and total_runs >= 8:
        pace = total_runs / period * 9
        signals.append({
            "market_type": "TOTAL",
            "bet_side": "OVER",
            "team": None,
            "confidence": min(75, 60 + int(total_runs)),
            "reason": f"{total_runs} runs through {period} innings ({pace:.1f}/9 pace) — OVER likely",
        })

    # Rule: Low scoring game → UNDER (only fire after inning 6)
    if period >= 6 and total_runs <= 1:
        signals.append({
            "market_type": "TOTAL",
            "bet_side": "UNDER",
            "team": None,
            "confidence": 72,
            "reason": f"Only {total_runs} runs through {period} innings — UNDER likely",
        })

    return signals


def _nhl_rules(game: dict) -> list[dict]:
    signals = []
    teams = game.get("teams", [])
    if len(teams) < 2:
        return signals

    scores = [_parse_score(t.get("score", 0)) for t in teams]
    names = [t.get("name", "?") for t in teams]
    period = int(game.get("period", 1) or 1)
    clock = _parse_clock_minutes(game.get("clock", "20:00"), "nhl")

    score_diff = abs(scores[0] - scores[1])
    elapsed = (period - 1) * 20 + (20 - clock)
    remaining = max(0, 60 - elapsed)

    if score_diff >= 2 and elapsed >= 40:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "WIN",
            "bet_side": "HOME" if leader_idx == 0 else "AWAY",
            "team": names[leader_idx],
            "confidence": min(88, 68 + int(elapsed / 5)),
            "reason": f"{names[leader_idx]} leads {scores[leader_idx]}-{scores[1-leader_idx]} "
                      f"with {remaining:.0f}min left",
        })

    return signals


# ── Main dispatcher ─────────────────────────────────────────────────────────

SPORT_RULES = {
    "soccer": _soccer_rules,
    "world_cup": _soccer_rules,
    "epl": _soccer_rules,
    "nba": _nba_rules,
    "nfl": _nfl_rules,
    "mlb": _mlb_rules,
    "nhl": _nhl_rules,
}


def analyze_game(game: dict) -> list[dict]:
    """Run rule-based analysis on a live game. Returns list of signals."""
    sport = game.get("sport", "soccer").lower()
    rule_fn = SPORT_RULES.get(sport)
    if not rule_fn:
        return []
    return rule_fn(game)


def _extract_single_legs(market: dict) -> list[dict]:
    """
    For Kalshi parlay markets, try to extract individual game legs
    that might match a single rule. Returns list of virtual single-leg markets.
    """
    title = market.get("title", "")
    ticker = market.get("ticker", "")
    close_time = market.get("close_time") or market.get("expiration_time")

    # Parse comma-separated legs from title
    # e.g. "yes Chicago C,yes Baltimore,yes Over 7.5 runs scored"
    legs = []
    if title and "," in title:
        parts = [p.strip() for p in title.split(",")]
        for part in parts:
            side = "YES"
            question = part
            if part.lower().startswith("yes "):
                side = "YES"
                question = part[4:].strip()
            elif part.lower().startswith("no "):
                side = "NO"
                question = part[3:].strip()

            if question:
                legs.append({
                    "ticker": ticker,
                    "question": question,
                    "yes_price": market.get("yes_price", 0.5),
                    "_hours_left": market.get("_hours_left", 24),
                    "_days_left": market.get("_days_left", 1),
                    "_source": "kalshi_parlay_leg",  # NOT a real orderable market
                    "_leg_side": side,
                    "_is_leg": True,  # Cannot place individual orders on this
                    "endDate": str(close_time) if close_time else "",
                    "liquidity": float(market.get("open_interest") or 0),
                })
    return legs


def run_rule_trader(live_games: list, all_kalshi_markets: list,
                    executor, state: dict, notifier) -> int:
    """
    Main entry — called from bot.py every LIVE_POLL_INTERVAL seconds.
    Uses pure rules, no Claude API.
    Returns number of positions opened.
    """
    global _daily_rule_trade_count, _daily_count_date

    if not live_games or not all_kalshi_markets:
        return 0

    # Reset daily count at midnight UTC
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _daily_count_date:
        _daily_rule_trade_count = 0
        _daily_count_date = today

    entries = 0
    now = time.time()

    from config import MAX_TRADE_USDC, KELLY_FRACTION
    from sports_data import match_game_to_market

    # Count current open rule trader positions
    open_lots = state.get("open_lots", {})
    rule_positions = sum(
        1 for lots in open_lots.values()
        for lot in (lots if isinstance(lots, list) else [lots])
        if lot.get("source") == "rule_trader"
    )

    if rule_positions >= MAX_OPEN_RULE_POSITIONS:
        log.debug("Rule trader: max open positions (%d) reached", MAX_OPEN_RULE_POSITIONS)
        return 0

    if _daily_rule_trade_count >= MAX_DAILY_RULE_TRADES:
        log.debug("Rule trader: daily limit (%d) reached", MAX_DAILY_RULE_TRADES)
        return 0

    for game in live_games:
        if not game.get("is_live"):
            continue

        game_id = game.get("raw_name", "") or game.get("short_name", "")

        # Cooldown
        if now - _game_cooldowns.get(game_id, 0) < GAME_COOLDOWN_SECONDS:
            continue

        # Safety limits
        if state.get("open_positions", 0) >= 10:
            break
        if state.get("daily_loss", 0) >= 100:
            break

        signals = analyze_game(game)
        if not signals:
            continue

        # Only bet on games that are actually IN PROGRESS
        # Prevent pre-game bets where we have no real edge
        game_clock = game.get("clock", "")
        game_status = game.get("status", "").lower()
        period = int(game.get("period", 0) or 0)

        is_in_progress = (
            period > 0 and
            game_status in ("in progress", "live", "active", "") and
            game.get("is_live", False)
        )
        if not is_in_progress:
            log.debug("Rule trader: skipping pre-game or finished game %s", game_id)
            continue

        # Filter signals by confidence threshold
        signals = [s for s in signals if s["confidence"] >= MIN_RULE_CONFIDENCE]
        if not signals:
            continue

        # Pick strongest signal
        best = max(signals, key=lambda s: s["confidence"])
        confidence = best["confidence"]
        market_type = best["market_type"]
        bet_side = best["bet_side"]
        reason = best["reason"]
        team = best.get("team")

        # Find matching Kalshi market — filter by sport first
        target_market = None
        best_score = 0.0

        sport = game.get("sport", "").lower()

        # Define which Kalshi series are relevant per sport
        SPORT_SERIES = {
            "mlb": ["KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD", "KXMLBHIT",
                    "KXMLBHR", "KXMLBKS", "KXMLBHRR", "KXMLBTB", "KXMLBRFI"],
            "nba": ["KXNBAGAME", "KXNBATOTAL", "KXNBASPREAD", "KXNBAPTS"],
            "nfl": ["KXNFLGAME", "KXNFLTOTAL", "KXNFLSPREAD"],
            "nhl": ["KXNHLGAME", "KXNHLTOTAL", "KXNHLSPREAD"],
            "soccer": ["KXWCGAME", "KXWCTOTAL", "KXWCSPREAD", "KXWCBTTS",
                       "KXWCGOAL", "KXSOCEPL", "KXSOCUCL"],
            "world_cup": ["KXWCGAME", "KXWCTOTAL", "KXWCSPREAD", "KXWCBTTS", "KXWCGOAL"],
            "nba": ["KXWNBAGAME", "KXWNBATOTAL", "KXWNBASPREAD", "KXWNBAPTS"],
        }

        allowed_series = SPORT_SERIES.get(sport, [])

        # Build expanded market list including parlay legs
        expanded_markets = list(all_kalshi_markets)
        for m in all_kalshi_markets:
            if m.get("title", "").count(",") >= 1:
                expanded_markets.extend(_extract_single_legs(m))

        for m in expanded_markets:
            ticker = m.get("ticker", "")

            # Filter by sport series if we know the sport
            if allowed_series and ticker:
                series_prefix = ticker.split("-")[0]
                if series_prefix not in allowed_series:
                    continue

            q = m.get("question", m.get("title", "")).lower()
            # Pass ticker for abbreviation-based matching
            match_score = match_game_to_market(game, q, ticker)

            if market_type == "TOTAL" and any(w in q for w in ["over", "under", "total", "goals", "runs", "points"]):
                match_score += 0.25
            elif market_type == "SPREAD" and "spread" in q:
                match_score += 0.25
            elif market_type == "WIN" and not any(w in q for w in ["spread", "over", "under", "total", "goal", "hit", "strikeout"]):
                match_score += 0.15

            if team and team.lower().split()[-1] in q:
                match_score += 0.35

            if match_score > best_score:
                best_score = match_score
                target_market = m

        if not target_market or best_score < 0.4:
            log.info("Rule trader: no confident Kalshi match for %s (best=%.2f)", game_id, best_score)
            continue

        ticker = target_market.get("ticker", "")
        if not ticker:
            continue

        # Skip if this is a parlay ticker or extracted leg from a parlay
        parlay_keywords = ["MULTIGAME", "EXTENDED", "CROSSCATEGORY", "KXMVE"]
        if any(kw in ticker.upper() for kw in parlay_keywords):
            log.info("Rule trader: skipping parlay ticker %s for %s", ticker[:30], game_id)
            continue

        # Skip if this is an extracted leg (has _is_leg flag) - can't order individually
        if target_market.get("_is_leg"):
            log.info("Rule trader: skipping parlay leg for %s — no single-game market found", game_id)
            continue

        # Skip if no real market question
        question = target_market.get("question", target_market.get("title", ""))
        if not question or question == "?":
            log.info("Rule trader: no real market question for %s", game_id)
            continue

        # Skip if this is a parlay leg (can't order individually)
        if target_market.get("_source") == "kalshi_parlay_leg":
            log.info("Rule trader: skipping parlay leg for %s", game_id)
            continue

        market_price = target_market.get("yes_price", 0.5)
        if bet_side in ("NO", "AWAY", "UNDER"):
            market_price = 1 - market_price

        # Skip extreme or low-value odds
        if market_price < MIN_BET_PRICE or market_price > MAX_BET_PRICE:
            log.info("Rule trader: skipping odds %.2f (outside %.2f-%.2f range) for %s",
                     market_price, MIN_BET_PRICE, MAX_BET_PRICE, game_id)
            continue

        # Simple Kelly sizing based on confidence edge
        implied_edge = (confidence / 100) - market_price
        if implied_edge <= 0:
            continue

        b = (1 - market_price) / market_price if market_price > 0 else 1
        my_prob = market_price + implied_edge
        kelly_f = max(0, (my_prob * (b + 1) - 1) / b) * KELLY_FRACTION
        bankroll = state.get("bankroll", 25.0)
        size_usdc = min(max(bankroll * kelly_f, 1.0), MAX_TRADE_USDC)

        kalshi_side = "YES" if bet_side in ("YES", "HOME", "OVER") else "NO"

        # Enforce minimum bet size — no tiny bets
        size_usdc = max(size_usdc, MIN_BET_SIZE)

        # --- Claude filter on rule trades ---
        # Ask Claude to review before placing any real money
        try:
            import claude_filter as _cf
            from config import USE_CLAUDE_FILTER
            if USE_CLAUDE_FILTER:
                from sports_data import format_game_context
                game_summary = format_game_context(game)
                market_info_for_filter = {
                    "question": target_market.get("question", "?"),
                    "outcome": kalshi_side,
                    "end_date": str(target_market.get("endDate", "")),
                    "liquidity": target_market.get("liquidity", 0),
                    "category": "SPORTS",
                    "url": f"https://kalshi.com/markets/{ticker}",
                    "extra_context": game_summary,
                }
                decision = _cf.evaluate_trade(
                    market_info=market_info_for_filter,
                    price=market_price,
                    your_size=size_usdc,
                    conviction=1,
                    num_wallets=1,
                )
                if decision.get("decision") == "SKIP":
                    log.info(
                        "Claude FILTERED rule trade: %s | conf=%d | %s",
                        target_market.get("question", "?")[:60],
                        decision.get("confidence", 0),
                        decision.get("reason", ""),
                    )
                    continue
                # Adjust size based on Claude's suggestion
                size_pct = decision.get("suggested_size_pct", 100)
                if size_pct < 100:
                    size_usdc = size_usdc * (size_pct / 100)
                    size_usdc = max(size_usdc, MIN_BET_SIZE)
        except Exception as e:
            log.debug("Claude filter error in rule trader: %s — proceeding", e)

        log.info(
            "RULE TRADE | %s | %s %s | conf=%d | $%.2f\n  %s\n  Market: %s",
            game.get("short_name", "?"), market_type, bet_side,
            confidence, size_usdc, reason,
            target_market.get("question", "?")[:70],
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
            "wallet": "rule_trader",
            "condition_id": ticker,
            "market_info": {
                "question": target_market.get("question", ""),
                "outcome": kalshi_side,
                "end_date": str(target_market.get("endDate", "")),
                "liquidity": target_market.get("liquidity", 0),
                "category": "SPORTS",
                "url": f"https://kalshi.com/markets/{ticker}",
            },
            "opened_at": now,
            "took_profit": False,
            "source": "rule_trader",
            "market_type": market_type,
        })
        open_lots[ticker] = lots
        state["open_positions"] = state.get("open_positions", 0) + 1
        _game_cooldowns[game_id] = now
        _daily_rule_trade_count += 1
        entries += 1

        notifier.send(
            title=f"📊 RULE {market_type} | {bet_side} | {game.get('short_name','?')}",
            message=(
                f"{target_market.get('question','?')}\n\n"
                f"Bet: {kalshi_side} @ {market_price:.3f}\n"
                f"Rule confidence: {confidence}%\n"
                f"Size: ${size_usdc:.2f}\n\n"
                f"{reason}"
            ),
        )

    return entries
