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
          "outcome": "Yes" / "No" / fighter name / etc,
          "url": "https://polymarket.com/event/...",
          "end_date": "2024-11-05",
          "liquidity": 12345.0,
          "category": "SPORTS",
        }
        Falls back gracefully if market lookup fails.
        """
        market = self.get_market_by_condition_id(condition_id)
        if not market:
            return {
                "question": f"Unknown market ({condition_id[:12]}...)",
                "outcome": f"token {token_id[:8]}...",
                "url": "",
                "end_date": "",
                "liquidity": 0,
                "category": "",
            }

        question = market.get("question") or market.get("title", "Unknown market")

        # end date — strip time component
        end_date = market.get("endDate") or market.get("end_date", "")
        if end_date and "T" in end_date:
            end_date = end_date.split("T")[0]

        liquidity = float(market.get("liquidity", 0) or 0)

        # URL from slug
        slug = market.get("slug") or market.get("marketSlug", "")
        url = f"https://polymarket.com/event/{slug}" if slug else ""

        # Category from tags array: [{"id": 1, "label": "Sports", "slug": "sports"}, ...]
        category = ""
        tags = market.get("tags") or []
        if isinstance(tags, list) and tags:
            # Take the first tag's label as the category
            first_tag = tags[0]
            if isinstance(first_tag, dict):
                category = first_tag.get("label") or first_tag.get("slug", "")
        if not category:
            # fallback: try direct category field
            category = market.get("category") or market.get("categoryName", "")

        # ---------------------------------------------------------------
        # Outcome matching
        # clobTokenIds and outcomes are returned as JSON-encoded strings
        # e.g. clobTokenIds = '["12345...","67890..."]'
        #      outcomes      = '["Justin Gaethje","Ilia Topuria"]'
        # ---------------------------------------------------------------
        outcome = "Unknown outcome"
        token_id_str = str(token_id)

        raw_token_ids = market.get("clobTokenIds") or market.get("tokens") or []
        raw_outcomes = market.get("outcomes") or []

        # Parse if JSON-encoded strings
        if isinstance(raw_token_ids, str):
            try:
                import json as _json
                raw_token_ids = _json.loads(raw_token_ids)
            except Exception:
                raw_token_ids = []

        if isinstance(raw_outcomes, str):
            try:
                import json as _json
                raw_outcomes = _json.loads(raw_outcomes)
            except Exception:
                raw_outcomes = []

        # Match token_id to outcome name via parallel index
        if isinstance(raw_token_ids, list) and isinstance(raw_outcomes, list):
            for i, tid in enumerate(raw_token_ids):
                if str(tid) == token_id_str and i < len(raw_outcomes):
                    outcome = raw_outcomes[i]
                    break

        # Fallback: tokens may be list of dicts
        if outcome == "Unknown outcome":
            tokens_list = market.get("tokens") or []
            if isinstance(tokens_list, list):
                for t in tokens_list:
                    if isinstance(t, dict):
                        if str(t.get("token_id") or t.get("tokenId", "")) == token_id_str:
                            outcome = t.get("outcome", "Unknown outcome")
                            break

        return {
            "question": question,
            "outcome": outcome,
            "url": url,
            "end_date": end_date,
            "liquidity": liquidity,
            "category": category,
        }
