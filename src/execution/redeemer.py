"""
Auto-redemption of winning Polymarket positions via the Builder Relayer API.

After a market resolves, winning conditional tokens must be redeemed to convert
them back to USDC. This module handles that process using Polymarket's gasless
relayer so no MATIC gas fees are needed.
"""

import logging
import os
from typing import Optional

from eth_utils import keccak
from eth_abi import encode

logger = logging.getLogger(__name__)

# Polymarket contract addresses (Polygon mainnet)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_BYTES32 = "0x" + "00" * 32

# Relayer URL
RELAYER_URL = "https://relayer-v2.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


def _function_selector(signature: str) -> bytes:
    """First 4 bytes of Keccak-256 of the function signature."""
    return keccak(text=signature)[:4]


def encode_redeem_positions(
    collateral_token: str,
    parent_collection_id: bytes,
    condition_id: str,
    index_sets: list[int],
) -> str:
    """Encode the redeemPositions call for the CTF contract."""
    selector = _function_selector(
        "redeemPositions(address,bytes32,bytes32,uint256[])"
    )
    # Convert condition_id to bytes32
    if condition_id.startswith("0x"):
        condition_bytes = bytes.fromhex(condition_id[2:])
    else:
        condition_bytes = bytes.fromhex(condition_id)

    encoded_args = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [collateral_token, parent_collection_id, condition_bytes, index_sets],
    )
    return "0x" + (selector + encoded_args).hex()


class PositionRedeemer:
    """
    Redeems winning conditional tokens on Polymarket via the gasless relayer.
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
    ):
        self._private_key = private_key or os.getenv("PRIVATE_KEY")
        self._api_key = api_key or os.getenv("POLYMARKET_API_KEY")
        self._api_secret = api_secret or os.getenv("POLYMARKET_API_SECRET")
        self._api_passphrase = api_passphrase or os.getenv("POLYMARKET_PASSPHRASE")
        self._client = None
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize the relayer client."""
        try:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

            if not all([self._private_key, self._api_key, self._api_secret, self._api_passphrase]):
                logger.error("Missing credentials for PositionRedeemer")
                return False

            builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=self._api_key,
                    secret=self._api_secret,
                    passphrase=self._api_passphrase,
                )
            )

            self._client = RelayClient(
                RELAYER_URL,
                CHAIN_ID,
                self._private_key,
                builder_config,
            )

            self._initialized = True
            logger.info("PositionRedeemer initialized successfully")
            return True

        except ImportError as e:
            logger.error(f"Missing dependency for PositionRedeemer: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize PositionRedeemer: {e}")
            return False

    def redeem(self, condition_id: str, is_neg_risk: bool = False) -> bool:
        """
        Redeem winning positions for a resolved market.

        Args:
            condition_id: The market's condition ID
            is_neg_risk: Whether this is a neg-risk (multi-outcome) market

        Returns:
            True if redemption was submitted successfully
        """
        if not self._initialized:
            if not self.initialize():
                return False

        try:
            from py_builder_relayer_client.models import SafeTransaction, OperationType

            # For standard binary markets, use CTF directly
            # For neg-risk markets, use the NegRiskAdapter
            target_contract = NEG_RISK_ADAPTER_ADDRESS if is_neg_risk else CTF_ADDRESS

            # Encode the redeemPositions call
            # parentCollectionId is always zero for Polymarket
            # indexSets [1, 2] covers both YES (index 0 → bit 1) and NO (index 1 → bit 2)
            calldata = encode_redeem_positions(
                collateral_token=USDC_ADDRESS,
                parent_collection_id=bytes(32),  # zero bytes32
                condition_id=condition_id,
                index_sets=[1, 2],
            )

            txn = SafeTransaction(
                to=target_contract,
                operation=OperationType.Call,
                data=calldata,
                value="0",
            )

            logger.info(
                f"Submitting redemption for condition {condition_id[:16]}... "
                f"({'neg-risk' if is_neg_risk else 'standard'} market)"
            )

            resp = self._client.execute([txn], "Redeem winning position")

            if resp:
                logger.info(f"Redemption submitted: {resp}")
                # Wait for confirmation
                try:
                    result = resp.wait()
                    logger.info(f"Redemption confirmed: {result}")
                    return True
                except Exception as e:
                    logger.warning(f"Redemption submitted but confirmation pending: {e}")
                    return True  # Still consider it submitted
            else:
                logger.error("Redemption submission returned no response")
                return False

        except Exception as e:
            logger.error(f"Failed to redeem position: {e}")
            return False
