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
    scores = [float(t.get("score", 0) or 0) for t in game.get("teams", [])] or [0, 0]
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
                    session: requests.Session = None,
                    spread_line: float = 1.5) -> dict:
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
    scores = [float(t.get("score", 0) or 0) for t in game.get("teams", [])] or [0, 0]
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

        elif signal_type == "SPREAD":
            sp = mlb_spread_probability(period, abs(score_diff), spread_line)
            # When we bet on the LEADER (HOME/AWAY/COVER/YES), we want them to cover
            # The leader covering = the "cover" probability
            betting_leader = signal_side in ("COVER", "YES", "HOME", "AWAY")
            true_prob = sp["cover"] if betting_leader else sp["no_cover"]
            reason_parts.append(
                f"MLB spread: {true_prob:.1%} to cover {spread_line} "
                f"(current margin {int(score_diff)}, expected {sp['expected_margin']})"
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

        elif signal_type == "SPREAD":
            sp = soccer_spread_probability(clock_min, abs(int(score_diff)), spread_line)
            betting_leader = signal_side in ("COVER", "YES", "HOME", "AWAY")
            true_prob = sp["cover"] if betting_leader else sp["no_cover"]
            reason_parts.append(
                f"Soccer spread: {true_prob:.1%} to cover {spread_line} "
                f"(margin {int(score_diff)} at {clock_min}')"
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

        elif signal_type == "SPREAD":
            sp = basketball_spread_probability(mins_remaining, abs(int(score_diff)), spread_line)
            betting_leader = signal_side in ("COVER", "YES", "HOME", "AWAY")
            true_prob = sp["cover"] if betting_leader else sp["no_cover"]
            reason_parts.append(
                f"NBA spread: {true_prob:.1%} to cover {spread_line} "
                f"(margin {int(score_diff)}, {mins_remaining:.0f}min left)"
            )

    elif sport == "nfl":
        mins_rem = _parse_clock(clock)
        if signal_type == "WIN" and score_diff > 0:
            true_prob = nfl_win_probability(score_diff, period, mins_rem)
            reason_parts.append(
                f"NFL win prob: {true_prob:.1%} "
                f"(+{score_diff} in Q{period})"
            )
        elif signal_type == "TOTAL":
            total = nfl_total_probability(s1 + s2, period, mins_rem, 44.5)
            true_prob = total["over"] if signal_side == "OVER" else total["under"]
            reason_parts.append(
                f"NFL total: {true_prob:.1%} {signal_side} "
                f"(projected {total['projected_total']})"
            )

        elif signal_type == "SPREAD":
            sp = nfl_spread_probability(period, mins_rem, abs(int(score_diff)), spread_line)
            betting_leader = signal_side in ("COVER", "YES", "HOME", "AWAY")
            true_prob = sp["cover"] if betting_leader else sp["no_cover"]
            reason_parts.append(
                f"NFL spread: {true_prob:.1%} to cover {spread_line} "
                f"(margin {int(score_diff)} in Q{period})"
            )

    elif sport == "nhl":
        mins_rem = _parse_clock(clock)
        if signal_type == "WIN" and score_diff > 0:
            true_prob = nhl_win_probability(score_diff, period, mins_rem)
            reason_parts.append(
                f"NHL win prob: {true_prob:.1%} "
                f"(+{score_diff} in P{period})"
            )
        elif signal_type == "TOTAL":
            total = nhl_total_probability(s1 + s2, period, mins_rem, 5.5)
            true_prob = total["over"] if signal_side == "OVER" else total["under"]
            reason_parts.append(
                f"NHL total: {true_prob:.1%} {signal_side} "
                f"(projected {total['projected_total']})"
            )

        elif signal_type == "SPREAD":
            sp = nhl_spread_probability(period, mins_rem, abs(int(score_diff)), spread_line)
            betting_leader = signal_side in ("COVER", "YES", "HOME", "AWAY")
            true_prob = sp["cover"] if betting_leader else sp["no_cover"]
            reason_parts.append(
                f"NHL spread: {true_prob:.1%} to cover {spread_line} "
                f"(margin {int(score_diff)} in P{period})"
            )

    elif sport in ("tennis", "tennis_atp", "tennis_wta"):
        if signal_type == "WIN" and score_diff != 0:
            leader_sets = int(max(s1, s2))
            trailer_sets = int(min(s1, s2))
            best_of_5 = game.get("best_of_5", False)
            true_prob = tennis_win_probability(
                leader_sets, trailer_sets, best_of_5=best_of_5
            )
            # If betting on the trailer, invert
            leader_is_home = s1 > s2
            betting_home = signal_side in ("HOME", "YES")
            if betting_home != leader_is_home:
                true_prob = 1 - true_prob
            reason_parts.append(
                f"Tennis win prob: {true_prob:.1%} "
                f"(sets {leader_sets}-{trailer_sets})"
            )

    elif sport in ("ufc", "mma"):
        if signal_type == "WIN":
            # UFC: only confident if there's a clear winner indication
            is_winning = score_diff > 0 or signal_side in ("HOME", "YES")
            true_prob = ufc_win_probability(period, is_winning=is_winning)
            reason_parts.append(
                f"UFC win prob: {true_prob:.1%} (round {period}) "
                f"— high variance, conservative estimate"
            )

    # Generic fallback for any sport/signal combo not covered above
    if not reason_parts and signal_type == "WIN" and score_diff > 0:
        total_periods = {"mlb": 9, "nba": 4, "nfl": 4, "nhl": 3,
                         "soccer": 2, "world_cup": 2}.get(sport, 4)
        true_prob = generic_win_probability(score_diff, period, total_periods)
        reason_parts.append(
            f"Generic win model: {true_prob:.1%} "
            f"(+{score_diff} lead, period {period}/{total_periods})"
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
    edge_required = 0.05
    has_model_data = len(reason_parts) > 0  # did we actually compute something?

    if not has_model_data:
        # No model data available — fall back to rule trader's own confidence
        # Don't block trades just because we can't compute a probability
        should_trade = True
        confidence = 60
        reason = f"No probability model for {sport} {signal_type} | Market: {market_price:.1%}"
    else:
        ev = calculate_ev(true_prob, market_price)
        should_trade = (
            ev["is_positive_ev"] and
            ev["edge"] >= edge_required and
            true_prob >= 0.55
        )
        confidence = int(min(95, max(0,
            50 +
            ev["edge"] * 200 +
            (true_prob - 0.5) * 60 +
            (form_adj * 100)
        )))
        reason = " | ".join(reason_parts)
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

    # No model data — allow trade but flag it
    return {
        "true_prob": 0.5,
        "ev": {"ev": 0, "edge": 0, "kelly": 0, "quarter_kelly": 0,
               "is_positive_ev": True, "ev_pct": 0},
        "form_adjustment": 0,
        "confidence": confidence,
        "should_trade": should_trade,
        "reason": reason,
        "quarter_kelly": 0,
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


# ─────────────────────────────────────────────
#  ADVANCED EDGE DETECTION
# ─────────────────────────────────────────────

"""
Advanced Edge Detection Layers
================================
These layers identify when Kalshi's market price is WRONG relative to
what the math says. The bigger the gap, the bigger the edge.

Layer 1: Momentum Model
  Tracks whether scoring rate is accelerating or decelerating.
  A team scoring 3 runs in the last inning has MORE edge than
  a team that scored their 3 runs in inning 1.

Layer 2: Pitcher/Fatigue Model (MLB)
  Late game = bullpen time = higher variance
  Starters through 7 innings are MORE reliable than bullpen

Layer 3: Market Lag Detection
  Kalshi prices update slower than reality.
  When a game changes dramatically (big inning, red card, injury)
  the market takes 30-90 seconds to adjust.
  We can detect this by comparing true_prob to market_price.

Layer 4: Outs-Adjusted MLB Model
  Accounts for outs remaining in inning, not just inning number.
  Bottom of 9th with 2 outs is very different from top of 9th.

Layer 5: Soccer Red Card Model
  A red card is worth ~0.3 goals and changes win probability significantly.
  Kalshi is slow to reflect this.

Layer 6: Score Pace Volatility
  High-scoring games have higher variance — OVER is better value.
  Low-scoring pitcher's duels have lower variance — UNDER is better value.
"""

import statistics as _stats


# ── Layer 1: Momentum Model ──────────────────

def calculate_momentum(game: dict) -> dict:
    """
    Analyze scoring momentum — is the leading team accelerating?
    Returns momentum score (-1 to +1) for the leading team.

    Positive = leading team is on a hot streak
    Negative = trailing team has momentum
    """
    teams = game.get("teams", [])
    if len(teams) < 2:
        return {"momentum": 0, "hot_team": None, "reason": "insufficient data"}

    # Get play-by-play scoring history if available
    plays = game.get("recent_plays", [])
    period = int(game.get("period", 0) or 0)
    sport = game.get("sport", "")

    scores = [float(t.get("score", 0) or 0) for t in teams]
    if not scores:
        return {"momentum": 0, "hot_team": None, "reason": "no scores"}

    leader_idx = 0 if scores[0] >= scores[1] else 1
    trailer_idx = 1 - leader_idx
    score_diff = abs(scores[0] - scores[1])

    # Without play-by-play, estimate momentum from scoring pace
    # In late innings, if leader has been scoring recently, momentum is positive
    momentum = 0.0
    reason = "base momentum"

    if sport == "mlb":
        # Late-inning large leads have strong positive momentum
        if period >= 7 and score_diff >= 3:
            momentum = 0.6
            reason = f"Large lead ({score_diff} runs) in inning {period}"
        elif period >= 8 and score_diff >= 2:
            momentum = 0.7
            reason = f"Late lead ({score_diff} runs) in inning {period}"
        elif period >= 9 and score_diff >= 1:
            momentum = 0.8
            reason = f"Final inning lead"

    elif sport in ("soccer", "world_cup"):
        try:
            clock_min = int(str(game.get("clock", "0")).split(":")[0])
        except (ValueError, IndexError):
            clock_min = period * 45

        if score_diff >= 2 and clock_min >= 70:
            momentum = 0.7
            reason = f"2+ goal lead at {clock_min}'"
        elif score_diff == 1 and clock_min >= 80:
            momentum = 0.4
            reason = f"1 goal lead at {clock_min}'"

    elif sport == "nba":
        # Minutes remaining approximation
        mins_per_q = 12
        mins_remaining = max(0, (4 - period) * mins_per_q)
        if score_diff >= 15 and mins_remaining <= 6:
            momentum = 0.8
            reason = f"+{score_diff} with {mins_remaining:.0f}min left"
        elif score_diff >= 10 and mins_remaining <= 4:
            momentum = 0.75
            reason = f"+{score_diff} with {mins_remaining:.0f}min left"

    elif sport == "nfl":
        mins_remaining = max(0, (4 - period) * 15)
        if score_diff >= 14 and mins_remaining <= 8:
            momentum = 0.75
            reason = f"+{score_diff} with {mins_remaining:.0f}min left"
        elif score_diff >= 21:
            momentum = 0.8
            reason = f"Three-score lead ({score_diff})"

    elif sport == "nhl":
        if score_diff >= 3 and period >= 3:
            momentum = 0.7
            reason = f"+{score_diff} goals in 3rd period"
        elif score_diff >= 2 and period >= 3:
            momentum = 0.5
            reason = f"+{score_diff} goals late"

    elif sport == "pga":
        # For golf, "score" is strokes — lower is better, momentum based on lead
        if score_diff >= 4:
            momentum = 0.6
            reason = f"{score_diff}-stroke lead"
        elif score_diff >= 2:
            momentum = 0.4
            reason = f"{score_diff}-stroke lead"

    elif sport in ("tennis", "tennis_atp", "tennis_wta"):
        # Sets lead is strong momentum in tennis
        if score_diff >= 2:
            momentum = 0.7
            reason = f"Up {score_diff} sets"
        elif score_diff == 1:
            momentum = 0.5
            reason = "Up 1 set"

    elif sport in ("ufc", "mma"):
        # UFC momentum is unreliable — keep it low
        momentum = 0.2 if score_diff > 0 else 0.0
        reason = "UFC — momentum unreliable, high KO variance"

    return {
        "momentum": momentum,
        "hot_team_idx": leader_idx,
        "hot_team": teams[leader_idx].get("name", "?") if teams else None,
        "reason": reason,
    }


# ── Layer 2: Market Lag Detection ────────────

def detect_market_lag(true_prob: float, market_price: float,
                       game: dict) -> dict:
    """
    Detect if Kalshi's market price hasn't caught up to reality.

    Returns lag score 0-1 and whether we should bet.
    High lag = market is stale = strong buy signal.
    """
    if true_prob <= 0 or market_price <= 0:
        return {"lag": 0, "edge": 0, "bet_now": False}

    raw_edge = true_prob - market_price
    lag_score = abs(raw_edge)

    # Classify the edge
    if lag_score >= 0.15:
        strength = "STRONG"
        bet_now = True
    elif lag_score >= 0.10:
        strength = "MODERATE"
        bet_now = True
    elif lag_score >= 0.05:
        strength = "WEAK"
        bet_now = raw_edge > 0  # only bet if in our favor
    else:
        strength = "NONE"
        bet_now = False

    # Directional — only signal if edge is in our favor
    in_our_favor = raw_edge > 0

    return {
        "lag": lag_score,
        "edge": raw_edge,
        "strength": strength,
        "bet_now": bet_now and in_our_favor,
        "reason": f"True prob {true_prob:.1%} vs market {market_price:.1%} = {raw_edge:+.1%} edge ({strength})"
    }


# ── Layer 3: Outs-Adjusted MLB Model ─────────

# MLB win probability accounting for outs in inning
# (inning, half, outs, run_diff) → win_prob
# half: 0=top (visitor batting), 1=bottom (home batting)
MLB_WIN_PROB_OUTS = {
    # Top of 9th (away team batting, home team leads by X)
    # Home team wants to preserve lead — higher win prob in bottom half
    (9, 0, 0, 1): 0.850, (9, 0, 0, 2): 0.945, (9, 0, 0, 3): 0.984,
    (9, 0, 1, 1): 0.870, (9, 0, 1, 2): 0.955, (9, 0, 1, 3): 0.988,
    (9, 0, 2, 1): 0.905, (9, 0, 2, 2): 0.970, (9, 0, 2, 3): 0.993,
    # Bottom of 9th (home team batting, trailing)
    (9, 1, 0, 1): 0.765, (9, 1, 0, 2): 0.892, (9, 1, 0, 3): 0.961,
    (9, 1, 1, 1): 0.810, (9, 1, 1, 2): 0.918, (9, 1, 1, 3): 0.973,
    (9, 1, 2, 1): 0.865, (9, 1, 2, 2): 0.948, (9, 1, 2, 3): 0.985,
}


def mlb_win_prob_outs(inning: int, half: int, outs: int,
                       run_diff: int, home_team_leads: bool) -> float:
    """
    More precise MLB win probability using outs remaining.
    half: 0=top, 1=bottom
    outs: 0, 1, or 2
    run_diff: positive integer (magnitude of lead)
    """
    diff_key = min(run_diff, 3)
    key = (inning, half, outs, diff_key)

    if key in MLB_WIN_PROB_OUTS:
        prob = MLB_WIN_PROB_OUTS[key]
        # Adjust if home/away is flipped relative to our key
        if not home_team_leads and half == 1:
            # Away team leads in bottom of inning — they're at bat
            prob = 1 - prob
        return prob

    # Fall back to inning-only model
    from stats_models import mlb_win_probability
    return mlb_win_probability(inning, run_diff, home_team_leads)


# ── Layer 4: Soccer Red Card Model ──────────

def soccer_red_card_adjustment(home_players: int = 11,
                                away_players: int = 11) -> dict:
    """
    Adjust win probabilities for red cards.
    A red card reduces a team to 10 men and is worth approximately
    +0.25 goal equivalent to the other team.
    """
    if home_players == away_players:
        return {"home_adj": 0, "away_adj": 0, "note": "no red cards"}

    home_advantage = home_players - away_players  # positive = home has more players

    # Each player advantage is worth ~0.12-0.15 probability points
    PLAYER_VALUE = 0.13

    return {
        "home_adj": home_advantage * PLAYER_VALUE,
        "away_adj": -home_advantage * PLAYER_VALUE,
        "note": f"Home {home_players} vs Away {away_players} players",
    }


# ── Layer 5: Score Pace Volatility ──────────

def score_pace_volatility(sport: str, current_total: float,
                           period: int, periods_remaining: float) -> dict:
    """
    Calculate expected variance in final score.
    High variance = totals markets are more valuable.
    Low variance = win markets are more valuable (leads are safer).

    Returns:
    - expected_final: predicted final score total
    - std_dev: standard deviation of expected final
    - over_value: which lines have good OVER value
    - under_value: which lines have good UNDER value
    """
    if period <= 0 or periods_remaining < 0:
        return {}

    # Points per period averages
    avg_per_period = {
        "mlb": 1.0,     # ~9 runs per game / 9 innings
        "nba": 24.0,    # ~200 points / ~8.3 quarters worth
        "nfl": 6.5,     # ~45 points / 4 quarters + OT
        "nhl": 0.65,    # ~5.5 goals / 9 periods
        "soccer": 1.3,  # ~2.6 goals / 2 halves
        "world_cup": 1.3,
    }.get(sport, 1.0)

    expected_additional = avg_per_period * periods_remaining
    expected_final = current_total + expected_additional

    # Variance scales with remaining periods
    # Using Poisson approximation: variance ≈ expected value
    variance = expected_additional * 1.1  # slight overdispersion
    std_dev = math.sqrt(max(variance, 0.1))

    # Which total lines are +EV?
    # Lines within 1 std dev of expected are roughly 50/50
    # Lines >1.5 std devs away have strong directional probability
    over_edge_line = expected_final + 0.5 * std_dev  # OVER this = slight value
    under_edge_line = expected_final - 0.5 * std_dev  # UNDER this = slight value

    return {
        "expected_final": round(expected_final, 1),
        "std_dev": round(std_dev, 2),
        "over_edge_line": round(over_edge_line, 1),
        "under_edge_line": round(under_edge_line, 1),
        "high_variance": std_dev > avg_per_period * 1.5,
    }


# ── Combined Advanced Evaluator ──────────────

def advanced_evaluate(game: dict, market_price: float,
                       signal_type: str, signal_side: str,
                       base_true_prob: float) -> dict:
    """
    Run all advanced layers and combine into final recommendation.

    Returns enhanced evaluation with:
    - final_true_prob: probability after all adjustments
    - total_edge: combined edge
    - confidence_boost: how much to boost rule confidence
    - bet_size_mult: Kelly-adjusted size multiplier
    - reasoning: full explanation
    """
    sport = game.get("sport", "")
    teams = game.get("teams", [])
    period = int(game.get("period", 0) or 0)
    scores = [float(t.get("score", 0) or 0) for t in teams]

    reasoning = []
    adjustments = []
    true_prob = base_true_prob

    # Layer 1: Momentum
    momentum = calculate_momentum(game)
    mom_score = momentum.get("momentum", 0)
    if mom_score > 0.3:
        adj = mom_score * 0.05  # up to +5% from momentum
        adjustments.append(adj)
        reasoning.append(f"Momentum: {momentum['reason']} (+{adj:.1%})")

    # Layer 2: Market Lag
    lag = detect_market_lag(true_prob, market_price, game)
    if lag.get("bet_now") and lag["lag"] > 0.05:
        reasoning.append(f"Market lag: {lag['reason']}")

    # Layer 3: Outs-adjusted (MLB only)
    if sport == "mlb" and period >= 7 and len(scores) >= 2:
        score_diff = abs(scores[0] - scores[1])
        home_leads = scores[1] > scores[0]  # scores[1] = home team
        outs = int(game.get("outs", 0) or 0)
        half = 0 if game.get("batting_team") == "away" else 1

        if score_diff > 0:
            outs_prob = mlb_win_prob_outs(
                period, half, outs, int(score_diff), home_leads
            )
            if abs(outs_prob - true_prob) > 0.03:
                adj = outs_prob - true_prob
                adjustments.append(adj)
                reasoning.append(
                    f"Outs-adjusted prob: {outs_prob:.1%} "
                    f"(outs={outs}, half={'bottom' if half else 'top'})"
                )
                true_prob = outs_prob

    # Layer 4: Score pace volatility for total markets
    if signal_type == "TOTAL":
        current_total = sum(scores)
        periods_per_game = {"mlb": 9, "nba": 4, "nfl": 4, "nhl": 3,
                            "soccer": 2, "world_cup": 2}.get(sport, 9)
        periods_remaining = max(0, periods_per_game - period)

        vol = score_pace_volatility(sport, current_total, period, periods_remaining)
        if vol:
            expected = vol.get("expected_final", current_total)
            reasoning.append(
                f"Score pace: expected final {expected:.1f} "
                f"(±{vol.get('std_dev', 0):.1f})"
            )

    # Apply adjustments
    for adj in adjustments:
        true_prob = max(0.01, min(0.99, true_prob + adj))

    # Calculate final edge and recommendation
    edge = true_prob - market_price
    ev = calculate_ev(true_prob, market_price)

    # Confidence boost based on number of confirming layers
    confidence_boost = min(15, len(reasoning) * 4)

    # Size multiplier — bet bigger when more layers confirm
    if edge >= 0.15:
        size_mult = 1.3
    elif edge >= 0.10:
        size_mult = 1.1
    elif edge >= 0.05:
        size_mult = 1.0
    else:
        size_mult = 0.8

    # Final recommendation
    should_bet = edge >= 0.05 and true_prob >= 0.55

    return {
        "true_prob": round(true_prob, 3),
        "market_price": market_price,
        "edge": round(edge, 3),
        "ev": ev,
        "should_bet": should_bet,
        "confidence_boost": confidence_boost,
        "size_mult": size_mult,
        "reasoning": reasoning,
        "reasoning_str": " | ".join(reasoning) if reasoning else "base model only",
        "momentum": momentum,
        "market_lag": lag,
    }


# ─────────────────────────────────────────────
#  NFL / NHL TOTAL MODELS + GENERIC FALLBACK
# ─────────────────────────────────────────────

def nfl_total_probability(current_total: int, period: int,
                           mins_remaining: float, line: float = 44.5) -> dict:
    """NFL total points over/under probability."""
    # NFL averages ~0.75 points per minute of game time
    total_game_mins = 60.0
    mins_elapsed = max(1, (period - 1) * 15 + (15 - mins_remaining)) if period <= 4 \
        else total_game_mins
    mins_left = max(0, total_game_mins - mins_elapsed)

    # Project remaining scoring based on pace
    if mins_elapsed > 0:
        pace = current_total / mins_elapsed
        projected_additional = pace * mins_left
    else:
        projected_additional = 0.75 * mins_left
    projected_total = current_total + projected_additional

    # Standard deviation grows with time remaining
    std = max(3.0, math.sqrt(mins_left) * 2.2)
    if std <= 0:
        over_prob = 1.0 if projected_total > line else 0.0
    else:
        z = (projected_total - line) / std
        over_prob = _normal_cdf(z)

    return {
        "over": round(over_prob, 3),
        "under": round(1 - over_prob, 3),
        "projected_total": round(projected_total, 1),
    }


def nhl_total_probability(current_total: int, period: int,
                           mins_remaining: float, line: float = 5.5) -> dict:
    """NHL total goals over/under probability."""
    total_game_mins = 60.0
    mins_elapsed = max(1, (period - 1) * 20 + (20 - mins_remaining)) if period <= 3 \
        else total_game_mins
    mins_left = max(0, total_game_mins - mins_elapsed)

    # NHL averages ~0.09 goals per minute
    expected_additional = 0.09 * mins_left
    projected_total = current_total + expected_additional

    # Poisson model for remaining goals
    over_prob = _poisson_over(line - current_total, expected_additional)

    return {
        "over": round(over_prob, 3),
        "under": round(1 - over_prob, 3),
        "projected_total": round(projected_total, 1),
    }


def _normal_cdf(z: float) -> float:
    """Standard normal CDF approximation."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _poisson_over(threshold: float, lam: float) -> float:
    """P(Poisson(lam) > threshold) — probability of exceeding threshold goals."""
    if lam <= 0:
        return 0.0 if threshold >= 0 else 1.0
    if threshold < 0:
        return 1.0
    # Sum P(X = k) for k from 0 to floor(threshold), subtract from 1
    cumulative = 0.0
    k_max = int(math.floor(threshold))
    for k in range(0, k_max + 1):
        cumulative += math.exp(-lam) * (lam ** k) / math.factorial(k)
    return max(0.0, min(1.0, 1 - cumulative))


def generic_win_probability(score_diff: int, period: int,
                             total_periods: int) -> float:
    """
    Generic win probability for sports without a specific model.
    Uses a logistic curve based on lead size and how late in the game it is.
    """
    if score_diff <= 0:
        return 0.5

    # How far through the game (0 to 1)
    progress = min(1.0, period / max(total_periods, 1))

    # Larger leads and later game = higher win probability
    # Logistic function: bigger lead + later = more certain
    base = 1 / (1 + math.exp(-0.4 * score_diff))  # lead component
    time_factor = 0.5 + 0.5 * progress  # late game amplifies

    win_prob = 0.5 + (base - 0.5) * (1 + time_factor)
    return max(0.5, min(0.99, win_prob))


# ─────────────────────────────────────────────
#  TENNIS & UFC MODELS
# ─────────────────────────────────────────────

def tennis_win_probability(sets_won_leader: int, sets_won_trailer: int,
                            games_lead_current_set: int = 0,
                            best_of_5: bool = False) -> float:
    """
    Tennis match win probability based on sets won.
    A player leading in sets has a strong advantage.

    Historical conversion rates:
    - Up 1 set in best-of-3: ~78% win
    - Up 2 sets in best-of-5: ~90% win
    - One set from winning: ~85-95%
    """
    sets_to_win = 3 if best_of_5 else 2
    set_diff = sets_won_leader - sets_won_trailer

    # Leader needs (sets_to_win - sets_won_leader) more sets
    sets_needed = sets_to_win - sets_won_leader

    if sets_needed <= 0:
        return 0.99  # already won

    if best_of_5:
        # Best of 5 conversion rates
        prob_table = {
            (1, 0): 0.70, (2, 0): 0.90, (2, 1): 0.78,
            (1, 1): 0.50, (0, 0): 0.50,
        }
    else:
        # Best of 3 conversion rates
        prob_table = {
            (1, 0): 0.78, (0, 0): 0.50, (1, 1): 0.50,
        }

    base = prob_table.get((sets_won_leader, sets_won_trailer), None)
    if base is None:
        # Fallback: each set lead worth ~25%
        base = min(0.95, 0.5 + set_diff * 0.25)

    # Adjust for games lead in current set
    if games_lead_current_set >= 2:
        base = min(0.97, base + 0.05)

    return max(0.5, min(0.97, base))


def ufc_win_probability(round_num: int, is_winning: bool = False,
                         finish_threat: bool = False) -> float:
    """
    UFC fight win probability — intentionally conservative.

    UFC is the highest-variance sport: a losing fighter can win
    instantly with one strike or submission at any moment.

    We never assign high confidence to live UFC bets unless there's
    a clear finish threat. Most of the decision is deferred to Claude
    and the actual market odds.
    """
    if not is_winning:
        return 0.5

    # Even when "winning" a round, the comeback risk is high
    # Later rounds with a lead are slightly safer (less time left)
    base = 0.55

    if round_num >= 4:
        base = 0.62  # championship rounds, less time for comeback
    elif round_num == 3:
        base = 0.58

    if finish_threat:
        base = min(0.75, base + 0.15)

    # Cap UFC confidence low — variance is extreme
    return max(0.5, min(0.72, base))


# ─────────────────────────────────────────────
#  SPREAD (MARGIN OF VICTORY) MODELS
# ─────────────────────────────────────────────

def mlb_spread_probability(inning: int, current_diff: int,
                            spread_line: float) -> dict:
    """
    Probability the leading team wins by MORE than spread_line runs.

    current_diff: current run differential (positive = leader's lead)
    spread_line: the margin to cover (e.g. 1.5 means win by 2+)

    Uses expected remaining runs and variance to project final margin.
    """
    innings_left = max(0, 9 - inning)

    # Each team scores ~0.5 runs/inning. The margin can grow or shrink.
    # Expected change in margin = 0 (both teams score similarly)
    # but variance grows with innings remaining.
    expected_final_diff = current_diff  # margin expected to hold

    # Standard deviation of margin change grows with innings left
    # Each inning adds ~variance of 1.0 to the margin
    margin_std = math.sqrt(max(0.5, innings_left * 1.4))

    # P(final_diff > spread_line)
    if margin_std <= 0:
        cover_prob = 1.0 if expected_final_diff > spread_line else 0.0
    else:
        z = (expected_final_diff - spread_line) / margin_std
        cover_prob = _normal_cdf(z)

    return {
        "cover": round(cover_prob, 3),
        "no_cover": round(1 - cover_prob, 3),
        "expected_margin": round(expected_final_diff, 1),
        "margin_std": round(margin_std, 2),
    }


def soccer_spread_probability(clock_min: int, current_diff: int,
                               spread_line: float) -> dict:
    """
    Probability the leading soccer team wins by MORE than spread_line goals.
    """
    mins_left = max(0, 90 - clock_min)

    # Goals are rare; margin rarely changes late
    expected_final_diff = current_diff

    # Variance from remaining goals (Poisson-ish)
    # ~0.03 goals/min combined, margin std grows slowly
    margin_std = math.sqrt(max(0.3, mins_left * 0.03))

    if margin_std <= 0:
        cover_prob = 1.0 if expected_final_diff > spread_line else 0.0
    else:
        z = (expected_final_diff - spread_line) / margin_std
        cover_prob = _normal_cdf(z)

    return {
        "cover": round(cover_prob, 3),
        "no_cover": round(1 - cover_prob, 3),
        "expected_margin": round(expected_final_diff, 1),
    }


def basketball_spread_probability(minutes_remaining: float, current_diff: int,
                                   spread_line: float) -> dict:
    """
    Probability the leading team wins by MORE than spread_line points.
    Basketball margins are higher variance.
    """
    expected_final_diff = current_diff

    # NBA/WNBA margin std — roughly 2 points per sqrt(minute)
    margin_std = max(2.0, math.sqrt(max(1, minutes_remaining)) * 2.5)

    if margin_std <= 0:
        cover_prob = 1.0 if expected_final_diff > spread_line else 0.0
    else:
        z = (expected_final_diff - spread_line) / margin_std
        cover_prob = _normal_cdf(z)

    return {
        "cover": round(cover_prob, 3),
        "no_cover": round(1 - cover_prob, 3),
        "expected_margin": round(expected_final_diff, 1),
    }


def generic_spread_probability(sport: str, period: int, total_periods: int,
                                current_diff: int, spread_line: float,
                                clock_min: int = None) -> dict:
    """
    Dispatch to the right spread model based on sport.
    """
    if sport == "mlb":
        return mlb_spread_probability(period, current_diff, spread_line)
    elif sport in ("soccer", "world_cup", "epl"):
        cm = clock_min if clock_min is not None else period * 45
        return soccer_spread_probability(cm, current_diff, spread_line)
    elif sport in ("nba", "wnba"):
        mins = max(0, (total_periods - period) * 12)
        return basketball_spread_probability(mins, current_diff, spread_line)
    else:
        # Generic fallback
        periods_left = max(0, total_periods - period)
        expected = current_diff
        std = max(1.0, math.sqrt(max(0.5, periods_left)) * 2.0)
        z = (expected - spread_line) / std if std > 0 else 0
        cover = _normal_cdf(z)
        return {"cover": round(cover, 3), "no_cover": round(1 - cover, 3),
                "expected_margin": round(expected, 1)}


def nfl_spread_probability(period: int, mins_remaining: float,
                           current_diff: int, spread_line: float) -> dict:
    """
    Probability the leading NFL team wins by MORE than spread_line points.
    """
    total_game_mins = 60.0
    mins_elapsed = max(1, (period - 1) * 15 + (15 - mins_remaining)) if period <= 4 \
        else total_game_mins
    mins_left = max(0, total_game_mins - mins_elapsed)

    expected_final_diff = current_diff
    # NFL margin std — scoring comes in chunks of 3 and 7
    margin_std = max(3.0, math.sqrt(max(1, mins_left)) * 2.0)

    if margin_std <= 0:
        cover_prob = 1.0 if expected_final_diff > spread_line else 0.0
    else:
        z = (expected_final_diff - spread_line) / margin_std
        cover_prob = _normal_cdf(z)

    return {
        "cover": round(cover_prob, 3),
        "no_cover": round(1 - cover_prob, 3),
        "expected_margin": round(expected_final_diff, 1),
    }


def nhl_spread_probability(period: int, mins_remaining: float,
                           current_diff: int, spread_line: float = 1.5) -> dict:
    """
    Probability the leading NHL team wins by MORE than spread_line goals.
    NHL spreads are usually 1.5 (the "puck line").
    """
    total_game_mins = 60.0
    mins_elapsed = max(1, (period - 1) * 20 + (20 - mins_remaining)) if period <= 3 \
        else total_game_mins
    mins_left = max(0, total_game_mins - mins_elapsed)

    expected_final_diff = current_diff
    # NHL low-scoring — but empty-net goals late often pad leads
    # A 2-goal lead late often becomes 3 via empty net
    if current_diff >= 1 and mins_left <= 5:
        expected_final_diff += 0.4  # empty net adjustment

    margin_std = max(0.8, math.sqrt(max(0.3, mins_left * 0.09)))

    if margin_std <= 0:
        cover_prob = 1.0 if expected_final_diff > spread_line else 0.0
    else:
        z = (expected_final_diff - spread_line) / margin_std
        cover_prob = _normal_cdf(z)

    return {
        "cover": round(cover_prob, 3),
        "no_cover": round(1 - cover_prob, 3),
        "expected_margin": round(expected_final_diff, 1),
    }
