"""
Live trading execution for Polymarket using py-clob-client SDK.

This module handles ONLY order submission to Polymarket's CLOB.
Position tracking is handled by CopiedPosition in the main bot.

IMPORTANT: This executes REAL trades with REAL money. Use with caution.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Dict
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Import py-clob-client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds, BalanceAllowanceParams
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    ClobClient = None
    OrderArgs = None
    OrderType = None
    ApiCreds = None
    BalanceAllowanceParams = None


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class LiveOrder:
    """Record of a live order submitted to Polymarket"""
    order_id: str
    token_id: str
    side: str
    price: float
    size: float  # In shares
    cost_usd: float  # Total cost in USD
    status: str  # pending, filled, partial, cancelled, failed
    created_at: str
    filled_at: Optional[str] = None
    filled_size: Optional[float] = None
    filled_avg_price: Optional[float] = None
    error_message: Optional[str] = None
    market_title: Optional[str] = None
    tx_hash: Optional[str] = None


class LiveTrader:
    """
    Pure order execution for Polymarket.
    Does NOT track positions or exposure — that's handled by CopiedPosition.

    Safety features:
    - Configurable max order size
    - Order verification
    - Comprehensive logging
    - Graceful error handling
    """

    CLOB_BASE_URL = "https://clob.polymarket.com"
    POLYGON_CHAIN_ID = 137

    # Safety defaults
    DEFAULT_MAX_ORDER_USD = 10.0  # $10 max per order

    def __init__(
        self,
        private_key: Optional[str] = None,
        funder: Optional[str] = None,
        max_order_usd: float = DEFAULT_MAX_ORDER_USD,
        dry_run: bool = True,  # Default to dry run for safety
    ):
        self.private_key = private_key or os.getenv("PRIVATE_KEY")
        self.funder = funder or os.getenv("FUNDER_ADDRESS")
        self.max_order_usd = max_order_usd
        self.dry_run = dry_run

        # State
        self._client: Optional[ClobClient] = None
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._initialized = False

        # Order tracking (just counts, no position tracking)
        self._orders: Dict[str, LiveOrder] = {}
        self._orders_submitted = 0
        self._orders_filled = 0
        self._orders_failed = 0

    def initialize(self) -> bool:
        """Initialize the CLOB client with authentication."""
        if self._initialized:
            return True

        if not HAS_CLOB_CLIENT:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            return False

        if not self.private_key:
            logger.error("No private key provided. Set PRIVATE_KEY environment variable.")
            return False

        try:
            client_kwargs = {
                "host": self.CLOB_BASE_URL,
                "key": self.private_key,
                "chain_id": self.POLYGON_CHAIN_ID,
            }
            if self.funder:
                client_kwargs["signature_type"] = 2  # POLY_GNOSIS_SAFE
                client_kwargs["funder"] = self.funder
                logger.info(f"Using proxy wallet (funder): {self.funder}")
                logger.info("Using signature_type=2 (POLY_GNOSIS_SAFE)")
            else:
                logger.warning(
                    "No FUNDER_ADDRESS set. Orders will use EOA wallet directly. "
                    "Set FUNDER_ADDRESS in .env to your Polymarket proxy wallet address."
                )

            self._client = ClobClient(**client_kwargs)

            creds = self._client.create_or_derive_api_creds()
            logger.info("Derived fresh API credentials for current config")

            self._client.set_api_creds(creds)

            if not self._verify_api_access():
                logger.error("API access verification failed. Check credentials and configuration.")
                return False

            self._initialized = True

            mode = "DRY RUN" if self.dry_run else "LIVE"
            logger.info(f"LiveTrader initialized successfully ({mode} mode)")
            logger.info(f"  Max order: ${self.max_order_usd:.2f}")

            return True

        except Exception as e:
            logger.error(f"Failed to initialize LiveTrader: {e}")
            return False

    def _verify_api_access(self) -> bool:
        """Verify API connectivity and credentials before trading."""
        if not self._client:
            return False

        try:
            ok_resp = self._client.get_ok()
            logger.info(f"Server health check: OK")
        except Exception as e:
            logger.error(f"Server health check failed: {e}")
            return False

        try:
            api_keys = self._client.get_api_keys()
            logger.info(f"API credentials valid. Active API keys: {len(api_keys) if isinstance(api_keys, list) else 'unknown'}")
        except Exception as e:
            logger.error(f"API key validation failed: {e}")
            logger.error("This usually means credentials don't match the signature_type/funder config.")
            return False

        try:
            signer_addr = self._client.get_address()
            logger.info(f"Signer (EOA) address: {signer_addr}")
            if self.funder:
                logger.info(f"Funder (proxy) address: {self.funder}")
            logger.info(f"Signature type: {self._client.builder.sig_type}")
        except Exception as e:
            logger.warning(f"Could not log address info: {e}")

        return True

    async def submit_buy_order(
        self,
        token_id: str,
        price: float,
        size_usd: float,
        market_title: str = "",
    ) -> Optional[LiveOrder]:
        """Submit a BUY order to Polymarket."""
        return await self._submit_order(
            token_id=token_id,
            side=OrderSide.BUY,
            price=price,
            size_usd=size_usd,
            market_title=market_title,
        )

    async def submit_sell_order(
        self,
        token_id: str,
        price: float,
        shares: float,
        market_title: str = "",
    ) -> Optional[LiveOrder]:
        """Submit a SELL order to Polymarket."""
        size_usd = shares * price
        return await self._submit_order(
            token_id=token_id,
            side=OrderSide.SELL,
            price=price,
            size_usd=size_usd,
            shares_override=shares,
            market_title=market_title,
        )

    async def _submit_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size_usd: float,
        shares_override: Optional[float] = None,
        market_title: str = "",
    ) -> Optional[LiveOrder]:
        """Internal method to submit an order with safety checks."""
        if not self._initialized:
            if not self.initialize():
                logger.error("Cannot submit order: LiveTrader not initialized")
                return None

        # Check 1: Order size limit
        if size_usd > self.max_order_usd:
            logger.warning(
                f"Order size ${size_usd:.2f} exceeds max ${self.max_order_usd:.2f}. "
                f"Reducing to max."
            )
            size_usd = self.max_order_usd

        # Check 2: Total exposure limit (with actual balance verification)
        if side == OrderSide.BUY:
            if self._total_exposure + size_usd > self.max_total_exposure:
                # Before rejecting, check the actual USDC balance on Polymarket
                # Internal tracking can drift if positions were closed externally
                actual_balance = self.get_collateral_balance()
                if actual_balance is not None and actual_balance >= size_usd:
                    logger.info(
                        f"Internal exposure tracking says limit reached "
                        f"(${self._total_exposure:.2f}/${self.max_total_exposure:.2f}), "
                        f"but actual USDC balance is ${actual_balance:.2f}. "
                        f"Resetting exposure tracking."
                    )
                    # Recalibrate: the real available balance is what Polymarket says
                    self._total_exposure = max(0.0, self.max_total_exposure - actual_balance)
                else:
                    available = self.max_total_exposure - self._total_exposure
                    if available <= 0:
                        logger.warning(
                            f"Max exposure ${self.max_total_exposure:.2f} reached. "
                            f"Cannot submit order."
                            + (f" (Actual USDC balance: ${actual_balance:.2f})"
                               if actual_balance is not None else "")
                        )
                        return None
                    logger.warning(
                        f"Order would exceed max exposure. Reducing to ${available:.2f}"
                    )
                    size_usd = available

        # Check 3: Valid price
        if price <= 0 or price >= 1:
            logger.error(f"Invalid price {price}. Must be between 0 and 1.")
            return None

        # Calculate shares
        if shares_override:
            shares = shares_override
        else:
            shares = size_usd / price

        # Create order record
        order_id = f"live_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        order = LiveOrder(
            order_id=order_id,
            token_id=token_id,
            side=side.value,
            price=price,
            size=shares,
            cost_usd=size_usd,
            status="pending",
            created_at=datetime.utcnow().isoformat(),
            market_title=market_title,
        )

        # === DRY RUN MODE ===
        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would submit {side.value} order:\n"
                f"  Token: {token_id[:20]}...\n"
                f"  Price: {price:.4f}\n"
                f"  Shares: {shares:.4f}\n"
                f"  Cost: ${size_usd:.2f}\n"
                f"  Market: {market_title[:50]}"
            )
            order.status = "dry_run"
            self._orders[order_id] = order
            return order

        # === LIVE EXECUTION ===
        try:
            logger.info(
                f"Submitting LIVE {side.value} order:\n"
                f"  Token: {token_id[:20]}...\n"
                f"  Price: {price:.4f}\n"
                f"  Shares: {shares:.4f}\n"
                f"  Cost: ${size_usd:.2f}"
            )

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._execute_order_sync,
                token_id,
                side,
                price,
                shares,
            )

            if result:
                order.status = "submitted"
                order.order_id = result.get("orderID", order_id)
                self._orders_submitted += 1

                logger.info(f"Order submitted successfully: {order.order_id}")

                # Assume market orders fill immediately
                order.status = "filled"
                order.filled_at = datetime.utcnow().isoformat()
                order.filled_size = shares
                order.filled_avg_price = price
                self._orders_filled += 1

            else:
                order.status = "failed"
                order.error_message = "Order submission returned no result"
                self._orders_failed += 1
                logger.error(f"Order failed: {order.error_message}")

        except Exception as e:
            order.status = "failed"
            order.error_message = str(e)
            self._orders_failed += 1
            logger.error(f"Order execution error: {e}")

        self._orders[order.order_id] = order
        return order

    def _execute_order_sync(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        shares: float,
    ) -> Optional[dict]:
        """Synchronous order execution (called from executor)."""
        if not self._client:
            return None

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side=side.value,
            )

            neg_risk = self._client.get_neg_risk(token_id)
            from py_clob_client.config import get_contract_config
            contract_config = get_contract_config(self._client.builder.signer.get_chain_id(), neg_risk)
            logger.info(
                f"Order signing context:\n"
                f"  neg_risk: {neg_risk}\n"
                f"  exchange (verifyingContract): {contract_config.exchange}\n"
                f"  chain_id: {self._client.builder.signer.get_chain_id()}"
            )

            signed_order = self._client.create_order(order_args)
            order_dict = signed_order.order.dict() if hasattr(signed_order, 'order') else {}
            logger.info(
                f"Order created (pre-post debug):\n"
                f"  maker: {order_dict.get('maker', 'N/A')}\n"
                f"  signer: {order_dict.get('signer', 'N/A')}\n"
                f"  signatureType: {order_dict.get('signatureType', 'N/A')}\n"
                f"  makerAmount: {order_dict.get('makerAmount', 'N/A')}\n"
                f"  takerAmount: {order_dict.get('takerAmount', 'N/A')}\n"
                f"  tokenId: {str(order_dict.get('tokenId', ''))[:20]}...\n"
                f"  signature: {signed_order.signature[:40] if hasattr(signed_order, 'signature') else 'N/A'}..."
            )

            result = self._client.post_order(signed_order)
            return result

        except Exception as e:
            logger.error(f"Sync order execution failed: {e}")
            raise

    def get_stats(self) -> dict:
        """Get order execution statistics (no position tracking)."""
        return {
            "mode": "DRY RUN" if self.dry_run else "LIVE",
            "initialized": self._initialized,
            "orders_submitted": self._orders_submitted,
            "orders_filled": self._orders_filled,
            "orders_failed": self._orders_failed,
        }

    def get_collateral_balance(self) -> Optional[float]:
        """
        Fetch the actual USDC collateral balance from Polymarket.

        Returns:
            USDC balance in dollars, or None if unable to fetch
        """
        if not self._initialized or not self._client:
            return None

        try:
            # asset_type=1 is for collateral (USDC)
            balance_info = self._client.get_balance_allowance(asset_type=1)
            if balance_info:
                return float(balance_info.get("balance", 0))
        except Exception as e:
            logger.warning(f"Could not fetch USDC balance: {e}")

        return None

    def shutdown(self):
        """Clean shutdown."""
        if self._executor:
            self._executor.shutdown(wait=False)
        logger.info("LiveTrader shutdown complete")
