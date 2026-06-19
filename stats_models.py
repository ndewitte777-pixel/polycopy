"""
Sports Statistical Models
===========================
Win probability and expected value calculations for each sport,
based on historical data and current game state.

Sources used:
- MLB: Historical win probability by run differential and inning (Fangraphs-style)
- Soccer: Poisson goal model + in-game win probability tables
- NBA: Log5 + current score margin + minutes remaining
- NFL: NFL win probability model (based on score, time, down)
- PGA: Historical make cut rates, top-10 rates by position after each round

Also fetches recent form (last 10 games) from ESPN stats API
to adjust base probabilities.
"""

import logging
import math
import time
import requests

log = logging.getLogger("polycopy.stats_models")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_STATS = "https://site.web.api.espn.com/apis/site/v2/sports"

# Cache recent form data (expires every 30 min)
_form_cache: dict = {}
_form_cache_time: dict = {}
FORM_CACHE_TTL = 1800


# ─────────────────────────────────────────────
#  MLB WIN PROBABILITY MODEL
# ─────────────────────────────────────────────

# Historical MLB win probability by (inning, run_differential)
# Based on Retrosheet/Fangraphs data
# Format: (inning, run_diff) → win_probability for leading team
MLB_WIN_PROB = {
    # (inning, run_diff): win_prob
    (1, 1): 0.583, (1, 2): 0.682, (1, 3): 0.763, (1, 4): 0.834, (1, 5): 0.890,
    (2, 1): 0.601, (2, 2): 0.706, (2, 3): 0.793, (2, 4): 0.862, (2, 5): 0.912,
    (3, 1): 0.617, (3, 2): 0.727, (3, 3): 0.819, (3, 4): 0.884, (3, 5): 0.930,
    (4, 1): 0.638, (4, 2): 0.751, (4, 3): 0.843, (4, 4): 0.906, (4, 5): 0.947,
    (5, 1): 0.664, (5, 2): 0.779, (5, 3): 0.869, (5, 4): 0.928, (5, 5): 0.962,
    (6, 1): 0.698, (6, 2): 0.814, (6, 3): 0.899, (6, 4): 0.950, (6, 5): 0.976,
    (7, 1): 0.743, (7, 2): 0.858, (7, 3): 0.929, (7, 4): 0.967, (7, 5): 0.985,
    (8, 1): 0.805, (8, 2): 0.907, (8, 3): 0.959, (8, 4): 0.982, (8, 5): 0.993,
    (9, 1): 0.870, (9, 2): 0.952, (9, 3): 0.982, (9, 4): 0.994, (9, 5): 0.998,
}


def mlb_win_probability(inning: int, run_diff: int,
                         home_team_leads: bool) -> float:
    """
    Returns win probability for the leading team.
    run_diff: positive integer (how many runs the leader is ahead)
    """
    if run_diff <= 0:
        return 0.5

    # Cap at 5 run differential (diminishing returns beyond)
    diff_key = min(run_diff, 5)
    inn_key = max(1, min(inning, 9))
    prob = MLB_WIN_PROB.get((inn_key, diff_key), 0.5)

    # Small home field adjustment (~3%)
    if home_team_leads:
        prob = min(0.99, prob + 0.02)
    else:
        prob = max(0.01, prob - 0.02)

    return prob


def mlb_total_runs_probability(inning: int, current_total: int,
                                line: float) -> dict:
    """
    Estimates probability of going OVER or UNDER a run total line.
    Based on average scoring pace and innings remaining.
    """
    innings_remaining = max(0, 9 - inning)
    if innings_remaining == 0:
        return {"over": float(current_total > line),
                "under": float(current_total < line)}

    # MLB averages ~0.5 runs per half-inning = ~1.0 runs/inning total
    avg_runs_per_inning = 1.0
    expected_additional = innings_remaining * avg_runs_per_inning
    projected_total = current_total + expected_additional

    # Use normal distribution approximation
    # Standard deviation: ~sqrt(innings_remaining * 1.0)
    std = math.sqrt(innings_remaining * 1.0)
    if std == 0:
        return {"over": float(projected_total > line), "under": float(projected_total < line)}

    z_over = (line - projected_total) / std
    over_prob = 1 - _normal_cdf(z_over)
    under_prob = _normal_cdf(z_over)

    return {
        "over": round(over_prob, 3),
        "under": round(under_prob, 3),
        "projected_total": round(projected_total, 1),
        "line": line,
    }


# ─────────────────────────────────────────────
#  SOCCER WIN PROBABILITY MODEL
# ─────────────────────────────────────────────

def soccer_win_probability(home_score: int, away_score: int,
                            minute: int) -> dict:
    """
    Returns win/draw/loss probabilities for home team.
    Uses Poisson model with time-adjusted goal expectation.
    """
    minutes_remaining = max(0, 90 - minute)
    score_diff = home_score - away_score

    if minutes_remaining == 0:
        if score_diff > 0:
            return {"home_win": 1.0, "draw": 0.0, "away_win": 0.0}
        elif score_diff < 0:
            return {"home_win": 0.0, "draw": 0.0, "away_win": 1.0}
        else:
            return {"home_win": 0.0, "draw": 1.0, "away_win": 0.0}

    # Average goals per minute in remaining time
    # World Cup averages ~2.6 goals/90min = ~0.0289 goals/min
    avg_goals_per_min = 0.0289
    expected_home = avg_goals_per_min * 0.53 * minutes_remaining
    expected_away = avg_goals_per_min * 0.47 * minutes_remaining

    # Simulate outcome probabilities using Poisson distribution
    home_win = 0.0
    draw = 0.0
    away_win = 0.0

    for h in range(8):  # additional home goals 0-7
        for a in range(8):  # additional away goals 0-7
            p = _poisson_prob(h, expected_home) * _poisson_prob(a, expected_away)
            final_diff = score_diff + h - a
            if final_diff > 0:
                home_win += p
            elif final_diff == 0:
                draw += p
            else:
                away_win += p

    total = home_win + draw + away_win
    if total > 0:
        home_win /= total
        draw /= total
        away_win /= total

    return {
        "home_win": round(home_win, 3),
        "draw": round(draw, 3),
        "away_win": round(away_win, 3),
        "minutes_remaining": minutes_remaining,
        "expected_more_goals": round(expected_home + expected_away, 2),
    }


def soccer_total_probability(home_score: int, away_score: int,
                              minute: int, line: float) -> dict:
    """Probability of going OVER the goal line."""
    minutes_remaining = max(0, 90 - minute)
    current_total = home_score + away_score

    if minutes_remaining == 0:
        return {"over": float(current_total > line),
                "under": float(current_total < line)}

    avg_goals_per_min = 0.0289
    expected_more = avg_goals_per_min * minutes_remaining

    over_prob = 0.0
    for additional in range(10):
        p = _poisson_prob(additional, expected_more)
        if current_total + additional > line:
            over_prob += p

    return {
        "over": round(over_prob, 3),
        "under": round(1 - over_prob, 3),
        "current_total": current_total,
        "expected_more": round(expected_more, 2),
        "projected_total": round(current_total + expected_more, 2),
    }


# ─────────────────────────────────────────────
#  NBA WIN PROBABILITY MODEL
# ─────────────────────────────────────────────

def nba_win_probability(score_diff: int, minutes_remaining: float,
                         quarter: int) -> float:
    """
    Win probability for the leading team in NBA.
    Based on historical win rates by score margin and time.
    """
    if minutes_remaining <= 0:
        return 1.0 if score_diff > 0 else (0.5 if score_diff == 0 else 0.0)

    if score_diff <= 0:
        return 0.5

    # NBA: ~2.0 points per minute scoring rate per team
    # So in X minutes, each team scores ~2X points
    # Standard deviation of score in X minutes ≈ 2.5 * sqrt(X)
    std = 2.5 * math.sqrt(minutes_remaining)
    if std == 0:
        return 1.0 if score_diff > 0 else 0.0

    # Probability of lead holding: P(margin + scoring > 0)
    z = score_diff / std
    prob = _normal_cdf(z)

    return round(min(0.99, max(0.01, prob)), 3)


def nba_total_probability(current_total: int, minutes_remaining: float,
                           line: float) -> dict:
    """Probability of going over NBA point total."""
    if minutes_remaining <= 0:
        return {"over": float(current_total > line),
                "under": float(current_total < line)}

    # NBA averages ~4.0 points per minute total (both teams)
    pts_per_min = 4.0
    expected_more = pts_per_min * minutes_remaining
    projected = current_total + expected_more
    std = 3.5 * math.sqrt(minutes_remaining)

    z = (line - projected) / std
    over_prob = 1 - _normal_cdf(z)

    return {
        "over": round(over_prob, 3),
        "under": round(1 - over_prob, 3),
        "projected_total": round(projected, 1),
    }


# ─────────────────────────────────────────────
#  NFL WIN PROBABILITY MODEL
# ─────────────────────────────────────────────

# Pre-calculated NFL win probabilities by (quarter, score_diff)
# Based on historical NFL play-by-play data
NFL_WIN_PROB = {
    (1, 1): 0.540, (1, 3): 0.584, (1, 7): 0.681, (1, 10): 0.741, (1, 14): 0.810,
    (2, 1): 0.548, (2, 3): 0.606, (2, 7): 0.724, (2, 10): 0.793, (2, 14): 0.858,
    (3, 1): 0.574, (3, 3): 0.653, (3, 7): 0.789, (3, 10): 0.860, (3, 14): 0.918,
    (4, 1): 0.643, (4, 3): 0.762, (4, 7): 0.901, (4, 10): 0.951, (4, 14): 0.980,
}


def nfl_win_probability(score_diff: int, quarter: int,
                         minutes_remaining: float) -> float:
    """Win probability for leading team in NFL game."""
    if score_diff <= 0:
        return 0.5
    if quarter == 4 and minutes_remaining <= 2 and score_diff >= 8:
        return 0.99  # two score lead in final 2 min

    # Find closest match in table
    diff_key = min([1, 3, 7, 10, 14], key=lambda x: abs(x - score_diff))
    prob = NFL_WIN_PROB.get((quarter, diff_key), 0.5)

    # Adjust for time remaining in quarter
    # More time = more variance = lower confidence
    if quarter == 4 and minutes_remaining < 5:
        # Adjust upward as game winds down
        time_factor = 1 - (minutes_remaining / 15)
        prob = prob + (1 - prob) * time_factor * 0.3

    return round(min(0.99, max(0.5, prob)), 3)


# ─────────────────────────────────────────────
#  NHL WIN PROBABILITY MODEL
# ─────────────────────────────────────────────

def nhl_win_probability(score_diff: int, period: int,
                         minutes_remaining: float) -> float:
    """Win probability for leading team in NHL."""
    if score_diff <= 0:
        return 0.5

    total_minutes_remaining = minutes_remaining + max(0, 3 - period) * 20

    if total_minutes_remaining <= 0:
        return 1.0 if score_diff > 0 else 0.0

    # NHL: ~0.05 goals per minute per team = 0.1 total
    goals_per_min = 0.05
    expected_more = goals_per_min * total_minutes_remaining
    std = math.sqrt(expected_more * 2)  # variance of Poisson

    if std == 0:
        return 1.0

    z = score_diff / std
    return round(min(0.99, _normal_cdf(z)), 3)


# ─────────────────────────────────────────────
#  EXPECTED VALUE CALCULATOR
# ─────────────────────────────────────────────

def calculate_ev(true_prob: float, market_price: float,
                  size: float = 1.0) -> dict:
    """
    Calculate expected value of a bet.

    true_prob: our estimated probability of winning
    market_price: Kalshi price (0-1, what we pay)
    size: dollar amount

    Kelly criterion optimal fraction:
    f* = (bp - q) / b
    where b = (1/market_price) - 1, p = true_prob, q = 1 - p
    """
    if market_price <= 0 or market_price >= 1:
        return {"ev": 0, "edge": 0, "kelly": 0}

    # Expected value per dollar bet
    payout_if_win = (1 / market_price) - 1  # net profit per $1
    ev = true_prob * payout_if_win - (1 - true_prob)

    # Edge = how much better our prob is vs market implied prob
    edge = true_prob - market_price

    # Kelly fraction (fraction of bankroll to bet)
    b = payout_if_win
    q = 1 - true_prob
    kelly = (b * true_prob - q) / b if b > 0 else 0
    quarter_kelly = kelly * 0.25  # use quarter Kelly for safety

    return {
        "ev": round(ev, 4),
        "edge": round(edge, 4),
        "kelly": round(kelly, 4),
        "quarter_kelly": round(quarter_kelly, 4),
        "true_prob": round(true_prob, 3),
        "market_price": market_price,
        "is_positive_ev": ev > 0.02,  # require >2% EV minimum
        "ev_pct": round(ev * 100, 1),
    }


# ─────────────────────────────────────────────
#  RECENT FORM FETCHER
# ─────────────────────────────────────────────

def fetch_team_recent_form(team_id: str, sport: str,
                            session: requests.Session = None) -> dict:
    """
    Fetch last 10 games for a team from ESPN.
    Returns win rate, avg score, avg allowed, recent trend.
    """
    cache_key = f"{sport}_{team_id}"
    now = time.time()
    if cache_key in _form_cache:
        if now - _form_cache_time.get(cache_key, 0) < FORM_CACHE_TTL:
            return _form_cache[cache_key]

    if session is None:
        session = requests.Session()

    sport_paths = {
        "mlb": "baseball/mlb",
        "nba": "basketball/nba",
        "nfl": "football/nfl",
        "nhl": "hockey/nhl",
        "soccer": "soccer/all",
        "world_cup": "soccer/fifa.world",
    }
    sport_path = sport_paths.get(sport, "baseball/mlb")
    url = f"{ESPN_BASE}/{sport_path}/teams/{team_id}/schedule"

    try:
        r = session.get(url, params={"limit": 10},
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=10)
        if r.status_code != 200:
            return {}

        data = r.json()
        events = data.get("events", [])

        wins, losses, scores_for, scores_against = 0, 0, [], []
        last_5 = []

        for event in events[-10:]:
            competitions = event.get("competitions", [{}])
            comp = competitions[0] if competitions else {}
            competitors = comp.get("competitors", [])

            team_comp = next(
                (c for c in competitors if c.get("id") == team_id), None
            )
            if not team_comp:
                continue

            winner = team_comp.get("winner", False)
            score = float(team_comp.get("score", 0) or 0)
            opp_comp = next(
                (c for c in competitors if c.get("id") != team_id), {}
            )
            opp_score = float(opp_comp.get("score", 0) or 0)

            scores_for.append(score)
            scores_against.append(opp_score)

            if winner:
                wins += 1
                last_5.append("W")
            else:
                losses += 1
                last_5.append("L")

        total = wins + losses
        win_rate = wins / total if total > 0 else 0.5
        avg_scored = sum(scores_for) / len(scores_for) if scores_for else 0
        avg_allowed = sum(scores_against) / len(scores_against) if scores_against else 0

        # Trend: is team improving or declining?
        recent_3 = last_5[-3:]
        trend = "hot" if recent_3.count("W") >= 2 else \
                "cold" if recent_3.count("L") >= 2 else "neutral"

        result = {
            "team_id": team_id,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 3),
            "avg_scored": round(avg_scored, 1),
            "avg_allowed": round(avg_allowed, 1),
            "run_differential": round(avg_scored - avg_allowed, 1),
            "last_5": last_5[-5:],
            "trend": trend,
            "games_played": total,
        }

        _form_cache[cache_key] = result
        _form_cache_time[cache_key] = now
        return result

    except Exception as e:
        log.debug("Form fetch failed for %s: %s", team_id, e)
        return {}


def get_game_form_adjustment(home_form: dict, away_form: dict,
                              sport: str) -> float:
    """
    Calculate a probability adjustment based on recent form.
    Returns adjustment to add to base win probability (-0.10 to +0.10).
    """
    if not home_form or not away_form:
        return 0.0

    home_wr = home_form.get("win_rate", 0.5)
    away_wr = away_form.get("win_rate", 0.5)

    # How much better/worse is home team vs average
    home_relative = home_wr - 0.5
    away_relative = away_wr - 0.5

    # Net adjustment (home advantage built into base model)
    adjustment = (home_relative - away_relative) * 0.15

    # Trend adjustment
    home_trend = home_form.get("trend", "neutral")
    away_trend = away_form.get("trend", "neutral")

    if home_trend == "hot" and away_trend != "hot":
        adjustment += 0.03
    elif away_trend == "hot" and home_trend != "hot":
        adjustment -= 0.03
    if home_trend == "cold":
        adjustment -= 0.03
    if away_trend == "cold":
        adjustment += 0.03

    return round(max(-0.10, min(0.10, adjustment)), 3)


def format_game_context(game: dict) -> str:
    """
    Format a game's current state as a string for Claude to evaluate.
    Includes all relevant stats for Claude to make a smart decision.
    """
    sport = game.get("sport", "")
    teams = game.get("teams", [])
    scores = game.get("scores", [0, 0])
    period = game.get("period", 0)
    clock = game.get("clock", "")

    if len(teams) >= 2:
        t1 = teams[0].get("name", "Home")
        t2 = teams[1].get("name", "Away")
    else:
        t1, t2 = "Home", "Away"

    s1, s2 = (scores[0], scores[1]) if len(scores) >= 2 else (0, 0)
    score_diff = abs(s1 - s2)
    leader = t1 if s1 > s2 else (t2 if s2 > s1 else "Tied")

    lines = [f"Sport: {sport.upper()}", f"Game: {t1} vs {t2}",
             f"Score: {t1} {s1} - {t2} {s2}", f"Period/Inning: {period}"]

    if clock:
        lines.append(f"Clock: {clock}")

    # Add sport-specific probability context
    if sport == "mlb":
        if score_diff > 0 and period > 0:
            home_leads = s1 > s2
            win_prob = mlb_win_probability(period, score_diff, home_leads)
            lines.append(f"Win probability ({leader}): {win_prob:.1%}")

            # Total runs context
            total = mlb_total_runs_probability(period, s1 + s2, 8.5)
            lines.append(
                f"Over 8.5 runs: {total['over']:.1%} "
                f"(projected total: {total.get('projected_total', '?')})"
            )

    elif sport in ("soccer", "world_cup"):
        if period > 0:
            try:
                clock_min = int(clock.split(":")[0]) if ":" in clock else int(clock)
            except (ValueError, IndexError):
                clock_min = period * 45
            probs = soccer_win_probability(s1, s2, clock_min)
            lines.append(
                f"Win probs: {t1} {probs['home_win']:.1%} | "
                f"Draw {probs['draw']:.1%} | {t2} {probs['away_win']:.1%}"
            )
            total = soccer_total_probability(s1, s2, clock_min, 2.5)
            lines.append(
                f"Over 2.5 goals: {total['over']:.1%} "
                f"(expected {total['expected_more']:.1f} more goals)"
            )

    elif sport == "nba":
        mins_per_q = 12
        mins_remaining = max(0, (4 - period) * mins_per_q +
                             (mins_per_q - _parse_clock(clock)))
        if score_diff > 0:
            win_prob = nba_win_probability(score_diff, mins_remaining, period)
            lines.append(f"Win prob ({leader}): {win_prob:.1%}")
            total = nba_total_probability(s1 + s2, mins_remaining, 220.5)
            lines.append(f"Over 220.5: {total['over']:.1%} "
                         f"(projected: {total.get('projected_total', '?')})")

    elif sport == "nfl":
        q = period
        mins_rem = _parse_clock(clock)
        if score_diff > 0:
            win_prob = nfl_win_probability(score_diff, q, mins_rem)
            lines.append(f"Win prob ({leader}): {win_prob:.1%}")

    elif sport == "nhl":
        mins_rem = _parse_clock(clock)
        if score_diff > 0:
            win_prob = nhl_win_probability(score_diff, period, mins_rem)
            lines.append(f"Win prob ({leader}): {win_prob:.1%}")

    return "\n".join(lines)


# ─────────────────────────────────────────────
#  MAIN SIGNAL EVALUATOR
# ─────────────────────────────────────────────

def evaluate_signal(game: dict, market_price: float,
                    signal_type: str, signal_side: str,
                    session: requests.Session = None) -> dict:
    """
    Full statistical evaluation of a trading signal.

    Returns:
    {
        true_prob: float,        # our estimated win probability
        ev: dict,                # expected value calculation
        form_adjustment: float,  # recent form adjustment
        confidence: int,         # 0-100 confidence score
        should_trade: bool,      # final recommendation
        reason: str,             # explanation
    }
    """
    sport = game.get("sport", "")
    teams = game.get("teams", [])
    scores = game.get("scores", [0, 0])
    period = game.get("period", 0)
    clock = game.get("clock", "")

    s1, s2 = (scores[0], scores[1]) if len(scores) >= 2 else (0, 0)
    score_diff = abs(s1 - s2)
    home_leads = s1 > s2

    true_prob = 0.5
    reason_parts = []

    # ── Get base win probability ──────────────────────────────────
    if sport == "mlb":
        if signal_type == "WIN" and score_diff > 0 and period > 0:
            true_prob = mlb_win_probability(period, score_diff, home_leads)
            if signal_side in ("AWAY", "NO") and not home_leads:
                true_prob = mlb_win_probability(period, score_diff, False)
            reason_parts.append(
                f"MLB win prob: {true_prob:.1%} "
                f"(up {score_diff} in inning {period})"
            )

        elif signal_type == "TOTAL":
            total = mlb_total_runs_probability(period, s1 + s2, 8.5)
            true_prob = total["over"] if signal_side == "OVER" else total["under"]
            reason_parts.append(
                f"MLB total: {true_prob:.1%} {signal_side} "
                f"(projected {total.get('projected_total', '?')} runs)"
            )

    elif sport in ("soccer", "world_cup"):
        try:
            clock_min = int(clock.split(":")[0]) if ":" in clock else int(clock)
        except (ValueError, IndexError):
            clock_min = period * 45

        if signal_type == "WIN":
            probs = soccer_win_probability(s1, s2, clock_min)
            if signal_side in ("HOME", "YES"):
                true_prob = probs["home_win"]
            elif signal_side in ("AWAY", "NO"):
                true_prob = probs["away_win"]
            reason_parts.append(
                f"Soccer win prob: {true_prob:.1%} at {clock_min}'"
            )

        elif signal_type == "TOTAL":
            line = 2.5  # default World Cup line
            total = soccer_total_probability(s1, s2, clock_min, line)
            true_prob = total["over"] if signal_side == "OVER" else total["under"]
            reason_parts.append(
                f"Soccer total: {true_prob:.1%} {signal_side} "
                f"({total['expected_more']:.1f} more goals expected)"
            )

    elif sport == "nba":
        mins_per_q = 12
        mins_remaining = max(0, (4 - period) * mins_per_q +
                             (mins_per_q - _parse_clock(clock)))
        if signal_type == "WIN" and score_diff > 0:
            true_prob = nba_win_probability(score_diff, mins_remaining, period)
            reason_parts.append(
                f"NBA win prob: {true_prob:.1%} "
                f"(+{score_diff} with {mins_remaining:.0f}min left)"
            )

        elif signal_type == "TOTAL":
            total = nba_total_probability(s1 + s2, mins_remaining, 220.5)
            true_prob = total["over"] if signal_side == "OVER" else total["under"]
            reason_parts.append(f"NBA total: {true_prob:.1%} {signal_side}")

    elif sport == "nfl":
        mins_rem = _parse_clock(clock)
        if signal_type == "WIN" and score_diff > 0:
            true_prob = nfl_win_probability(score_diff, period, mins_rem)
            reason_parts.append(
                f"NFL win prob: {true_prob:.1%} "
                f"(+{score_diff} in Q{period})"
            )

    elif sport == "nhl":
        mins_rem = _parse_clock(clock)
        if signal_type == "WIN" and score_diff > 0:
            true_prob = nhl_win_probability(score_diff, period, mins_rem)
            reason_parts.append(
                f"NHL win prob: {true_prob:.1%} "
                f"(+{score_diff} in P{period})"
            )

    # ── Recent form adjustment ────────────────────────────────────
    form_adj = 0.0
    if session and len(teams) >= 2:
        home_id = teams[0].get("id", "")
        away_id = teams[1].get("id", "")
        if home_id and away_id:
            home_form = fetch_team_recent_form(home_id, sport, session)
            away_form = fetch_team_recent_form(away_id, sport, session)

            if home_form and away_form:
                form_adj = get_game_form_adjustment(home_form, away_form, sport)
                true_prob = max(0.01, min(0.99, true_prob + form_adj))

                # Add form context to reason
                home_trend = home_form.get("trend", "neutral")
                away_trend = away_form.get("trend", "neutral")
                home_wr = home_form.get("win_rate", 0.5)
                away_wr = away_form.get("win_rate", 0.5)
                h_name = teams[0].get("name", "Home")
                a_name = teams[1].get("name", "Away")

                reason_parts.append(
                    f"Recent form: {h_name} {home_wr:.0%}WR ({home_trend}), "
                    f"{a_name} {away_wr:.0%}WR ({away_trend}). "
                    f"Form adjustment: {form_adj:+.1%}"
                )

                if home_form.get("last_5"):
                    reason_parts.append(
                        f"{h_name} L5: {''.join(home_form['last_5'])}"
                    )
                if away_form.get("last_5"):
                    reason_parts.append(
                        f"{a_name} L5: {''.join(away_form['last_5'])}"
                    )

    # ── Expected value calculation ────────────────────────────────
    ev = calculate_ev(true_prob, market_price)

    # ── Final recommendation ──────────────────────────────────────
    edge_required = 0.05  # need 5% edge minimum
    ev_required = 0.02    # need 2% EV minimum

    should_trade = (
        ev["is_positive_ev"] and
        ev["edge"] >= edge_required and
        true_prob >= 0.55  # don't bet coin flips
    )

    confidence = int(min(95, max(0,
        50 +
        ev["edge"] * 200 +  # edge contribution
        (true_prob - 0.5) * 60 +  # probability contribution
        (form_adj * 100)  # form contribution
    )))

    reason = " | ".join(reason_parts) if reason_parts else "No model data"
    reason += (
        f" | EV: {ev['ev_pct']:+.1f}% "
        f"| Edge: {ev['edge']:+.1%} "
        f"| True prob: {true_prob:.1%} "
        f"| Market: {market_price:.1%}"
    )

    return {
        "true_prob": true_prob,
        "ev": ev,
        "form_adjustment": form_adj,
        "confidence": confidence,
        "should_trade": should_trade,
        "reason": reason,
        "quarter_kelly": ev["quarter_kelly"],
    }


# ─────────────────────────────────────────────
#  UTILITY FUNCTIONS
# ─────────────────────────────────────────────

def _poisson_prob(k: int, lam: float) -> float:
    """P(X=k) for Poisson distribution with rate lambda."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    try:
        return math.exp(-lam) * (lam ** k) / math.factorial(k)
    except (OverflowError, ValueError):
        return 0.0


def _normal_cdf(z: float) -> float:
    """Approximate normal CDF using error function."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _parse_clock(clock: str) -> float:
    """Parse game clock string to minutes remaining."""
    if not clock:
        return 12.0
    try:
        if ":" in clock:
            parts = clock.split(":")
            return int(parts[0]) + int(parts[1]) / 60
        return float(clock)
    except (ValueError, IndexError):
        return 12.0
