"""
Thin wrapper around Polymarket's public Data/Gamma APIs.
No authentication required for these endpoints.
"""

import requests
import logging
from config import DATA_API_URL, GAMMA_API_URL

log = logging.getLogger("polycopy.data")


class DataAPI:
    def __init__(self, session: requests.Session = None):
        self.session = session or requests.Session()
        self._market_cache = {}  # condition_id -> market info

    # ---------------------------------------------------------------
    def get_leaderboard(self, limit=20, time_period="MONTH", order_by="PNL", category="OVERALL"):
        """
        Fetch top traders.
        time_period: DAY, WEEK, MONTH, ALL
        order_by: PNL or VOL
        category: OVERALL, POLITICS, SPORTS, CRYPTO, CULTURE, MENTIONS, WEATHER, ECONOMICS, TECH, FINANCE
        """
        url = f"{DATA_API_URL}/v1/leaderboard"
        params = {
            "limit": limit,
            "timePeriod": time_period,
            "orderBy": order_by,
            "category": category,
        }
        try:
            r = self.session.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Failed to fetch leaderboard: %s", e)
            return []

    # ---------------------------------------------------------------
    def get_activity(self, user: str, limit=50, types=("TRADE",), start=None):
        """
        Fetch recent on-chain activity for a wallet address.
        Returns most-recent-first list of activity dicts.
        """
        url = f"{DATA_API_URL}/activity"
        params = {
            "user": user,
            "limit": limit,
            "type": ",".join(types),
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if start:
            params["start"] = start
        try:
            r = self.session.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Failed to fetch activity for %s: %s", user, e)
            return []

    # ---------------------------------------------------------------
    def get_positions(self, user: str):
        """Fetch current open positions for a wallet (used to estimate bankroll %)."""
        url = f"{DATA_API_URL}/positions"
        params = {"user": user}
        try:
            r = self.session.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Failed to fetch positions for %s: %s", user, e)
            return []

    # ---------------------------------------------------------------
    def get_market_by_condition_id(self, condition_id: str):
        """Fetch market metadata (liquidity, tokens, outcome names, etc.) via Gamma API.
        Results are cached in memory to avoid redundant API calls."""
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        url = f"{GAMMA_API_URL}/markets"
        params = {"condition_ids": condition_id}
        try:
            r = self.session.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            market = data[0] if isinstance(data, list) and data else data
            if market:
                self._market_cache[condition_id] = market
            return market
        except Exception as e:
            log.error("Failed to fetch market %s: %s", condition_id, e)
            return None

    # ---------------------------------------------------------------
    def get_market_info(self, condition_id: str, token_id: str) -> dict:
        """
        Returns a human-readable dict about a market/outcome:
        {
          "question": "Will X happen?",
          "outcome": "Yes" or "No" or outcome name,
          "url": "https://polymarket.com/event/...",
          "end_date": "2024-11-05",
          "liquidity": 12345.0,
        }
        Falls back to truncated IDs if market lookup fails.
        """
        market = self.get_market_by_condition_id(condition_id)
        if not market:
            return {
                "question": f"Unknown market ({condition_id[:12]}...)",
                "outcome": f"token {token_id[:8]}...",
                "url": "",
                "end_date": "",
                "liquidity": 0,
            }

        question = market.get("question") or market.get("title", "Unknown market")
        end_date = market.get("endDate") or market.get("end_date", "")
        if end_date and "T" in end_date:
            end_date = end_date.split("T")[0]
        liquidity = float(market.get("liquidity", 0) or 0)
        slug = market.get("slug") or market.get("marketSlug", "")
        url = f"https://polymarket.com/event/{slug}" if slug else ""

        # Match token_id to an outcome name
        outcome = "Unknown outcome"
        tokens = market.get("tokens") or market.get("clobTokenIds") or []
        outcomes = market.get("outcomes") or []

        # tokens can be a list of dicts or list of strings
        if isinstance(tokens, list) and tokens:
            if isinstance(tokens[0], dict):
                for t in tokens:
                    if str(t.get("token_id") or t.get("tokenId", "")) == str(token_id):
                        outcome = t.get("outcome", "Unknown")
                        break
            elif isinstance(tokens[0], str) and outcomes:
                # parallel lists: clobTokenIds[i] matches outcomes[i]
                for i, tid in enumerate(tokens):
                    if str(tid) == str(token_id) and i < len(outcomes):
                        outcome = outcomes[i]
                        break

        return {
            "question": question,
            "outcome": outcome,
            "url": url,
            "end_date": end_date,
            "liquidity": liquidity,
        }
