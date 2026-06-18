"""
Live Sports Data Fetcher
========================
Fetches real-time game data from ESPN's unofficial API (no key needed)
and other free sources to give Claude live context for in-game betting.

Supports: Soccer (World Cup, EPL, etc.), NBA, NFL, UFC, MLB, NHL
"""

import re
import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("polycopy.sports")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_SCORE_BASE = "https://site.web.api.espn.com/apis/site/v2/sports"

SPORT_ENDPOINTS = {
    "soccer": f"{ESPN_BASE}/soccer/all/scoreboard",
    "world_cup": f"{ESPN_BASE}/soccer/fifa.world/scoreboard",
    "epl": f"{ESPN_BASE}/soccer/eng.1/scoreboard",
    "nba": f"{ESPN_BASE}/basketball/nba/scoreboard",
    "nfl": f"{ESPN_BASE}/football/nfl/scoreboard",
    "ufc": f"{ESPN_BASE}/mma/ufc/scoreboard",
    "mlb": f"{ESPN_BASE}/baseball/mlb/scoreboard",
    "nhl": f"{ESPN_BASE}/hockey/nhl/scoreboard",
}


def fetch_live_games(sport: str = "soccer", session: requests.Session = None) -> list:
    """
    Fetch currently live or recently completed games for a sport.
    Returns list of game context dicts.
    """
    if session is None:
        session = requests.Session()

    endpoint = SPORT_ENDPOINTS.get(sport, SPORT_ENDPOINTS["soccer"])

    try:
        r = session.get(endpoint, timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        events = data.get("events", [])
        games = []
        for event in events:
            game = parse_espn_event(event, sport)
            if game:
                games.append(game)
        return games
    except Exception as e:
        log.warning("Failed to fetch %s games: %s", sport, e)
        return []


def parse_espn_event(event: dict, sport: str) -> dict | None:
    """Parse an ESPN event into a clean game context dict."""
    try:
        name = event.get("name", "")
        short_name = event.get("shortName", name)
        status = event.get("status", {})
        status_type = status.get("type", {})
        state = status_type.get("state", "")  # pre, in, post
        detail = status.get("displayClock", "")
        period = status.get("period", 0)
        status_desc = status_type.get("description", "")

        competitions = event.get("competitions", [{}])
        comp = competitions[0] if competitions else {}
        competitors = comp.get("competitors", [])

        teams = []
        for c in competitors:
            team = c.get("team", {})
            teams.append({
                "name": team.get("displayName") or team.get("name", "?"),
                "abbreviation": team.get("abbreviation", "?"),
                "score": c.get("score", "0"),
                "home_away": c.get("homeAway", "?"),
                "winner": c.get("winner", False),
            })

        # Key stats / situation
        situation = comp.get("situation", {})
        possession = situation.get("possession", "")

        # Recent scoring plays
        scoring_plays = comp.get("scoringPlays", [])
        recent_plays = []
        for play in scoring_plays[-3:]:  # last 3 scoring events
            recent_plays.append({
                "text": play.get("text", ""),
                "period": play.get("period", {}).get("displayValue", ""),
                "clock": play.get("clock", {}).get("displayValue", ""),
            })

        # Odds from ESPN if available
        odds_data = comp.get("odds", [{}])
        odds = odds_data[0] if odds_data else {}
        spread = odds.get("details", "")
        over_under = odds.get("overUnder", "")

        return {
            "name": name,
            "short_name": short_name,
            "sport": sport,
            "state": state,           # "pre", "in", "post"
            "is_live": state == "in",
            "is_finished": state == "post",
            "status": status_desc,
            "clock": detail,
            "period": period,
            "teams": teams,
            "recent_plays": recent_plays,
            "possession": possession,
            "spread": spread,
            "over_under": over_under,
            "raw_name": name,
        }
    except Exception as e:
        log.debug("Failed to parse ESPN event: %s", e)
        return None


def fetch_all_live_games(session: requests.Session = None) -> list:
    """Fetch live games across all supported sports."""
    all_games = []
    sports = ["world_cup", "soccer", "nba", "nfl", "mlb", "nhl"]
    for sport in sports:
        games = fetch_live_games(sport, session)
        live = [g for g in games if g.get("is_live")]
        if live:
            log.info("Found %d live %s games", len(live), sport)
            all_games.extend(live)
    return all_games


def match_game_to_market(game: dict, market_question: str,
                         market_ticker: str = "") -> float:
    """
    Returns a confidence score (0-1) that a game matches a Kalshi market.

    Primary matching: Kalshi tickers embed team codes and dates.
    e.g. KXMLBGAME-26JUN172140PITATH → PIT vs ATH on June 17
         KXWCGAME-26JUN18CZERSA → CZE vs RSA on June 18

    Secondary matching: question text keyword overlap.
    """
    score = 0.0
    teams = game.get("teams", [])
    ticker_upper = market_ticker.upper() if market_ticker else ""

    # --- Primary: match team abbreviations in Kalshi ticker ---
    # Kalshi ticker format: KXMLBGAME-26JUN172140PITATH
    # Date is encoded as YYMMMDD (e.g. 26JUN17 = June 17, 2026)
    ticker_team_part = ""
    ticker_date_str = ""
    if ticker_upper:
        parts = ticker_upper.split("-")
        if len(parts) >= 2:
            last = parts[-1]
            import re as _re
            # Extract team codes (letters only at end)
            team_match = _re.search(r'([A-Z]{6,})$', last)
            if team_match:
                ticker_team_part = team_match.group(1)
            # Extract date (e.g. 26JUN17)
            date_match = _re.search(r'(\d{2}[A-Z]{3}\d{2})', last)
            if date_match:
                ticker_date_str = date_match.group(1)

    abbrevs = []
    full_names = []
    for team in teams:
        abbr = team.get("abbreviation", "").upper()
        name = (team.get("name") or team.get("displayName") or "").lower()
        if abbr:
            abbrevs.append(abbr)
        if name:
            full_names.append(name)
            # Also add last word (e.g. "Pittsburgh Pirates" → "pirates")
            words = name.split()
            if words:
                full_names.append(words[-1])
                full_names.append(words[0])

    # Score ticker abbreviation matches (highest weight)
    if ticker_team_part and abbrevs:
        matched_abbrevs = sum(1 for a in abbrevs if a in ticker_team_part)
        if matched_abbrevs == 2:
            score += 0.8  # both teams match — very confident
        elif matched_abbrevs == 1:
            score += 0.4  # one team matches

    # --- Secondary: question text keyword matching ---
    q_lower = market_question.lower() if market_question else ""
    if q_lower:
        for name in full_names:
            if name and len(name) > 3 and name in q_lower:
                score += 0.2
                break

    return min(score, 1.0)


def format_game_context(game: dict) -> str:
    """Format game state as a string for Claude's prompt."""
    teams = game.get("teams", [])
    team_strs = []
    for t in teams:
        team_strs.append(f"{t['name']} ({t['home_away']}): {t['score']}")

    score_line = " vs ".join(team_strs)
    clock = game.get("clock", "")
    period = game.get("period", "")
    status = game.get("status", "")

    recent = game.get("recent_plays", [])
    recent_str = ""
    if recent:
        plays = [f"- {p['period']} {p['clock']}: {p['text']}" for p in recent]
        recent_str = "\nRecent scoring:\n" + "\n".join(plays)

    sport = game.get("sport", "").upper()

    return (
        f"[LIVE {sport}] {game.get('short_name', '')}\n"
        f"Score: {score_line}\n"
        f"Time: {clock} | Period/Half: {period} | Status: {status}"
        f"{recent_str}"
    )
