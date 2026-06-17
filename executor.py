"""
Execution layer - supports both Kalshi and Polymarket.
Set EXCHANGE=kalshi or EXCHANGE=polymarket in Railway Variables.
Default is kalshi since it works in the US without restrictions.

Kalshi setup:
- KALSHI_API_KEY_ID   : from kalshi.com Settings -> API
- KALSHI_PRIVATE_KEY  : RSA private key PEM (paste full key)
- KALSHI_USE_DEMO     : "true" for paper trading first

Polymarket setup (requires VPN/non-US):
- PRIVATE_KEY         : wallet private key
- CLOB_API_KEY/SECRET/PASSPHRASE
"""

import os
import logging
import requests
from config import DRY_RUN, YOUR_BANKROLL_USDC

log = logging.getLogger("polycopy.executor")

EXCHANGE = os.environ.get("EXCHANGE", "kalshi").lower()


class Executor:
    def __init__(self):
        self.client = None
        self.exchange = EXCHANGE
        if not DRY_RUN:
            self._init_client()

    def _init_client(self):
        if self.exchange == "kalshi":
            self._init_kalshi()
        else:
            self._init_polymarket()

    def _init_kalshi(self):
        from kalshi_python import Configuration, KalshiClient
        api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        private_key_pem = os.environ.get("KALSHI_PRIVATE_KEY", "")
        use_demo = os.environ.get("KALSHI_USE_DEMO", "true").lower() == "true"

        if not api_key_id or not private_key_pem:
            raise RuntimeError(
                "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY must be set in Railway Variables."
            )

        pem = private_key_pem.strip()
        if not pem.startswith("-----"):
            pem = f"-----BEGIN PRIVATE KEY-----\n{pem}\n-----END PRIVATE KEY-----"
        elif "\\n" in pem:
            pem = pem.replace("\\n", "\n")

        config = Configuration(
            host="https://demo-api.kalshi.co/trade-api/v2" if use_demo
                 else "https://api.elections.kalshi.com/trade-api/v2"
        )
        config.api_key_id = api_key_id
        config.private_key_pem = pem

        self.client = KalshiClient(config)
        log.info("Kalshi executor ready (%s mode)", "DEMO" if use_demo else "LIVE")

    def _init_polymarket(self):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from config import CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE, CLOB_API_URL, POLYGON_CHAIN_ID

        private_key = os.environ.get("PRIVATE_KEY", "")
        if not private_key or not CLOB_API_KEY:
            raise RuntimeError("Polymarket requires PRIVATE_KEY and CLOB_API_KEY.")

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
        log.info("Polymarket executor ready (LIVE)")

    # ── Balance ───────────────────────────────────────────────────
    def get_balance(self) -> float:
        if DRY_RUN or not self.client:
            return YOUR_BANKROLL_USDC
        try:
            if self.exchange == "kalshi":
                from kalshi_data import get_balance as kalshi_get_bal
                bal = kalshi_get_bal()
                log.info("Kalshi account balance: $%.2f", bal)
                return bal if bal > 0 else YOUR_BANKROLL_USDC
            else:
                private_key = os.environ.get("PRIVATE_KEY", "")
                if not private_key:
                    return YOUR_BANKROLL_USDC
                from eth_account import Account
                address = Account.from_key(private_key).address
                USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                data = "0x70a08231" + address[2:].lower().zfill(64)
                r = requests.post("https://polygon-rpc.com",
                                  json={"jsonrpc":"2.0","method":"eth_call",
                                        "params":[{"to":USDC,"data":data},"latest"],"id":1},
                                  timeout=8)
                raw = int(r.json().get("result","0x0"), 16) / 1_000_000
                return raw if raw > 0 else YOUR_BANKROLL_USDC
        except Exception as e:
            log.warning("Balance fetch failed: %s", e)
            return YOUR_BANKROLL_USDC

    # ── Place order ───────────────────────────────────────────────
    def place_order(self, token_id: str, side: str, price: float,
                    size_usdc: float) -> dict:
        """
        Unified order placement.
        token_id: Kalshi ticker OR Polymarket token ID
        side: 'BUY'/'YES' or 'SELL'/'NO'
        price: 0-1 float
        size_usdc: dollar amount to spend
        """
        if DRY_RUN:
            log.info(
                "[DRY RUN] %s | %s %s @ %.4f size=$%.2f",
                self.exchange.upper(), side, token_id[:20], price, size_usdc,
            )
            return {"dry_run": True, "status": "simulated"}

        if self.exchange == "kalshi":
            return self._place_kalshi_order(token_id, side, price, size_usdc)
        else:
            return self._place_polymarket_order(token_id, side, price, size_usdc)

    def _place_kalshi_order(self, ticker: str, side: str, price: float,
                             size_usdc: float) -> dict:
        import uuid
        # Convert price (0-1 float) to cents (1-99 int)
        price_cents = max(1, min(99, int(price * 100)))
        # Count = number of $1 contracts
        count = max(1, int(size_usdc / price)) if price > 0 else 1
        kalshi_side = "yes" if side.upper() in ("BUY", "YES") else "no"

        try:
            from kalshi_python.models import CreateOrderRequest
            resp = self.client.create_order(CreateOrderRequest(
                ticker=ticker,
                action="buy",
                type="limit",
                side=kalshi_side,
                yes_price=price_cents if kalshi_side == "yes" else None,
                no_price=price_cents if kalshi_side == "no" else None,
                count=count,
                client_order_id=str(uuid.uuid4()),
            ))
            result = resp.to_dict() if hasattr(resp, "to_dict") else {"status": "ok"}
            log.info("Kalshi order placed: %s", result)
            return result
        except Exception as e:
            log.error("Kalshi order failed: %s", e)
            return {"error": str(e)}

    def _place_polymarket_order(self, token_id: str, side: str, price: float,
                                 size_usdc: float) -> dict:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        side_const = BUY if side.upper() == "BUY" else SELL
        order_args = MarketOrderArgs(
            token_id=token_id, amount=size_usdc,
            side=side_const, order_type=OrderType.FOK,
        )
        try:
            signed = self.client.create_market_order(order_args)
            resp = self.client.post_order(signed, OrderType.FOK)
            log.info("Polymarket order placed: %s", resp)
            return resp
        except Exception as e:
            log.error("Polymarket order failed: %s", e)
            return {"error": str(e)}
