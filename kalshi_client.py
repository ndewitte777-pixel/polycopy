"""
Kalshi API Client
=================
Handles authentication, market data, and order placement for Kalshi.

Authentication uses RSA-PSS signed requests:
- KALSHI-ACCESS-KEY: your API key ID
- KALSHI-ACCESS-TIMESTAMP: current timestamp in ms
- KALSHI-ACCESS-SIGNATURE: RSA-PSS signature of the request

Set in Railway Variables:
- KALSHI_API_KEY_ID    : from kalshi.com Settings -> API
- KALSHI_PRIVATE_KEY   : RSA private key (PEM format)
- KALSHI_USE_DEMO      : "true" for paper trading, "false" for live
"""

import base64
import hashlib
import logging
import os
import time
import requests
from datetime import datetime, timezone

log = logging.getLogger("polycopy.kalshi")

KALSHI_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"


class KalshiClient:
    def __init__(self, api_key_id: str, private_key_pem: str, use_demo: bool = False):
        self.api_key_id = api_key_id
        self.private_key_pem = private_key_pem
        self.base_url = KALSHI_DEMO_URL if use_demo else KALSHI_BASE_URL
        self.session = requests.Session()
        self._load_private_key()
        log.info("Kalshi client initialized (%s)", "DEMO" if use_demo else "LIVE")

    def _load_private_key(self):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        pem = self.private_key_pem
        if not pem.startswith("-----"):
            # Raw base64 key — wrap it
            pem = f"-----BEGIN PRIVATE KEY-----\n{pem}\n-----END PRIVATE KEY-----"
        self._private_key = load_pem_private_key(pem.encode(), password=None)

    def _sign_request(self, method: str, path: str, body: str = "") -> dict:
        """Generate RSA-PSS signed headers for Kalshi API."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp_ms = str(int(time.time() * 1000))
        msg = timestamp_ms + method.upper() + path + body
        signature = self._private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        headers = self._sign_request("GET", path)
        r = self.session.get(
            self.base_url + path,
            headers=headers,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        import json
        body_str = json.dumps(body)
        headers = self._sign_request("POST", path, body_str)
        r = self.session.post(
            self.base_url + path,
            headers=headers,
            data=body_str,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    # ── Account ──────────────────────────────────────────────────
    def get_balance(self) -> float:
        """Returns available USDC balance."""
        try:
            data = self._get("/portfolio/balance")
            # Balance returned in cents
            cents = data.get("balance", 0) or 0
            return float(cents) / 100
        except Exception as e:
            log.warning("Failed to fetch balance: %s", e)
            return 0.0

    def get_positions(self) -> list:
        """Returns current open positions."""
        try:
            data = self._get("/portfolio/positions")
            return data.get("market_positions", [])
        except Exception as e:
            log.warning("Failed to fetch positions: %s", e)
            return []

    # ── Markets ───────────────────────────────────────────────────
    def get_markets(self, limit: int = 100, status: str = "open",
                    min_close_ts: int = None, max_close_ts: int = None) -> list:
        """Fetch active markets."""
        try:
            params = {"limit": limit, "status": status}
            if min_close_ts:
                params["min_close_ts"] = min_close_ts
            if max_close_ts:
                params["max_close_ts"] = max_close_ts
            data = self._get("/markets", params=params)
            return data.get("markets", [])
        except Exception as e:
            log.error("Failed to fetch markets: %s", e)
            return []

    def get_market(self, ticker: str) -> dict:
        """Fetch a single market by ticker."""
        try:
            return self._get(f"/markets/{ticker}")
        except Exception as e:
            log.warning("Failed to fetch market %s: %s", ticker, e)
            return {}

    def get_events(self, limit: int = 50, status: str = "open") -> list:
        """Fetch active events (groups of related markets)."""
        try:
            data = self._get("/events", params={"limit": limit, "status": status})
            return data.get("events", [])
        except Exception as e:
            log.error("Failed to fetch events: %s", e)
            return []

    def get_market_price(self, ticker: str) -> tuple[float, float]:
        """Returns (yes_price, no_price) as floats 0-1."""
        try:
            data = self._get(f"/markets/{ticker}")
            market = data.get("market", data)
            yes = float(market.get("yes_ask", market.get("yes_bid", 0.5)) or 0.5)
            no = float(market.get("no_ask", market.get("no_bid", 0.5)) or 0.5)
            # Kalshi prices are in dollars (0.00-1.00) after March 2026 migration
            return yes, no
        except Exception as e:
            log.warning("Failed to fetch price for %s: %s", ticker, e)
            return 0.5, 0.5

    # ── Orders ────────────────────────────────────────────────────
    def place_order(self, ticker: str, side: str, count: int,
                    price_dollars: float, order_type: str = "limit") -> dict:
        """
        Place an order on Kalshi.
        ticker: market ticker e.g. 'KXNFLGAME-25OCT12CLEPIT'
        side: 'yes' or 'no'
        count: number of contracts (each contract = $1 max payout)
        price_dollars: price per contract in dollars (0.01 to 0.99)
        """
        import uuid
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "type": order_type,
            "action": "buy",
            "side": side.lower(),
            "count": count,
            f"{side.lower()}_price": f"{price_dollars:.4f}",
        }
        return self._post("/portfolio/orders", body)

    def get_trades(self, ticker: str = None, limit: int = 50) -> list:
        """Fetch recent trades for a market or all markets."""
        try:
            params = {"limit": limit}
            if ticker:
                params["ticker"] = ticker
            data = self._get("/markets/trades", params=params)
            return data.get("trades", [])
        except Exception as e:
            log.warning("Failed to fetch trades: %s", e)
            return []
