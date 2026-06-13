"""
Execution layer: wraps py-clob-client for placing orders.
In DRY_RUN mode, no real client is created and orders are only logged.
"""

import logging
from config import PRIVATE_KEY, CLOB_API_URL, POLYGON_CHAIN_ID, DRY_RUN

log = logging.getLogger("polycopy.executor")


class Executor:
    def __init__(self):
        self.client = None
        if not DRY_RUN:
            self._init_client()

    def _init_client(self):
        from py_clob_client.client import ClobClient

        if not PRIVATE_KEY:
            raise RuntimeError(
                "PRIVATE_KEY is empty in config.py but DRY_RUN is False. "
                "Set your wallet private key before going live."
            )

        self.client = ClobClient(
            CLOB_API_URL,
            key=PRIVATE_KEY,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=1,  # 1 = email/Magic wallet (default Polymarket signup)
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        log.info("CLOB client initialized (LIVE TRADING ENABLED)")

    # ------------------------------------------------------------
    def place_order(self, token_id: str, side: str, price: float, size_usdc: float):
        """
        Place a market order.
        side: 'BUY' or 'SELL'
        price: current price (0-1) for the outcome token, used to compute token size
        size_usdc: amount in USDC to spend (for BUY) or notional to sell (for SELL)
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
