"""
Executor - places limit orders on Polymarket CLOB
Uses py-clob-client with EIP-712 signatures
"""

import asyncio
import logging
import os
from typing import Optional
from datetime import datetime, timezone
from edge_calculator import TradeOpportunity

logger = logging.getLogger(__name__)

ORDER_CHECK_INTERVAL = 300   # 5 minutes
LIMIT_PRICE_DISCOUNT = 0.98  # bid 2% below fair value to get better fill


class Executor:
    """
    Handles both paper trading (simulation) and live trading via py-clob-client.
    In paper mode, no real orders are placed — everything is simulated and logged.
    """

    def __init__(self, paper: bool = True):
        self.paper = paper
        self._client = None

        if not paper:
            self._init_live_client()

    # ------------------------------------------------------------------
    # Client init (live only)
    # ------------------------------------------------------------------
    def _init_live_client(self):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            from py_clob_client.constants import POLYGON

            def clean_env(val):
                if not val:
                    return val
                s = str(val)
                if '=' in s:
                    s = s.split('=', 1)[-1]
                return s.strip()

            private_key = clean_env(os.getenv("POLYMARKET_PRIVATE_KEY"))
            proxy_address = clean_env(os.getenv("POLYMARKET_PROXY_ADDRESS"))

            logger.warning(f"[LIVE] proxy_address: {proxy_address}")

            if not private_key:
                raise ValueError("POLYMARKET_PRIVATE_KEY not set in .env")
            if not proxy_address:
                raise ValueError("POLYMARKET_PROXY_ADDRESS not set in .env")

            # signature_type=2 = EIP-712 (Gnosis Safe / proxy wallet)
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=private_key,
                chain_id=POLYGON,
                signature_type=2,
                funder=proxy_address,
            )

            # Create API credentials if needed
            try:
                creds = self._client.create_or_derive_api_creds()
                self._client.set_api_creds(creds)
                logger.warning(f"CLOB client initialized OK — api_key={getattr(creds,'api_key','?')[:8]}...")
            except Exception as e:
                logger.warning(f"API creds FAILED (non-fatal): {e}")


        except ImportError:
            logger.error(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )
            raise
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            raise

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    async def execute(self, opp: TradeOpportunity) -> dict:
        """
        Place an order for the opportunity.
        Returns dict with order_id, limit_price, status, message.
        """
        # Determine token and limit price
        # Limit price: bid just above current market price to ensure fill
        # Use market_price (not fair_value) so we don't place insane bids
        if opp.direction == "YES":
            token_id = opp.yes_token_id
            limit_price = round(min(opp.market_price * 1.05, 0.97), 4)
        else:
            token_id = opp.no_token_id
            limit_price = round(min(opp.market_price * 1.05, 0.97), 4)

        # Ensure limit price is valid
        limit_price = max(0.01, min(0.99, limit_price))

        if self.paper:
            return await self._simulate_order(opp, token_id, limit_price)
        else:
            return await self._place_live_order(opp, token_id, limit_price)

    # ------------------------------------------------------------------
    # Paper trading simulation
    # ------------------------------------------------------------------
    async def _simulate_order(self, opp: TradeOpportunity, token_id: Optional[str], limit_price: float) -> dict:
        fake_order_id = f"PAPER-{opp.condition_id[:8]}-{int(datetime.now().timestamp())}"

        logger.info(
            f"[PAPER] Would place: {opp.direction} ${opp.position_size:.2f} "
            f"on '{opp.question[:50]}' @ {limit_price:.3f} "
            f"(fair={opp.fair_value:.3f}, edge={opp.edge:+.1%})"
        )

        return {
            "order_id": fake_order_id,
            "limit_price": limit_price,
            "token_id": token_id,
            "status": "paper_placed",
            "message": f"Paper order simulated: {opp.direction} ${opp.position_size:.2f} @ {limit_price:.3f}",
            "paper": True,
        }

    # ------------------------------------------------------------------
    # Live order placement
    # ------------------------------------------------------------------
    async def _place_live_order(self, opp: TradeOpportunity, token_id: Optional[str], limit_price: float) -> dict:
        if not self._client:
            return {"order_id": "", "status": "error", "message": "Client not initialized"}

        if not token_id:
            return {"order_id": "", "status": "error", "message": "No token_id for this direction"}

        # Fetch real token_id from CLOB client (authenticated)
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: self._client.get_market(opp.condition_id))
            found = False
            for tok in (data.get("tokens") or []):
                if str(tok.get("outcome", "")).upper() == opp.direction:
                    token_id = str(tok["token_id"])
                    logger.warning(f"[LIVE] CLOB token_id: {token_id[:40]}")
                    found = True
                    break
            if not found:
                return {"order_id": "", "status": "error", "message": f"No {opp.direction} token found"}
        except Exception as e:
            logger.warning(f"[LIVE] get_market failed: {e}")
            return {"order_id": "", "status": "error", "message": f"get_market failed: {e}"}

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            # Calculate shares - round UP to ensure cost >= position_size (min $1)
            import math
            shares = math.ceil(opp.position_size / limit_price * 100) / 100

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side="BUY",
            )

            # Run sync client call in thread pool to not block event loop
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._client.create_and_post_order(order_args)
            )

            logger.warning(f"[LIVE] RAW API response type={type(resp).__name__}: {repr(resp)[:500]}")

            # py_clob_client returns nested structure: resp["order"]["id"] OR resp["orderID"]
            order_id = ""
            if isinstance(resp, dict):
                order_id = (
                    resp.get("orderID") or
                    resp.get("order_id") or
                    (resp.get("order") or {}).get("id") or
                    (resp.get("order") or {}).get("orderID") or
                    ""
                )
            elif hasattr(resp, "order_id"):
                order_id = resp.order_id or ""
            elif hasattr(resp, "orderID"):
                order_id = resp.orderID or ""

            if not order_id:
                logger.warning(f"[LIVE] EMPTY order_id — full resp: {repr(resp)[:800]}")

            logger.info(
                f"[LIVE] Order placed: {opp.direction} {shares:.2f} shares "
                f"of '{opp.question[:50]}' @ {limit_price:.3f} | ID={order_id}"
            )

            return {
                "order_id": order_id,
                "limit_price": limit_price,
                "token_id": token_id,
                "status": "pending",
                "message": f"Order placed: {shares:.2f} shares @ {limit_price:.3f}",
                "paper": False,
            }

        except Exception as e:
            import traceback
            logger.warning(f"[LIVE] ORDER EXCEPTION: {type(e).__name__}: {e}")
            logger.warning(f"[LIVE] TRACEBACK: {traceback.format_exc()[:600]}")
            return {
                "order_id": "",
                "status": "error",
                "message": f"{type(e).__name__}: {e}",
                "paper": False,
            }

    # ------------------------------------------------------------------
    # Order status check (called after ORDER_CHECK_INTERVAL)
    # ------------------------------------------------------------------
    async def check_order_status(self, order_id: str) -> dict:
        """Returns {'status': 'filled'|'open'|'cancelled', 'fill_price': float}"""
        if self.paper or order_id.startswith("PAPER-"):
            # Simulate 40% fill rate for paper orders
            import random
            filled = random.random() < 0.40
            return {
                "status": "filled" if filled else "cancelled",
                "fill_price": 0.0,
                "paper": True,
            }

        if not self._client:
            return {"status": "unknown", "fill_price": 0.0}

        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._client.get_order(order_id)
            )
            status = resp.get("status", "unknown").lower()
            fill_price = float(resp.get("avg_price", 0) or 0)
            return {"status": status, "fill_price": fill_price, "paper": False}
        except Exception as e:
            logger.error(f"Order status check failed {order_id}: {e}")
            return {"status": "unknown", "fill_price": 0.0}

    # ------------------------------------------------------------------
    # Cancel order
    # ------------------------------------------------------------------
    async def cancel_order(self, order_id: str) -> bool:
        if self.paper or order_id.startswith("PAPER-"):
            logger.info(f"[PAPER] Cancelled order {order_id}")
            return True

        if not self._client:
            return False

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._client.cancel(order_id=order_id)
            )
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Cancel failed {order_id}: {e}")
            return False
