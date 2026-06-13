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

    # ---------------------------------------------------------------
    def get_leaderboard(self, limit=20, period="month", order_by="pnl"):
        """
        Fetch top traders.
        order_by: 'pnl' or 'volume' (API specifics may vary; adjust as needed).
        """
        url = f"{DATA_API_URL}/leaderboard"
        params = {"limit": limit}
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
        """Fetch market metadata (liquidity, tokens, etc.) via Gamma API."""
        url = f"{GAMMA_API_URL}/markets"
        params = {"condition_ids": condition_id}
        try:
            r = self.session.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
            return data
        except Exception as e:
            log.error("Failed to fetch market %s: %s", condition_id, e)
            return None
