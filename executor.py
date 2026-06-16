"""
Execution layer: wraps py-clob-client.
Uses API keys for authentication AND private key for order signing.
Both are required by py-clob-client to place orders on Polymarket.

Set in Railway Variables:
- CLOB_API_KEY       : from Polymarket profile -> API Keys
- CLOB_API_SECRET    : from Polymarket profile -> API Keys  
- CLOB_API_PASSPHRASE: from Polymarket profile -> API Keys
- PRIVATE_KEY        : your wallet private key (for signing orders)
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
        import os
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if not CLOB_API_KEY or not CLOB_API_SECRET:
            raise RuntimeError(
                "CLOB_API_KEY or CLOB_API_SECRET is empty. "
                "Set them in Railway Variables."
            )

        private_key = os.environ.get("PRIVATE_KEY", "")
        if not private_key:
            raise RuntimeError(
                "PRIVATE_KEY is required for signing orders. "
                "Set it in Railway Variables (your wallet private key)."
            )

        self.client = ClobClient(
            CLOB_API_URL,
            key=private_key,
            chain_id=POLYGON_CHAIN_ID,
            creds=ApiCreds(
                api_key=CLOB_API_KEY,
                api_secret=CLOB_API_SECRET,
                api_passphrase=CLOB_API_PASSPHRASE,
            ),
        )
        log.info("CLOB client initialized (LIVE TRADING ENABLED)")

    # ------------------------------------------------------------
    def get_balance(self) -> float:
        """Fetch real USDC balance via Polygon RPC."""
        if DRY_RUN or not self.client:
            return YOUR_BANKROLL_USDC

        try:
            import os
            private_key = os.environ.get("PRIVATE_KEY", "")
            if not private_key:
                return YOUR_BANKROLL_USDC

            # Derive address from private key
            from eth_account import Account
            address = Account.from_key(private_key).address

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
            raw = int(result, 16) / 1_000_000
            log.info("Live wallet balance: $%.2f USDC", raw)
            return raw if raw > 0 else YOUR_BANKROLL_USDC

        except Exception as e:
            log.warning("Could not fetch live balance: %s", e)
            return YOUR_BANKROLL_USDC

    # ------------------------------------------------------------
    def place_order(self, token_id: str, side: str, price: float, size_usdc: float):
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
