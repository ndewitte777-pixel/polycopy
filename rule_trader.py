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
                "preferred_line": 2.5,   # only match OVER 2.5, not 3.5+
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

def _pga_rules(game: dict) -> list[dict]:
    """
    PGA Tour rules based on leaderboard position.
    Covers: Win, Top 5/10/20, Make Cut, Score O/U, 3-Balls, Round Leader.
    """
    signals = []
    players = game.get("players", [])
    round_num = int(game.get("round", 0) or 0)

    if not players or round_num == 0:
        return signals

    def _parse_score(score_str: str) -> int:
        s = str(score_str).strip()
        if s == "E":
            return 0
        try:
            return int(s)
        except (ValueError, TypeError):
            return 0

    def _parse_position(pos_str: str) -> int:
        s = str(pos_str).strip().lstrip("T")
        try:
            return int(s)
        except (ValueError, TypeError):
            return 999

    leader = players[0] if players else {}
    leader_score = _parse_score(leader.get("score", "E"))
    leader_name = leader.get("name", "?")
    leader_last = leader.get("last_name", "?")

    # Calculate field average score for O/U
    scored_players = [p for p in players[:30]
                      if p.get("score") and p.get("score") != "?"]
    avg_score = (sum(_parse_score(p["score"]) for p in scored_players) /
                 len(scored_players)) if scored_players else 0

    for i, player in enumerate(players[:20]):
        name = player.get("name", "?")
        last_name = player.get("last_name", "?")
        pos = _parse_position(player.get("position", "99"))
        score = _parse_score(player.get("score", "E"))
        thru = player.get("thru", "F")
        shots_back = score - leader_score

        # ── Tournament Winner ──────────────────────────────────────
        if round_num >= 3 and pos == 1 and score <= -10:
            margin = score - _parse_score(players[1]["score"]) if len(players) > 1 else -2
            if margin <= -2:  # leading by 2+ shots
                signals.append({
                    "market_type": "WIN",
                    "bet_side": "YES",
                    "team": last_name,
                    "player_name": name,
                    "confidence": min(78, 65 + abs(margin) * 3),
                    "reason": f"{name} leads by {abs(margin)} at {score} after R{round_num}",
                })

        # ── Top 5 finish ──────────────────────────────────────────
        if round_num >= 2 and pos <= 3 and score <= -8:
            signals.append({
                "market_type": "TOP5",
                "bet_side": "YES",
                "team": last_name,
                "player_name": name,
                "confidence": 68,
                "reason": f"{name} T{pos} at {score} after R{round_num} — strong top 5 position",
            })

        # ── Top 10 finish ─────────────────────────────────────────
        if round_num >= 2 and pos <= 5 and score <= -6:
            signals.append({
                "market_type": "TOP10",
                "bet_side": "YES",
                "team": last_name,
                "player_name": name,
                "confidence": 67,
                "reason": f"{name} T{pos} at {score} after R{round_num}",
            })

        # ── Make the Cut ─────────────────────────────────────────
        if round_num == 2 and 25 <= pos <= 55 and -3 <= shots_back <= 1:
            signals.append({
                "market_type": "MAKECUT",
                "bet_side": "YES",
                "team": last_name,
                "player_name": name,
                "confidence": 66,
                "reason": f"{name} pos {pos} at {score} — on cut bubble, likely to make it",
            })

        # ── Round Leader (for next round) ─────────────────────────
        if round_num in (1, 2) and pos == 1 and score <= -6:
            next_round = round_num + 1
            r_lead_type = f"R{next_round}LEAD"
            signals.append({
                "market_type": r_lead_type,
                "bet_side": "YES",
                "team": last_name,
                "player_name": name,
                "confidence": 65,
                "reason": f"{name} leads R{round_num} at {score} — likely to hold lead in R{next_round}",
            })

        # ── 3-Ball Matchup — leader vs next two players ───────────
        if i == 0 and len(players) >= 3 and round_num >= 1:
            group = [players[0], players[1], players[2]]
            group_names = [p.get("name", "?") for p in group]
            group_scores = [_parse_score(p.get("score", "E")) for p in group]
            best_in_group_idx = group_scores.index(min(group_scores))
            if group_scores[best_in_group_idx] < min(group_scores[1:]) - 1:  # 2+ ahead
                winner = group[best_in_group_idx]
                signals.append({
                    "market_type": "3BALL",
                    "bet_side": "YES",
                    "team": winner.get("last_name", "?"),
                    "player_name": winner.get("name", "?"),
                    "group_players": group_names,
                    "confidence": 67,
                    "reason": f"{winner.get('name')} leads 3-ball group by 2+ shots",
                })

    # ── Score Total O/U ────────────────────────────────────────────
    # Bet UNDER if field is scoring well above par (scoring easier day)
    # Bet OVER if field is struggling (scoring harder day)
    if round_num >= 1 and len(scored_players) >= 10:
        if avg_score <= -4:  # field averaging -4 or better = scoring fest = UNDER
            signals.append({
                "market_type": "SCORE_UNDER",
                "bet_side": "NO",  # NO on OVER = UNDER
                "team": None,
                "player_name": None,
                "confidence": 66,
                "reason": f"Field averaging {avg_score:.1f} in R{round_num} — low scoring conditions, UNDER",
            })
        elif avg_score >= 0:  # field averaging over par = OVER
            signals.append({
                "market_type": "SCORE_OVER",
                "bet_side": "YES",
                "team": None,
                "player_name": None,
                "confidence": 65,
                "reason": f"Field averaging {avg_score:.1f} in R{round_num} — tough conditions, OVER",
            })

    return signals


def _pga_match_market(signal: dict, all_markets: list) -> tuple[dict | None, float]:
    """
    Find the right Kalshi PGA market for a signal.
    PGA markets include player name in ticker or question.
    """
    player_name = signal.get("player_name", "")
    last_name = signal.get("team", "")
    market_type = signal.get("market_type", "")
    group_players = signal.get("group_players", [])  # for 3-balls

    # Map market type to Kalshi series
    type_to_series = {
        "WIN": ["KXPGATOUR"],
        "TOP5": ["KXPGATOP5"],
        "TOP10": ["KXPGATOP10", "KXPGATOP20"],
        "TOP20": ["KXPGATOP20"],
        "MAKECUT": ["KXPGAMAKECUT"],
        "R1LEAD": ["KXPGAR1LEAD"],
        "R2LEAD": ["KXPGAR2LEAD"],
        "R3LEAD": ["KXPGAR3LEAD"],
        "SCORE_OVER": ["KXPGASCORE"],
        "SCORE_UNDER": ["KXPGASCORE"],
        "3BALL": ["KXPGA3BALL", "KXPGAGROUP"],
        "BIRDIE": ["KXPGABIRDIE"],
        "EAGLE": ["KXPGAEAGLE"],
        "HIO": ["KXPGAHIO"],
    }
    target_series = type_to_series.get(market_type, [])

    best_market = None
    best_score = 0.0

    for m in all_markets:
        ticker = m.get("ticker", "").upper()
        question = (m.get("question") or m.get("title") or "").lower()

        # Must be a PGA series
        ticker_series = ticker.split("-")[0]
        if target_series and ticker_series not in target_series:
            continue

        match_score = 0.0

        # Player name matching
        if last_name and last_name.lower() in question:
            match_score += 0.7
        if player_name:
            parts = player_name.lower().split()
            if any(p in question for p in parts if len(p) > 2):
                match_score += 0.3

        # 3-ball: match all players in group
        if market_type == "3BALL" and group_players:
            matched = sum(1 for p in group_players
                         if p.lower().split()[-1] in question)
            match_score = matched / len(group_players)

        # Score markets: match tournament name
        if market_type in ("SCORE_OVER", "SCORE_UNDER"):
            match_score = 0.5  # any score market is potentially relevant

        if match_score > best_score:
            best_score = match_score
            best_market = m

    return best_market, best_score


SPORT_RULES = {
    "soccer": _soccer_rules,
    "world_cup": _soccer_rules,
    "epl": _soccer_rules,
    "nba": _nba_rules,
    "nfl": _nfl_rules,
    "mlb": _mlb_rules,
    "nhl": _nhl_rules,
    "pga": _pga_rules,
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

        # Only bet on games actually in progress (not pre-game)
        # PGA is handled differently — check round number instead
        sport = game.get("sport", "").lower()
        if sport == "pga":
            round_num = int(game.get("round", 0) or 0)
            is_in_progress = round_num >= 1 and game.get("in_progress", False)
        else:
            game_clock = game.get("clock", "")
            game_status = game.get("status", "").lower()
            period = int(game.get("period", 0) or 0)
            is_in_progress = (
                period > 0 and
                game_status in ("in progress", "live", "active", "") and
                game.get("is_live", False)
            )

        if not is_in_progress:
            log.debug("Rule trader: skipping pre-game or finished: %s", game_id)
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
            "nba": ["KXNBAGAME", "KXNBATOTAL", "KXNBASPREAD", "KXNBAPTS",
                    "KXWNBAGAME", "KXWNBATOTAL", "KXWNBASPREAD", "KXWNBAPTS"],
            "nfl": ["KXNFLGAME", "KXNFLTOTAL", "KXNFLSPREAD"],
            "nhl": ["KXNHLGAME", "KXNHLTOTAL", "KXNHLSPREAD"],
            "soccer": ["KXWCGAME", "KXWCTOTAL", "KXWCSPREAD", "KXWCBTTS",
                       "KXWCGOAL", "KXSOCEPL", "KXSOCUCL"],
            "world_cup": ["KXWCGAME", "KXWCTOTAL", "KXWCSPREAD", "KXWCBTTS", "KXWCGOAL"],
            # PGA — all tournament markets
            "pga": ["KXPGATOUR",      # Tournament winner
                    "KXPGATOP5",       # Top 5 finish
                    "KXPGATOP10",      # Top 10 finish
                    "KXPGATOP20",      # Top 20 finish
                    "KXPGAMAKECUT",    # Make the cut
                    "KXPGAR1LEAD",     # Round 1 leader
                    "KXPGAR2LEAD",     # Round 2 leader
                    "KXPGAR3LEAD",     # Round 3 leader
                    "KXPGASCORE",      # Score totals (over/under)
                    "KXPGA3BALL",      # 3-ball matchups
                    "KXPGAGROUP",      # Group betting
                    "KXPGAHIO",        # Hole in one
                    "KXPGABIRDIE",     # Birdie or better
                    "KXPGAEAGLE",      # Eagle or better
                    "KXPGANAT",        # Nationality props
                    ],
        }

        allowed_series = SPORT_SERIES.get(sport, [])

        # PGA uses player-name matching, not team abbrev matching
        if sport == "pga":
            target_market, best_score = _pga_match_market(best, all_kalshi_markets)
            if not target_market or best_score < 0.4:
                log.info("Rule trader: no PGA market match for %s (score=%.2f)",
                         best.get("player_name", "?"), best_score)
                continue
        else:
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
                match_score = match_game_to_market(game, q, ticker)

                if market_type == "TOTAL" and any(w in q for w in ["over", "under", "total", "goals", "runs", "points"]):
                    # Check if this market's line matches our preferred line
                    import re as _re
                    line_match = _re.search(r'(\d+\.?\d*)\s*(goal|run|point|total)', q)
                    preferred = best.get("preferred_line", 0)
                    if line_match and preferred:
                        line_val = float(line_match.group(1))
                        # Max realistic totals by sport
                        max_total = {
                            "soccer": 2.5, "world_cup": 2.5,
                            "mlb": 9.5, "nba": 225.5, "nhl": 5.5,
                        }.get(sport, 5.5)
                        if line_val > max_total:
                            log.debug("Skip total %.1f (max %.1f) for %s",
                                     line_val, max_total, ticker)
                            continue
                        # Boost score if line matches preferred
                        if abs(line_val - preferred) <= 0.5:
                            match_score += 0.4
                        else:
                            match_score += 0.15
                    else:
                        match_score += 0.25
                elif market_type == "SPREAD" and "spread" in q:
                    # Filter spread lines — only realistic margins
                    # Extract the number from question e.g. "wins by more than 3.5" → 3.5
                    import re as _re
                    line_match = _re.search(r'(\d+\.?\d*)\s*(goal|run|point)', q)
                    if line_match:
                        line_val = float(line_match.group(1))
                        # Max spread lines by sport
                        max_spread = {
                            "soccer": 1.5, "world_cup": 1.5, "epl": 1.5,
                            "mlb": 1.5, "nba": 6.5, "nfl": 7.5, "nhl": 1.5,
                        }.get(sport, 2.5)
                        if line_val > max_spread:
                            log.debug("Skip spread %.1f (max %.1f) for %s",
                                     line_val, max_spread, ticker)
                            continue  # skip this market, too big a spread
                    match_score += 0.25
                elif market_type == "WIN" and not any(w in q for w in ["spread", "over", "under", "total", "goal", "hit", "strikeout"]):
                    match_score += 0.15

                if team and team.lower().split()[-1] in q:
                    match_score += 0.35

                if match_score > best_score:
                    best_score = match_score
                    target_market = m

            if not target_market or best_score < 0.4:
                log.info("Rule trader: no confident Kalshi match for %s (best=%.2f)",
                         game_id, best_score)
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

        # ── Statistical EV check ──────────────────────────────────
        # Evaluate signal using probability models + recent form
        try:
            import stats_models as sm
            eval_result = sm.evaluate_signal(
                game=game,
                market_price=market_price,
                signal_type=market_type,
                signal_side=bet_side,
                session=requests.Session(),
            )
            true_prob = eval_result.get("true_prob", 0.5)
            ev_data = eval_result.get("ev", {})
            stats_reason = eval_result.get("reason", "")

            # Require positive EV and minimum edge
            if not eval_result.get("should_trade", False):
                log.info(
                    "Rule trader: SKIP %s %s — no EV | %s",
                    game_id, market_type, stats_reason[:80]
                )
                continue

            # Boost confidence based on model
            model_conf = eval_result.get("confidence", confidence)
            confidence = int((confidence + model_conf) / 2)

            log.info(
                "Stats model: %s | edge=%+.1f%% | EV=%+.1f%% | "
                "true_prob=%.1f%% | market=%.1f%%",
                game_id,
                ev_data.get("edge", 0) * 100,
                ev_data.get("ev_pct", 0),
                true_prob * 100,
                market_price * 100,
            )

            # Size bet by Kelly criterion
            kelly = eval_result.get("quarter_kelly", 0)
            if kelly > 0:
                from config import YOUR_BANKROLL_USDC, MAX_TRADE_USDC
                bankroll = state.get("bankroll", YOUR_BANKROLL_USDC)
                kelly_size = bankroll * kelly
                size_usdc = min(kelly_size, MAX_TRADE_USDC,
                                size_usdc * 1.3 if kelly > 0.05 else size_usdc)
                size_usdc = max(MIN_BET_SIZE, size_usdc)

        except Exception as e:
            log.debug("Stats model error: %s — using rule confidence", e)
            stats_reason = ""

        # Enforce minimum bet size — no tiny bets
        size_usdc = max(size_usdc, MIN_BET_SIZE)

        kalshi_side = "YES" if bet_side in ("YES", "HOME", "OVER") else "NO"

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
