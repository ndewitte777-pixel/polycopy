"""
Execution layer: wraps py-clob-client using API key authentication.
In DRY_RUN mode, no real client is created and orders are only logged.
"""

import logging
import requests
from config import (
    CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE,
    CLOB_API_URL, POLYGON_CHAIN_ID, DRY_RUN, YOUR_BANKROLL_USDC,
)

log = logging.getLogger("polycopy.executor")


class Executor:
    def __init__(self):
        self.client = None
        if not DRY_RUN:
            self._init_client()

    def _init_client(self):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if not CLOB_API_KEY or not CLOB_API_SECRET:
            raise RuntimeError(
                "CLOB_API_KEY or CLOB_API_SECRET is empty. "
                "Set them in Railway Variables before going live."
            )

        self.client = ClobClient(
            CLOB_API_URL,
            chain_id=POLYGON_CHAIN_ID,
            creds=ApiCreds(
                api_key=CLOB_API_KEY,
                api_secret=CLOB_API_SECRET,
                api_passphrase=CLOB_API_PASSPHRASE,
            ),
        )
        log.info("CLOB client initialized with API keys (LIVE TRADING ENABLED)")

    # ------------------------------------------------------------
    def get_balance(self) -> float:
        """Fetch real USDC balance. Falls back to YOUR_BANKROLL_USDC on error."""
        if DRY_RUN or not self.client:
            return YOUR_BANKROLL_USDC

        try:
            # py-clob-client stores the wallet address on the client
            address = getattr(self.client, "address", None)
            if not address:
                return YOUR_BANKROLL_USDC

            # Fetch via Polygon RPC - balanceOf(address) on USDC contract
            USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            data = "0x70a08231" + address[2:].lower().zfill(64)
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{"to": USDC_CONTRACT, "data": data}, "latest"],
                "id": 1,
            }
            resp = requests.post("https://polygon-rpc.com", json=payload, timeout=8)
            result = resp.json().get("result", "0x0")
            raw = int(result, 16) / 1_000_000  # USDC has 6 decimals
            log.info("Live wallet balance: $%.2f USDC", raw)
            return raw if raw > 0 else YOUR_BANKROLL_USDC

        except Exception as e:
            log.warning("Could not fetch live balance, using config value: %s", e)
            return YOUR_BANKROLL_USDC

    # ------------------------------------------------------------
    def place_order(self, token_id: str, side: str, price: float, size_usdc: float):
        """
        Place a market order.
        side: 'BUY' or 'SELL'
        price: current price (0-1) for the outcome token
        size_usdc: amount in USDC to spend
        """
        if DRY_RUN:
            log.info(
                "[DRY RUN] Would place %s order: token_id=%s price=%.4f size_usdc=%.2f",
                side, token_id, price, size_usdc,
            )
            return {"dry_run": True, "status": "simulated"}

        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        side_const = BUY if side == "BUY" else SELL

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size_usdc,
            side=side_const,
            order_type=OrderType.FOK,
        )

        try:
            signed = self.client.create_market_order(order_args)
            resp = self.client.post_order(signed, OrderType.FOK)
            log.info("Order placed: %s", resp)
            return resp
        except Exception as e:
            log.error("Order failed: %s", e)
            return {"error": str(e)}
