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
    "tennis_atp": f"{ESPN_BASE}/tennis/atp/scoreboard",
    "tennis_wta": f"{ESPN_BASE}/tennis/wta/scoreboard",
}

PGA_LEADERBOARD_URL = f"{ESPN_BASE}/golf/pga/leaderboard"


def fetch_pga_leaderboard(session: requests.Session = None) -> dict:
    """
    Fetch current PGA Tour leaderboard.
    Returns dict with tournament info and top players.
    """
    if session is None:
        session = requests.Session()
    try:
        r = session.get(
            PGA_LEADERBOARD_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        tournaments = data.get("events", [])
        if not tournaments:
            return {}

        event = tournaments[0]
        event_name = event.get("name", "PGA Tournament")
        status = event.get("status", {})
        in_progress = status.get("type", {}).get("state", "") in ("in", "pre")
        round_num = status.get("period", 0)

        competitors = event.get("competitors", [])
        players = []
        for c in competitors[:20]:  # top 20 only
            athlete = c.get("athlete", {})
            stats = c.get("statistics", [])
            score_stat = next((s for s in stats if s.get("name") == "score"), {})
            pos = c.get("status", {}).get("position", {}).get("displayName", "?")
            score = score_stat.get("displayValue", "E")
            players.append({
                "name": athlete.get("displayName", "?"),
                "last_name": athlete.get("lastName", "?"),
                "position": pos,
                "score": score,
                "thru": c.get("status", {}).get("thru", ""),
            })

        return {
            "sport": "pga",
            "tournament": event_name,
            "round": round_num,
            "in_progress": in_progress,
            "players": players,
            "is_live": in_progress,
            "raw_name": event_name,
            "short_name": event_name,
        }
    except Exception as e:
        log.debug("PGA leaderboard fetch failed: %s", e)
        return {}


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
            # Team record (e.g. "45-30") — useful pregame signal
            records = c.get("records", [])
            overall_record = ""
            for rec in records:
                if rec.get("type") == "total" or rec.get("name") == "overall":
                    overall_record = rec.get("summary", "")
                    break
            if not overall_record and records:
                overall_record = records[0].get("summary", "")
            teams.append({
                "name": team.get("displayName") or team.get("name", "?"),
                "abbreviation": team.get("abbreviation", "?"),
                "score": c.get("score", "0"),
                "home_away": c.get("homeAway", "?"),
                "winner": c.get("winner", False),
                "record": overall_record,
                "ml_odds": c.get("odds", {}).get("moneyLine", None) if isinstance(c.get("odds"), dict) else None,
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

        # Game date from ESPN
        game_date = event.get("date", "")[:10]  # "2026-06-21"

        return {
            "name": name,
            "short_name": short_name,
            "sport": sport,
            "state": state,
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
            "is_pregame": state == "pre",
            "espn_spread": spread,
            "espn_total": over_under,
            "raw_name": name,
            "game_date": game_date,
            # Unique game ID includes date to distinguish series games
            "game_id": f"{short_name}_{game_date}" if game_date else short_name,
        }
    except Exception as e:
        log.debug("Failed to parse ESPN event: %s", e)
        return None


def fetch_all_live_games(session: requests.Session = None) -> list:
    """Fetch live games across all supported sports including PGA."""
    all_games = []
    sports = ["world_cup", "soccer", "nba", "nfl", "mlb", "nhl",
              "ufc", "tennis_atp", "tennis_wta"]
    for sport in sports:
        games = fetch_live_games(sport, session)
        live = [g for g in games if g.get("is_live")]
        if live:
            log.info("Found %d live %s games", len(live), sport)
            all_games.extend(live)

    # PGA Tour leaderboard
    pga = fetch_pga_leaderboard(session)
    if pga and pga.get("in_progress") and pga.get("players"):
        log.info("PGA Tour in progress: %s (Round %s)",
                 pga.get("tournament", "?"), pga.get("round", "?"))
        all_games.append(pga)

    return all_games


def match_game_to_market(game: dict, market_question: str,
                         market_ticker: str = "") -> float:
    """
    Returns confidence score (0-1) that a game matches a Kalshi market.
    Uses Kalshi ticker abbreviations as primary signal.
    e.g. KXMLBGAME-26JUN192145MINAZ → MIN vs AZ (Minnesota vs Arizona)
    """
    import re as _re
    score = 0.0
    teams = game.get("teams", [])
    ticker_upper = market_ticker.upper() if market_ticker else ""

    # ESPN → Kalshi ticker code mappings
    # Kalshi uses non-standard codes for some teams (ARI→AZ, OAK→ATH, etc)
    ABBREV_ALIASES = {
        "ARI": ["AZ", "ARI"], "ATH": ["ATH", "OAK"], "OAK": ["ATH", "OAK"],
        "SD": ["SD"], "SF": ["SF"], "KC": ["KC"], "LAA": ["LAA"],
        "LAD": ["LAD"], "NYM": ["NYM"], "NYY": ["NYY"], "TB": ["TB"],
        "WSH": ["WSH", "WAS"], "CWS": ["CWS"], "MIN": ["MIN"],
        "BOS": ["BOS"], "SEA": ["SEA"], "BAL": ["BAL"], "TOR": ["TOR"],
        "HOU": ["HOU"], "TEX": ["TEX"], "MIL": ["MIL"], "STL": ["STL"],
        "PIT": ["PIT"], "CIN": ["CIN"], "CHC": ["CHC"], "COL": ["COL"],
        "DET": ["DET"], "CLE": ["CLE"], "MIA": ["MIA"], "PHI": ["PHI"],
        "ATL": ["ATL"], "NY": ["NY"], "PHX": ["PHX"], "LV": ["LV"],
        "LA": ["LA"], "IND": ["IND"], "CHI": ["CHI"], "GS": ["GS"],
        "CON": ["CON"], "DAL": ["DAL"], "PDX": ["PDX"],
    }

    # Extract team code suffix from Kalshi ticker
    ticker_team_part = ""
    ticker_date_str = ""
    if ticker_upper:
        parts = ticker_upper.split("-")
        seg = parts[1] if len(parts) >= 2 else parts[0]
        team_match = _re.search(r"([A-Z]{3,})$", seg)
        if team_match:
            ticker_team_part = team_match.group(1)
        # Extract date: 26JUN21 from e.g. "26JUN211605LAAATH"
        date_match = _re.match(r"(\d{2}[A-Z]{3}\d{2})", seg)
        if date_match:
            ticker_date_str = date_match.group(1)

    # Game date from ESPN
    game_date = game.get("game_date", "")  # "2026-06-20"

    # Score: how many teams match the ticker
    if ticker_team_part:
        matched = 0
        for team in teams:
            espn_abbr = team.get("abbreviation", "").upper()
            aliases = ABBREV_ALIASES.get(espn_abbr, [espn_abbr])
            for alias in aliases:
                if alias in ticker_team_part:
                    matched += 1
                    break
        if matched >= 2:
            score += 0.8
        elif matched == 1:
            score += 0.4

    # Date check — penalize if ticker date is too far from game date.
    # Allow ±1 day because games starting late evening ET cross midnight UTC,
    # so a game ESPN dates 2026-06-23 may have a Kalshi ticker dated 26JUN22.
    if score > 0 and ticker_date_str and game_date:
        month_map = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05",
                     "JUN":"06","JUL":"07","AUG":"08","SEP":"09",
                     "OCT":"10","NOV":"11","DEC":"12"}
        dm = _re.match(r"(\d{2})([A-Z]{3})(\d{2})", ticker_date_str)
        if dm:
            ticker_date = (f"20{dm.group(1)}-"
                          f"{month_map.get(dm.group(2), '00')}-"
                          f"{dm.group(3)}")
            # Compare as dates with 1-day tolerance
            try:
                from datetime import datetime as _dt
                td = _dt.strptime(ticker_date, "%Y-%m-%d")
                gd = _dt.strptime(game_date, "%Y-%m-%d")
                day_diff = abs((td - gd).days)
                if day_diff == 0:
                    pass  # exact match, no penalty
                elif day_diff == 1:
                    score *= 0.85  # adjacent day (midnight crossover) — small penalty
                else:
                    score = 0.0  # 2+ days off — definitely wrong game
            except ValueError:
                if ticker_date != game_date:
                    score = 0.0

    # Secondary: question text matching (critical for tennis/UFC player names)
    q_lower = market_question.lower() if market_question else ""
    sport = game.get("sport", "")
    is_individual = sport in ("tennis", "tennis_atp", "tennis_wta", "ufc", "mma")

    if q_lower:
        matched_names = 0
        for team in teams:
            name = (team.get("name") or team.get("displayName") or "").lower()
            # Match on surname (last word) — most reliable for athletes
            words = [w for w in name.split() if len(w) > 2]
            # For individuals, the last name is the key identifier
            surname = words[-1] if words else ""
            if surname and surname in q_lower:
                matched_names += 1
            elif any(w in q_lower for w in words if len(w) > 3):
                matched_names += 0.5

        if is_individual:
            # Tennis/UFC — player name match is the primary signal
            if matched_names >= 2:
                score = max(score, 0.8)  # both players named
            elif matched_names >= 1:
                score = max(score, 0.5)  # one player named
        else:
            # Team sports — name match is secondary boost
            if matched_names >= 1 and score < 0.5:
                score += 0.2

    return min(score, 1.0)

def format_game_context(game: dict) -> str:
    """Format game state as a string for Claude's prompt."""
    teams = game.get("teams", [])
    team_strs = []
    for t in teams:
        team_strs.append(
            f"{t.get('name','?')} ({t.get('home_away','?')}): {t.get('score','0')}"
        )

    score_line = " vs ".join(team_strs)
    clock = game.get("clock", "")
    period = game.get("period", "")
    status = game.get("status", "")

    recent = game.get("recent_plays", [])
    recent_str = ""
    if recent:
        plays = [f"- {p.get('period','')} {p.get('clock','')}: {p.get('text','')}"
                 for p in recent]
        recent_str = "\nRecent scoring:\n" + "\n".join(plays)

    sport = game.get("sport", "").upper()

    return (
        f"[LIVE {sport}] {game.get('short_name', '')}\n"
        f"Score: {score_line}\n"
        f"Time: {clock} | Period/Half: {period} | Status: {status}"
        f"{recent_str}"
    )
