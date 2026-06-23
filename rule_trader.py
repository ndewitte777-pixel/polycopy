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
MAX_DAILY_RULE_TRADES = 9999  # effectively unlimited; open-position cap controls risk

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
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
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

    # ── TOTAL GOALS LOGIC (pace-aware) ──────────────────────────
    # The main soccer line is typically O/U 2.5 goals.
    # We only bet UNDER/OVER when the GAME STATE clearly diverges from
    # the expected ~2.5 final, and never pre-game or too early.
    MAIN_LINE = 2.5

    if elapsed >= 30:  # need enough game played to judge pace
        # Project the final total from current pace
        goals_per_min = total_goals / max(elapsed, 1)
        projected_final = total_goals + goals_per_min * remaining
        # Blend pace projection with base rate (2.5) — early game trusts base more
        weight_pace = min(1.0, elapsed / 90)
        expected_final = projected_final * weight_pace + MAIN_LINE * (1 - weight_pace)

        # UNDER: only when projection is well below the next sensible line
        # AND we're late enough that few goals remain possible
        if elapsed >= 70 and total_goals <= 1:
            # The realistic line to bet UNDER is current_total + 1.5
            under_line = total_goals + 1.5  # e.g. 0 goals → UNDER 1.5, 1 goal → UNDER 2.5
            # Only bet if expected final is comfortably below the line
            if expected_final < under_line - 0.4:
                signals.append({
                    "market_type": "TOTAL",
                    "bet_side": "UNDER",
                    "team": None,
                    "confidence": min(76, 60 + int((90 - elapsed) * 0)  + int((under_line - expected_final) * 15)),
                    "reason": f"{total_goals} goals at {elapsed:.0f}min, "
                              f"projected final {expected_final:.1f} — UNDER {under_line} likely",
                    "preferred_line": under_line,
                })

        # OVER: only when pace is high AND projection clears the next line
        if elapsed >= 40 and total_goals >= 2:
            over_line = max(2.5, total_goals + 0.5)
            goals_per_90 = total_goals / elapsed * 90
            if goals_per_90 >= 3.5 and expected_final > over_line + 0.4:
                signals.append({
                    "market_type": "TOTAL",
                    "bet_side": "OVER",
                    "team": None,
                    "confidence": min(78, 58 + int((expected_final - over_line) * 12)),
                    "reason": f"{total_goals} goals at {elapsed:.0f}min ({goals_per_90:.1f}/90 pace), "
                              f"projected {expected_final:.1f} — OVER {over_line} likely",
                    "preferred_line": over_line,
                })

    # Rule: 2+ goal lead late → SPREAD (cover 1.5)
    if abs(scores[0] - scores[1]) >= 2 and elapsed >= 70:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "SPREAD",
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(80, 62 + int(elapsed / 6)),
            "reason": f"{names[leader_idx]} leads by {abs(scores[0]-scores[1])} "
                      f"at {elapsed:.0f}min — likely covers spread",
            "spread_line": 1.5,
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
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(92, 70 + score_diff),
            "reason": f"{names[leader_idx]} up {score_diff} pts with {clock:.1f}min left in 4th",
        })

    # ── TOTAL POINTS LOGIC (pace-aware) ─────────────────────────
    # NBA main line ~220.5, WNBA ~165.5. Project final from pace.
    is_wnba = game.get("sport") == "wnba" or "wnba" in str(game.get("league", "")).lower()
    main_line = 165.5 if is_wnba else 220.5
    game_mins = 40 if is_wnba else 48

    elapsed_mins = (period - 1) * (game_mins / 4) + ((game_mins / 4) - clock)
    if elapsed_mins >= 8 and period <= 3:
        pace_per_min = total_pts / max(elapsed_mins, 1)
        projected_final = pace_per_min * game_mins
        weight = min(1.0, elapsed_mins / game_mins)
        expected_final = projected_final * weight + main_line * (1 - weight)

        # Only bet when projection clearly diverges from main line
        if expected_final >= main_line + 12:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "OVER",
                "team": None,
                "confidence": min(74, 58 + int((expected_final - main_line) / 3)),
                "reason": f"Pace projects {expected_final:.0f} pts (line ~{main_line}) — OVER",
                "preferred_line": main_line,
            })
        elif expected_final <= main_line - 12:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "UNDER",
                "team": None,
                "confidence": min(74, 58 + int((main_line - expected_final) / 3)),
                "reason": f"Pace projects {expected_final:.0f} pts (line ~{main_line}) — UNDER",
                "preferred_line": main_line,
            })

    # Rule: Large lead in 4th → SPREAD cover
    if period >= 4 and score_diff >= 12 and clock <= 5:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "SPREAD",
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(82, 64 + score_diff),
            "reason": f"{names[leader_idx]} up {score_diff} with {clock:.1f}min left "
                      f"— likely covers spread",
            "spread_line": 5.5,
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
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(90, 72 + int(score_diff / 3)),
            "reason": f"{names[leader_idx]} up {score_diff} pts with {clock:.1f}min in 4th",
        })

    # ── TOTAL POINTS LOGIC (pace-aware) ─────────────────────────
    # NFL main line ~44.5. Project from pace, only bet on clear divergence.
    MAIN_LINE = 44.5
    if elapsed_mins >= 12 and period <= 3:
        pace_per_min = total_pts / max(elapsed_mins, 1)
        projected_final = pace_per_min * 60
        weight = min(1.0, elapsed_mins / 60)
        expected_final = projected_final * weight + MAIN_LINE * (1 - weight)

        if expected_final >= MAIN_LINE + 7:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "OVER",
                "team": None,
                "confidence": min(72, 56 + int((expected_final - MAIN_LINE) / 2)),
                "reason": f"Pace projects {expected_final:.0f} pts (line ~{MAIN_LINE}) — OVER",
                "preferred_line": MAIN_LINE,
            })
        elif expected_final <= MAIN_LINE - 7:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "UNDER",
                "team": None,
                "confidence": min(72, 56 + int((MAIN_LINE - expected_final) / 2)),
                "reason": f"Pace projects {expected_final:.0f} pts (line ~{MAIN_LINE}) — UNDER",
                "preferred_line": MAIN_LINE,
            })

    # Rule: Two-score lead late → SPREAD cover
    if period >= 4 and score_diff >= 11 and clock <= 6:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "SPREAD",
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(82, 66 + int(score_diff / 3)),
            "reason": f"{names[leader_idx]} up {score_diff} with {clock:.1f}min "
                      f"— likely covers spread",
            "spread_line": 6.5,
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
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(88, 65 + score_diff * 4),
            "reason": f"{names[leader_idx]} leads {scores[leader_idx]}-{scores[1-leader_idx]} "
                      f"in inning {period}",
        })

    # ── TOTAL RUNS LOGIC (pace-aware) ───────────────────────────
    # MLB main line is typically O/U 8.5 runs.
    # Project final runs from current pace and only bet when it
    # clearly diverges from the realistic line.
    MAIN_LINE = 8.5

    if 4 <= period <= 8:
        runs_per_inning = total_runs / max(period, 1)
        innings_left = 9 - period
        projected_final = total_runs + runs_per_inning * innings_left
        # Blend with base rate — earlier innings trust the base more
        weight = min(1.0, period / 9)
        expected_final = projected_final * weight + MAIN_LINE * (1 - weight)

        # OVER: high pace, projection clears a sensible line
        if total_runs >= 7 and period >= 5:
            over_line = max(8.5, total_runs + 0.5)
            if expected_final > over_line + 0.5:
                signals.append({
                    "market_type": "TOTAL",
                    "bet_side": "OVER",
                    "team": None,
                    "confidence": min(76, 58 + int((expected_final - over_line) * 6)),
                    "reason": f"{total_runs} runs through {period} "
                              f"(proj {expected_final:.1f}) — OVER {over_line} likely",
                    "preferred_line": over_line,
                })

        # UNDER: low pace late, projection well below line
        if total_runs <= 3 and period >= 6:
            under_line = max(7.5, total_runs + 4.5)
            if expected_final < under_line - 0.5:
                signals.append({
                    "market_type": "TOTAL",
                    "bet_side": "UNDER",
                    "team": None,
                    "confidence": min(74, 58 + int((under_line - expected_final) * 5)),
                    "reason": f"Only {total_runs} runs through {period} "
                              f"(proj {expected_final:.1f}) — UNDER {under_line} likely",
                    "preferred_line": under_line,
                })

    # Rule: Big late lead → SPREAD (likely to cover a small margin)
    if period >= 8 and score_diff >= 3:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "SPREAD",
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(80, 60 + score_diff * 4),
            "reason": f"{names[leader_idx]} leads by {score_diff} in inning {period} "
                      f"— likely to cover spread",
            "spread_line": 1.5,
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

    total_goals = sum(scores)

    if score_diff >= 2 and elapsed >= 40:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "WIN",
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(88, 68 + int(elapsed / 5)),
            "reason": f"{names[leader_idx]} leads {scores[leader_idx]}-{scores[1-leader_idx]} "
                      f"with {remaining:.0f}min left",
        })

    # ── TOTAL GOALS LOGIC (pace-aware) ──────────────────────────
    # NHL main line ~5.5 goals. Project final from pace.
    MAIN_LINE = 5.5
    if elapsed >= 20 and period <= 3:
        pace_per_min = total_goals / max(elapsed, 1)
        projected_final = pace_per_min * 60
        weight = min(1.0, elapsed / 60)
        expected_final = projected_final * weight + MAIN_LINE * (1 - weight)

        if expected_final >= MAIN_LINE + 1.2:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "OVER",
                "team": None,
                "confidence": min(72, 56 + int((expected_final - MAIN_LINE) * 6)),
                "reason": f"Pace projects {expected_final:.1f} goals (line ~{MAIN_LINE}) — OVER",
                "preferred_line": MAIN_LINE,
            })
        elif expected_final <= MAIN_LINE - 1.2:
            signals.append({
                "market_type": "TOTAL",
                "bet_side": "UNDER",
                "team": None,
                "confidence": min(72, 56 + int((MAIN_LINE - expected_final) * 6)),
                "reason": f"Pace projects {expected_final:.1f} goals (line ~{MAIN_LINE}) — UNDER",
                "preferred_line": MAIN_LINE,
            })

    # Rule: 2+ goal lead in 3rd period → SPREAD (puck line 1.5)
    # Empty-net goals often pad the lead late, making the cover likely
    if score_diff >= 2 and period >= 3:
        leader_idx = 0 if scores[0] > scores[1] else 1
        signals.append({
            "market_type": "SPREAD",
            "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
            "team": names[leader_idx],
            "confidence": min(80, 64 + int(elapsed / 6)),
            "reason": f"{names[leader_idx]} leads by {score_diff} in P{period} "
                      f"— empty-net often pads lead, covers 1.5",
            "spread_line": 1.5,
        })

    return signals


# ── Main dispatcher ─────────────────────────────────────────────────────────

def _tennis_rules(game: dict) -> list[dict]:
    """
    Tennis match rules based on sets and games won.
    A player up a set and a break is a strong favorite to win.
    Covers: match winner.
    """
    signals = []
    teams = game.get("teams", [])
    if len(teams) < 2:
        return signals

    # In tennis, ESPN "score" is sets won; linescores have game counts
    sets = [_tennis_parse(t.get("score", "0")) for t in teams]

    # Current set game scores from linescores if available
    p1_games = game.get("p1_games", 0)
    p2_games = game.get("p2_games", 0)

    set_diff = sets[0] - sets[1]
    leader_idx = 0 if sets[0] > sets[1] else 1

    # Best of 3 (most matches) vs best of 5 (Grand Slam men's)
    sets_to_win = 3 if game.get("best_of_5") else 2

    # Rule: A player who has won enough sets to be on match point
    if abs(set_diff) >= 1:
        leader_sets = sets[leader_idx]
        # If leader needs just one more set
        if leader_sets == sets_to_win - 1:
            confidence = 80 if abs(set_diff) >= 2 else 70
            signals.append({
                "market_type": "WIN",
                "bet_side": (teams[leader_idx].get("home_away","home") or "home").upper(),
                "team": teams[leader_idx].get("name"),
                "confidence": confidence,
                "reason": f"{teams[leader_idx].get('name')} leads {leader_sets} sets to {sets[1-leader_idx]}, one set from victory",
            })

    return signals


def _tennis_parse(s) -> int:
    """Parse tennis set score to int."""
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return 0


def _ufc_rules(game: dict) -> list[dict]:
    """
    UFC fight rules. Live in-fight betting is limited because fights
    can end suddenly (KO/submission). We only signal on clear dominance
    indicators if available, otherwise stay out.
    Covers: fight winner.

    NOTE: UFC is high-variance — a losing fighter can win instantly with
    one punch. We are conservative here and mostly rely on Claude + odds.
    """
    signals = []
    teams = game.get("teams", [])
    if len(teams) < 2:
        return signals

    period = int(game.get("period", 0) or 0)  # round number

    # UFC live data from ESPN is limited. We only act if ESPN flags a
    # clear winner indication (e.g. a fighter marked as winning).
    # Otherwise we let it go to Claude with whatever odds exist.
    for idx, t in enumerate(teams):
        if t.get("winner"):
            signals.append({
                "market_type": "WIN",
                "bet_side": "HOME" if idx == 0 else "AWAY",
                "team": t.get("name"),
                "confidence": 65,
                "reason": f"{t.get('name')} winning in round {period}",
            })
            break

    return signals


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
    "tennis_atp": _tennis_rules,
    "tennis_wta": _tennis_rules,
    "tennis": _tennis_rules,
    "ufc": _ufc_rules,
    "mma": _ufc_rules,
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
    global _daily_rule_trade_count, _daily_count_date, _game_cooldowns

    if not live_games or not all_kalshi_markets:
        return 0

    # HARD STOP: daily loss limit applies to ALL engines including rule trader
    from config import MAX_DAILY_LOSS_USDC, MAX_DAILY_TRADES
    if state.get("daily_loss", 0) >= MAX_DAILY_LOSS_USDC:
        log.warning("Rule trader halted: daily loss $%.2f >= limit $%.2f",
                    state.get("daily_loss", 0), MAX_DAILY_LOSS_USDC)
        return 0

    # HARD STOP: combined daily trade limit across all engines
    if state.get("daily_trades", 0) >= MAX_DAILY_TRADES:
        log.info("Rule trader halted: daily trade limit %d reached",
                 MAX_DAILY_TRADES)
        return 0

    # Balance floor — never trade below $2
    if state.get("bankroll", 25.0) < 2.00:
        log.warning("Rule trader halted: balance $%.2f too low",
                    state.get("bankroll", 0))
        return 0

    # Restore persisted cooldowns from state (survives restarts)
    now = time.time()
    for k, t in state.get("rule_cooldowns", {}).items():
        if now - t < GAME_COOLDOWN_SECONDS:
            _game_cooldowns.setdefault(k, t)

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

    # Stop trading if balance is critically low
    bankroll = state.get("bankroll", 25.0)
    if bankroll < 2.00:
        log.warning("Rule trader: balance $%.2f too low to trade safely — stopping", bankroll)
        return 0

    # Pre-rank live games by their strongest available signal so that when
    # slots are limited, the BEST opportunities across all games get filled
    # first — not just whichever game happened to be looped first.
    def _best_signal_conf(g):
        if not g.get("is_live"):
            return -1
        try:
            sigs = analyze_game(g)
            sigs = [s for s in sigs if s["confidence"] >= MIN_RULE_CONFIDENCE]
            return max((s["confidence"] for s in sigs), default=-1)
        except Exception:
            return -1

    live_games = sorted(live_games, key=_best_signal_conf, reverse=True)

    for game in live_games:
        if not game.get("is_live"):
            continue

        # Use date-aware game_id to distinguish series games
        # e.g. "LAA @ ATH_2026-06-21" not just "LAA @ ATH"
        game_id = game.get("game_id") or game.get("raw_name", "") or game.get("short_name", "")

        # Cooldown — skip if we recently traded this specific game
        if now - _game_cooldowns.get(game_id, 0) < GAME_COOLDOWN_SECONDS:
            continue

        # Safety limits — use the real configured values, not hardcoded
        from config import MAX_DAILY_LOSS_USDC, MAX_OPEN_POSITIONS
        if state.get("open_positions", 0) >= MAX_OPEN_POSITIONS:
            log.info("Rule trader: max open positions (%d) reached", MAX_OPEN_POSITIONS)
            break
        if state.get("daily_loss", 0) >= MAX_DAILY_LOSS_USDC:
            log.warning("Rule trader: daily loss limit hit ($%.2f >= $%.2f) — stopping",
                        state.get("daily_loss", 0), MAX_DAILY_LOSS_USDC)
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
            "mlb": ["KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD",
                    "KXMLBHIT", "KXMLBHR", "KXMLBKS", "KXMLBHRR", "KXMLBTB", "KXMLBRFI"],
            "nba": ["KXWNBAGAME", "KXWNBATOTAL", "KXWNBASPREAD", "KXWNBAPTS",
                    "KXNBAGAME", "KXNBATOTAL", "KXNBASPREAD", "KXNBAPTS"],
            "nfl": ["KXNFLGAME", "KXNFLTOTAL", "KXNFLSPREAD"],
            "nhl": ["KXNHLGAME", "KXNHLTOTAL", "KXNHLSPREAD"],
            "soccer": ["KXWCGAME", "KXWCTOTAL", "KXWCSPREAD", "KXWCBTTS", "KXWCGOAL"],
            "world_cup": ["KXWCGAME", "KXWCTOTAL", "KXWCSPREAD", "KXWCBTTS", "KXWCGOAL"],
            # Tennis — match winner markets
            "tennis_atp": ["KXATPMATCH"],
            "tennis_wta": ["KXWTAMATCH"],
            "tennis": ["KXATPMATCH", "KXWTAMATCH"],
            # UFC — fight winner markets
            "ufc": ["KXUFCFIGHT"],
            "mma": ["KXUFCFIGHT"],
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

                # CRITICAL: if teams/date don't match, skip entirely.
                # Don't let line-proximity bonuses rescue a wrong-game market.
                if match_score < 0.4:
                    continue

                if market_type == "TOTAL" and any(w in q for w in ["over", "under", "total", "goals", "runs", "points", "winner"]):
                    import re as _re2
                    ticker_line_match = _re2.search(r'-(\d+\.?\d*)$', ticker)
                    line_val = float(ticker_line_match.group(1)) if ticker_line_match else 999

                    current_total = sum(
                        float(t.get("score", 0) or 0)
                        for t in game.get("teams", [])
                    )

                    # Skip if betting OVER and line already beaten — market is at 0.99
                    # or betting UNDER and line already beaten — market is at 0.01
                    if bet_side == "OVER" and line_val <= current_total:
                        log.debug("Skip OVER %s — line %.1f already beaten (%d runs)",
                                  ticker, line_val, current_total)
                        continue
                    if bet_side == "UNDER" and line_val <= current_total:
                        log.debug("Skip UNDER %s — line %.1f already beaten (%d runs)",
                                  ticker, line_val, current_total)
                        continue

                    # Skip impossible lines
                    max_total = {
                        "soccer": 4.5, "world_cup": 4.5,
                        "mlb": 13.5, "nba": 235.5, "nhl": 7.5,
                    }.get(sport, 15.0)
                    if line_val > max_total:
                        continue

                    # Prefer line closest to preferred_line if signal specified one,
                    # otherwise closest to current score + buffer
                    pref = best.get("preferred_line", 0)
                    if pref:
                        # HARD GUARD: reject lines more than 1.0 from preferred.
                        # Prevents betting UNDER 1.5 when the rule wanted UNDER 2.5
                        if abs(line_val - pref) > 1.0:
                            continue
                        target_line = pref
                    else:
                        target_line = current_total + 2
                    proximity = 1 / (1 + abs(line_val - target_line))
                    # Strong boost when line matches preferred exactly
                    if pref and abs(line_val - pref) <= 0.25:
                        match_score += 0.5
                    else:
                        match_score += 0.20 + proximity * 0.15
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
                # Diagnostic: show what the best near-miss was so we can see why
                near_miss = ""
                try:
                    candidates = []
                    for m in expanded_markets:
                        tk = m.get("ticker", "")
                        if tk and tk.split("-")[0] in allowed_series:
                            qq = m.get("question", m.get("title", "")).lower()
                            sc = match_game_to_market(game, qq, tk)
                            if sc > 0:
                                candidates.append((sc, tk))
                    candidates.sort(reverse=True)
                    if candidates:
                        near_miss = " | top candidates: " + ", ".join(
                            f"{tk}({sc:.2f})" for sc, tk in candidates[:3])
                    else:
                        near_miss = f" | no {sport} markets matched any score (series={allowed_series[:3]})"
                except Exception as _e:
                    near_miss = f" | diag error: {_e}"
                log.info("Rule trader: no confident Kalshi match for %s (best=%.2f)%s",
                         game_id, best_score, near_miss)
                # Set a short cooldown so we don't spam this every 20s
                _game_cooldowns[game_id] = now - GAME_COOLDOWN_SECONDS + 120  # retry in 2 min
                continue

        ticker = target_market.get("ticker", "")
        if not ticker:
            continue

        # Identify market category for context (spread/prop need extra scrutiny)
        ticker_upper = ticker.upper()
        is_spread = "SPREAD" in ticker_upper
        is_prop = any(p in ticker_upper for p in
                      ["HIT", "HR", "KS", "HRR", "TB", "RFI", "PTS",
                       "GOAL", "3BALL", "BIRDIE", "EAGLE"])
        market_category = "SPREAD" if is_spread else ("PROP" if is_prop else "STANDARD")

        # Skip if we already have an open position on this ticker
        if ticker in state.get("open_lots", {}):
            log.debug("Rule trader: already have position on %s", ticker)
            continue

        # Skip if this ticker was recently traded (even after restart)
        if now - _game_cooldowns.get(ticker, 0) < GAME_COOLDOWN_SECONDS:
            log.debug("Rule trader: ticker %s in cooldown", ticker[:30])
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
            log.info("Rule trader: no real market question for %s — skipping", game_id)
            _game_cooldowns[game_id] = now - GAME_COOLDOWN_SECONDS + 120
            continue

        # Skip if this is a parlay leg (can't order individually)
        if target_market.get("_source") == "kalshi_parlay_leg":
            log.info("Rule trader: skipping parlay leg for %s", game_id)
            continue

        # Market price for live games — fetch a fresh real-time price.
        # The bulk/cached price is often missing ask/bid for live games and
        # defaults to 0.50, which is WRONG for a blowout. Always re-fetch.
        cached_yes = float(target_market.get("yes_price", 0) or 0)
        yes_price = None
        try:
            from kalshi_data import get_market_price
            fresh_yes, fresh_no = get_market_price(ticker)
            # 0.5 exactly is the failure signal from get_market_price
            if fresh_yes and fresh_yes != 0.5 and 0.005 < fresh_yes < 0.995:
                yes_price = fresh_yes
                log.debug("Fresh live price for %s: %.3f", ticker, fresh_yes)
        except Exception as e:
            log.debug("Could not fetch fresh price for %s: %s", ticker, e)

        # If fresh fetch failed, try the cached price (but not if it's the 0.5 default)
        if yes_price is None:
            if cached_yes and cached_yes != 0.5 and 0.005 < cached_yes < 0.995:
                yes_price = cached_yes
            else:
                # No reliable price — skip rather than trade on a fake 0.50.
                # This is critical: betting at a wrong price poisons the data.
                log.info("Rule trader: skipping %s — no reliable price "
                         "(fresh fetch failed, cached=%.2f)", game_id, cached_yes)
                _game_cooldowns[game_id] = now - GAME_COOLDOWN_SECONDS + 120
                continue

        # Convert to the price for OUR side
        if bet_side in ("NO", "AWAY", "UNDER"):
            market_price = 1 - yes_price
        else:
            market_price = yes_price

        # Skip extreme or low-value odds
        if market_price < MIN_BET_PRICE or market_price > MAX_BET_PRICE:
            log.info("Rule trader: skipping odds %.2f (outside %.2f-%.2f range) for %s",
                     market_price, MIN_BET_PRICE, MAX_BET_PRICE, game_id)
            continue

        # Minimum payout filter — reject trades where the upside is too small
        # relative to what's risked. payout_ratio = profit / amount risked.
        # At price 0.80 → win $0.20 per $0.80 risked = 0.25 ratio.
        # At price 0.95 → win $0.05 per $0.95 risked = 0.053 ratio → rejected.
        from config import MIN_PAYOUT_RATIO
        payout_ratio = (1 - market_price) / market_price if market_price > 0 else 0
        if payout_ratio < MIN_PAYOUT_RATIO:
            log.info("Rule trader: skipping %s — payout only %.0f%% "
                     "(need %.0f%%), price %.2f pays %.2fx",
                     game_id, payout_ratio * 100, MIN_PAYOUT_RATIO * 100,
                     market_price, 1 + payout_ratio)
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

        # Size UP to hit the minimum profit target if needed.
        # profit = size * payout_ratio, so size_needed = MIN_PROFIT / payout_ratio
        from config import MIN_PROFIT_USDC
        payout_ratio = (1 - market_price) / market_price if market_price > 0 else 0
        if payout_ratio > 0:
            size_for_profit = MIN_PROFIT_USDC / payout_ratio
            # Bump size up to reach the profit floor, but never past the hard cap
            size_usdc = max(size_usdc, size_for_profit)
            size_usdc = min(size_usdc, MAX_TRADE_USDC)

            # If even the max stake can't reach the profit floor, the trade isn't
            # worth it — skip rather than place an under-target bet
            max_possible_profit = size_usdc * payout_ratio
            if max_possible_profit < MIN_PROFIT_USDC - 0.01:
                log.info("Rule trader: skipping %s — max profit $%.2f at price %.2f "
                         "below $%.2f floor (would need $%.2f stake)",
                         game_id, max_possible_profit, market_price,
                         MIN_PROFIT_USDC, size_for_profit)
                continue

        try:
            spread_line_val = best.get("spread_line", 1.5)
            eval_result = sm.evaluate_signal(
                game=game,
                market_price=market_price,
                signal_type=market_type,
                signal_side=bet_side,
                session=requests.Session(),
                spread_line=spread_line_val,
            )
            true_prob = eval_result.get("true_prob", 0.5)
            ev_data = eval_result.get("ev", {})
            stats_reason = eval_result.get("reason", "")

            if not eval_result.get("should_trade", True):
                log.info("Stats model SKIP %s %s — %s",
                         game_id, market_type, stats_reason[:80])
                continue

            # ── Advanced edge detection ──────────────────────────
            advanced = sm.advanced_evaluate(
                game=game,
                market_price=market_price,
                signal_type=market_type,
                signal_side=bet_side,
                base_true_prob=true_prob,
            )

            # Update true_prob with advanced model
            true_prob = advanced["true_prob"]
            edge = advanced["edge"]

            # Boost confidence from advanced layers
            confidence = min(95, confidence + advanced["confidence_boost"])

            # Adjust trade size from advanced model
            size_usdc = min(size_usdc * advanced["size_mult"], MAX_TRADE_USDC)
            size_usdc = max(MIN_BET_SIZE, size_usdc)

            # Skip if advanced model says no edge
            if not advanced["should_bet"] and true_prob < 0.60:
                log.info(
                    "Advanced model SKIP %s — edge=%.1f%% true_prob=%.1f%%",
                    game_id, edge * 100, true_prob * 100
                )
                continue

            adv_reason = advanced["reasoning_str"]
            lag = advanced["market_lag"]

            log.info(
                "Edge: %.1f%% | True: %.1f%% | Market: %.1f%% | %s",
                edge * 100, true_prob * 100, market_price * 100, adv_reason
            )
            stats_reason = f"{stats_reason} | {adv_reason}" if stats_reason else adv_reason

            # Alert if strong market lag detected
            if lag.get("strength") in ("STRONG", "MODERATE"):
                log.info("⚡ MARKET LAG DETECTED: %s", lag.get("reason", ""))

        except Exception as e:
            log.debug("Stats model error: %s — using rule confidence", e)
            stats_reason = ""

        # Enforce minimum bet size — no tiny bets
        size_usdc = max(size_usdc, MIN_BET_SIZE)

        # Re-assert the minimum-profit stake (the advanced model's size_mult
        # above may have shrunk it). This is the final word on profit floor.
        from config import MIN_PROFIT_USDC
        _pr = (1 - market_price) / market_price if market_price > 0 else 0
        if _pr > 0:
            size_usdc = max(size_usdc, MIN_PROFIT_USDC / _pr)
            size_usdc = min(size_usdc, MAX_TRADE_USDC)

        # CASH RESERVE: never let total open exposure exceed (1 - reserve) of bankroll.
        # With $3-4 bets this keeps the account from being over-committed.
        try:
            from config import CASH_RESERVE_PCT
            bankroll = state.get("bankroll", 25.0)
            at_risk = state.get("total_at_risk", 0.0)
            max_exposure = bankroll * (1 - CASH_RESERVE_PCT)
            available = max_exposure - at_risk
            if available < MIN_BET_SIZE:
                log.info("Rule trader: cash reserve reached "
                         "($%.2f at risk, $%.2f cap) — holding", at_risk, max_exposure)
                break
            if size_usdc > available:
                # Trim to fit, but only if the trimmed bet still hits profit floor
                trimmed_profit = available * _pr if _pr > 0 else 0
                if trimmed_profit < MIN_PROFIT_USDC - 0.01:
                    log.info("Rule trader: skipping %s — only $%.2f available under "
                             "cash reserve, can't reach $%.2f profit floor",
                             game_id, available, MIN_PROFIT_USDC)
                    continue
                size_usdc = available
        except Exception as e:
            log.debug("Cash reserve check error: %s", e)

        # HOUSE MONEY: once the daily goal is hit, only risk the overflow.
        # E.g. goal $5, made $6.50 → only $1.50 is riskable.
        try:
            import profit_targets as pt
            net = pt.st.net_daily_pnl(state)
            target = pt.get_daily_target()
            if net >= target:
                overflow = net - target
                if overflow < MIN_BET_SIZE:
                    log.info("Daily goal $%.2f locked — overflow $%.2f below min bet, "
                             "stopping for the day", target, overflow)
                    break
                if overflow < size_usdc:
                    log.info("House money: capping bet to overflow $%.2f (goal $%.2f locked)",
                             overflow, target)
                    size_usdc = overflow
        except Exception as e:
            log.debug("Profit target check error: %s", e)

        # Default side mapping: YES for HOME/OVER/YES bets, NO for AWAY/UNDER/NO.
        # WIN markets refine this below using the ticker suffix; TOTAL/SPREAD/PROP
        # use this default directly. Setting it here ensures kalshi_side is always
        # defined regardless of market type.
        kalshi_side = "yes" if bet_side in ("YES", "HOME", "OVER") else "no"

        # For WIN markets, the ticker suffix tells us who YES is
        # e.g. KXMLBGAME-26JUN202210BOSSEA-SEA → suffix=SEA → YES=SEA wins
        # e.g. KXMLBGAME-26JUN202205LAAATH-LAA → suffix=LAA → YES=LAA wins
        if market_type == "WIN" and ticker:
            import re as _re_win
            suffix_match = _re_win.search(r'-([A-Z]{2,4})$', ticker.upper())
            if suffix_match:
                yes_team_code = suffix_match.group(1)  # e.g. "LAA", "SEA", "BOS"

                # Find which team in the game matches this code
                teams_in_game = game.get("teams", [])
                ALIASES = {"ARI": ["AZ"], "OAK": ["ATH"], "AZ": ["ARI"], "ATH": ["OAK"]}

                for t in teams_in_game:
                    abbr = t.get("abbreviation", "").upper()
                    codes = [abbr] + ALIASES.get(abbr, [])
                    if yes_team_code in codes:
                        # This team is the YES side
                        # Do we want this team to win?
                        team_home_away = t.get("home_away", "")
                        if (bet_side == "HOME" and team_home_away == "home") or \
                           (bet_side == "AWAY" and team_home_away == "away"):
                            kalshi_side = "yes"  # YES team is our pick
                        else:
                            kalshi_side = "no"   # YES team is NOT our pick
                        log.debug("WIN side: YES=%s pick=%s %s → %s",
                                  yes_team_code, abbr, bet_side, kalshi_side)
                        break

        # --- Claude filter on rule trades ---
        claude_reason = "filter disabled"
        claude_confidence = 0
        # Ask Claude to review with full statistical context before placing
        try:
            import claude_filter as _cf
            from config import USE_CLAUDE_FILTER
            if USE_CLAUDE_FILTER:
                from sports_data import format_game_context
                game_summary = format_game_context(game)
                edge_val = edge if 'edge' in dir() else 0.0
                true_prob_val = true_prob if 'true_prob' in dir() else 0.5

                decision = _cf.evaluate_rule_trade(
                    game_context=game_summary,
                    market_question=target_market.get("question", "?"),
                    market_price=market_price,
                    bet_side=f"{kalshi_side} ({bet_side})",
                    true_prob=true_prob_val,
                    edge=edge_val,
                    stats_reasoning=stats_reason or "no statistical model",
                    rule_reason=reason,
                    planned_size=size_usdc,
                    market_category=market_category,
                )
                if decision.get("decision") == "SKIP":
                    log.info(
                        "🚫 Claude SKIP: %s | conf=%d | %s",
                        target_market.get("question", "?")[:50],
                        decision.get("confidence", 0),
                        decision.get("reason", ""),
                    )
                    continue

                claude_confidence = decision.get("confidence", 0)
                claude_reason = decision.get("reason", "")
                claude_size_pct = decision.get("suggested_size_pct", 100)

                # Require minimum Claude confidence to proceed
                from config import CLAUDE_MIN_CONFIDENCE
                if claude_confidence < CLAUDE_MIN_CONFIDENCE:
                    log.info(
                        "🚫 Claude confidence too low (%d < %d): %s",
                        claude_confidence, CLAUDE_MIN_CONFIDENCE,
                        claude_reason[:60],
                    )
                    continue

                # Adjust size by Claude's suggestion
                if claude_size_pct < 100:
                    size_usdc = size_usdc * (claude_size_pct / 100)
                    size_usdc = max(size_usdc, MIN_BET_SIZE)

                log.info("✅ Claude APPROVED: conf=%d | size=%d%% | %s",
                         claude_confidence, claude_size_pct, claude_reason[:80])
        except Exception as e:
            log.debug("Claude filter error in rule trader: %s — proceeding", e)
            claude_confidence = 60
            claude_reason = reason or "filter error — proceeding on rules"

        log.info(
            "RULE TRADE | %s | %s %s | conf=%d | edge=%.1f%% | $%.2f\n"
            "  Rule: %s\n"
            "  Market: %s\n"
            "  Stats: %s\n"
            "  Claude: %s",
            game.get("short_name", "?"), market_type, bet_side,
            confidence, edge * 100 if 'edge' in dir() else 0,
            size_usdc, reason,
            target_market.get("question", "?")[:70],
            stats_reason[:120] if stats_reason else "no model",
            claude_reason[:100] if claude_reason else "n/a",
        )

        resp = executor.place_order(
            token_id=ticker,
            side=kalshi_side,
            price=market_price,
            size_usdc=size_usdc,
        )

        # Place a RESTING take-profit limit sell so the position banks profit
        # automatically when the price climbs — no polling/monitoring needed.
        # e.g. bought at 0.60 → resting sell at 0.80 fills on its own.
        take_profit_target = None
        try:
            from config import TAKE_PROFIT_MARGIN, TAKE_PROFIT_PRICE
            if TAKE_PROFIT_PRICE > 0:
                tp_target = TAKE_PROFIT_PRICE
            else:
                tp_target = market_price + TAKE_PROFIT_MARGIN
            # Cap target at 0.97 — can't sell above near-certainty
            tp_target = min(0.97, round(tp_target, 2))

            if tp_target > market_price:
                # Contracts held = size / entry price (each contract costs `price`)
                contracts = max(1, int(size_usdc / market_price))
                sell_resp = executor.place_limit_sell(
                    ticker=ticker,
                    position_side=kalshi_side.lower(),
                    target_price=tp_target,
                    count=contracts,
                )
                if sell_resp.get("resting") or sell_resp.get("dry_run"):
                    take_profit_target = tp_target
                    log.info("📌 Take-profit resting at %.2f (entry %.2f, +%.0f%% gain)",
                             tp_target, market_price,
                             ((tp_target - market_price) / market_price * 100))
        except Exception as e:
            log.debug("Take-profit placement error: %s", e)

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
            "take_profit_target": take_profit_target,
            "has_resting_sell": take_profit_target is not None,
        })
        open_lots[ticker] = lots
        state["open_positions"] = state.get("open_positions", 0) + 1
        state["daily_trades"] = state.get("daily_trades", 0) + 1
        state["total_at_risk"] = state.get("total_at_risk", 0.0) + size_usdc
        # Cooldown on both game_id AND ticker to prevent re-entry on same game/market
        _game_cooldowns[game_id] = now
        _game_cooldowns[ticker] = now
        # Persist to state so cooldowns survive bot restarts
        state.setdefault("rule_cooldowns", {})[game_id] = now
        state.setdefault("rule_cooldowns", {})[ticker] = now
        _daily_rule_trade_count += 1
        entries += 1

        # Record this trade in the journal so the dry-run produces real data.
        # Captures everything needed to later judge if the signal had an edge.
        try:
            import journal as _jnl
            _jnl.record_signal(state, {
                "type": "rule",
                "source": "rule_trader",
                "sport": game.get("sport", "?"),
                "game": game.get("short_name", "?"),
                "market": target_market.get("question", "?"),
                "ticker": ticker,
                "market_type": market_type,
                "side": kalshi_side,
                "price": round(market_price, 3),
                "size": round(size_usdc, 2),
                "true_prob": round(true_prob, 3) if 'true_prob' in dir() else None,
                "edge": round(edge, 3) if 'edge' in dir() else None,
                "claude_confidence": claude_confidence,
                "rule_confidence": confidence,
                "potential_profit": round(size_usdc * ((1 - market_price) / market_price), 2)
                                    if market_price > 0 else 0,
                "rule": reason[:100],
                "action": "placed",
                "timestamp": now,
            })
        except Exception as e:
            log.debug("Journal record error: %s", e)

        # Build comprehensive notification with all the math
        edge_str = f"{edge*100:+.1f}%" if 'edge' in dir() else "n/a"
        true_prob_str = f"{true_prob*100:.0f}%" if 'true_prob' in dir() else "n/a"
        try:
            ctx_lines = format_game_context(game).split("\n")
            game_score = ctx_lines[1] if len(ctx_lines) > 1 else game.get("short_name", "?")
        except Exception:
            game_score = game.get("short_name", "?")

        # Show both the live game AND the exact Kalshi market for verification
        notifier.send(
            title=f"📊 {kalshi_side} | {game.get('short_name','?')} | {claude_confidence}%",
            message=(
                f"🏟️ LIVE GAME: {game.get('short_name','?')}\n"
                f"📍 {game_score}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🎯 KALSHI MARKET:\n"
                f"{target_market.get('question','?')}\n"
                f"Ticker: {ticker}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💵 Bet: {kalshi_side} @ {market_price:.2f} | ${size_usdc:.2f}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📐 THE MATH:\n"
                f"• True probability: {true_prob_str}\n"
                f"• Market price: {market_price*100:.0f}%\n"
                f"• Edge: {edge_str}\n"
                f"• Stats: {stats_reason[:100] if stats_reason else 'rule-based'}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🤖 CLAUDE ({claude_confidence}% confident):\n"
                f"{claude_reason}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"⚙️ Rule trigger: {reason}"
            ),
        )

    return entries
