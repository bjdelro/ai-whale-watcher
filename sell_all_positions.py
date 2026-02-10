#!/usr/bin/env python3
"""
Sell All Positions — Emergency/reset script for Polymarket whale copy trader.

Reads the saved state file, shows all open positions, and sells them
via the CLOB API. Also resets the state file to start fresh.

Usage:
    python sell_all_positions.py                 # Show positions (dry run)
    python sell_all_positions.py --execute       # Actually sell everything
    python sell_all_positions.py --reset-only    # Just wipe state, don't sell on-chain
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# ================================================================
# Cloudflare bypass patch (same as main bot)
# ================================================================
from curl_cffi import requests as _curl_requests
from py_clob_client.http_helpers import helpers as _clob_helpers
from py_clob_client import client as _clob_client_module
from py_clob_client.exceptions import PolyApiException as _PolyApiException
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

_original_post = _clob_helpers.post


def _patched_post(endpoint, headers=None, data=None):
    if "/order" in endpoint:
        headers = _clob_helpers.overloadHeaders("POST", headers)
        if isinstance(data, str):
            resp = _curl_requests.post(
                endpoint, headers=headers,
                data=data.encode("utf-8"), impersonate="chrome",
            )
        else:
            resp = _curl_requests.post(
                endpoint, headers=headers,
                json=data, impersonate="chrome",
            )
        if resp.status_code != 200:
            raise _PolyApiException(resp)
        try:
            return resp.json()
        except ValueError:
            return resp.text
    return _original_post(endpoint, headers, data)


_clob_helpers.post = _patched_post
_clob_client_module.post = _patched_post

# ================================================================

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def find_state_files():
    """Find all state files in market_logs/."""
    state_dir = os.getenv("STATE_DIR", "market_logs")
    files = []
    if os.path.isdir(state_dir):
        for f in os.listdir(state_dir):
            if f.startswith("positions_state_") and f.endswith(".json"):
                files.append(os.path.join(state_dir, f))
    return files


def load_open_positions(state_file: str) -> list:
    """Load open positions from a state file."""
    with open(state_file, "r") as f:
        state = json.load(f)

    positions = []
    for pid, pos in state.get("positions", {}).items():
        if pos.get("status") == "open":
            positions.append({
                "position_id": pid,
                "market_title": pos.get("market_title", "Unknown"),
                "outcome": pos.get("outcome", "?"),
                "token_id": pos.get("token_id", ""),
                "entry_price": pos.get("entry_price", 0),
                "shares": pos.get("live_shares") or pos.get("shares", 0),
                "cost_usd": pos.get("live_cost_usd") or pos.get("copy_amount_usd", 0),
                "whale_name": pos.get("whale_name", "Unknown"),
                "entry_time": pos.get("entry_time", ""),
            })
    return positions


def init_clob_client():
    """Initialize CLOB client for selling."""
    private_key = os.getenv("PRIVATE_KEY")
    funder = os.getenv("FUNDER_ADDRESS")

    if not private_key:
        logger.error("No PRIVATE_KEY in environment. Cannot sell.")
        return None

    kwargs = {
        "host": "https://clob.polymarket.com",
        "key": private_key,
        "chain_id": 137,
    }
    if funder:
        kwargs["signature_type"] = 2
        kwargs["funder"] = funder

    client = ClobClient(**kwargs)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    # Verify connectivity
    try:
        client.get_ok()
        logger.info("CLOB API connection verified")
    except Exception as e:
        logger.error(f"CLOB API connection failed: {e}")
        return None

    return client


def get_on_chain_balance(client, token_id: str) -> float:
    """Check actual on-chain balance for a token."""
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
            signature_type=2,
        )
        info = client.get_balance_allowance(params)
        balance = float(info.get("balance", 0)) if info else 0
        # Polymarket returns raw units (6 decimals) for large values
        return balance / 1e6 if balance > 100 else balance
    except Exception as e:
        logger.warning(f"Could not check balance for {token_id[:20]}...: {e}")
        return 0


def sell_position(client, token_id: str, price: float, shares: float) -> bool:
    """Submit a SELL order."""
    if shares < 5.0:
        logger.warning(f"  Shares {shares:.2f} < 5 minimum. Skipping dust position.")
        return False

    # Sell slightly below current price to ensure fill
    sell_price = max(0.01, round(price - 0.01, 2))

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=sell_price,
            size=shares,
            side="SELL",
        )
        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order)
        order_id = result.get("orderID", "unknown") if result else "failed"
        logger.info(f"  SELL order submitted: {order_id}")
        return True
    except Exception as e:
        logger.error(f"  SELL failed: {e}")
        return False


def fetch_current_price(token_id: str) -> float:
    """Fetch current mid price from Polymarket REST API."""
    import urllib.request
    try:
        url = f"https://clob.polymarket.com/price?token_id={token_id}&side=sell"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return float(data.get("price", 0))
    except Exception:
        return 0


def reset_state_file(state_file: str):
    """Reset a state file — close all positions and zero out counters."""
    backup = state_file + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    with open(state_file, "r") as f:
        state = json.load(f)

    # Backup first
    with open(backup, "w") as f:
        json.dump(state, f, indent=2)
    logger.info(f"Backed up state to: {backup}")

    # Reset: mark all positions closed, zero counters
    now = datetime.now(timezone.utc).isoformat()
    for pid, pos in state.get("positions", {}).items():
        if pos.get("status") == "open":
            pos["status"] = "closed"
            pos["exit_time"] = now
            pos["exit_reason"] = "manual_sell_all"
            pos["exit_price"] = pos.get("entry_price", 0)  # Mark as break-even
            pos["pnl"] = 0

    state["total_exposure"] = 0.0
    state["saved_at"] = now
    # Reset per-whale tracking so we start fresh
    state["whale_copy_pnl"] = {}
    state["pruned_whales"] = []

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

    logger.info(f"State file reset: {state_file}")


async def main():
    parser = argparse.ArgumentParser(description="Sell all open Polymarket positions")
    parser.add_argument("--execute", action="store_true",
                        help="Actually submit sell orders (default is dry run)")
    parser.add_argument("--reset-only", action="store_true",
                        help="Only reset state file, don't sell on-chain")
    parser.add_argument("--state-file", type=str, default=None,
                        help="Path to specific state file (auto-detects if not set)")
    args = parser.parse_args()

    # Find state files
    if args.state_file:
        state_files = [args.state_file]
    else:
        state_files = find_state_files()

    if not state_files:
        logger.error("No state files found in market_logs/. Nothing to do.")
        logger.info("If your state file is elsewhere, use --state-file <path>")
        return

    # Process each state file
    for state_file in state_files:
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {state_file}")
        logger.info(f"{'='*60}")

        positions = load_open_positions(state_file)
        if not positions:
            logger.info("No open positions found.")
            continue

        # Show all open positions
        total_cost = 0
        logger.info(f"\n{len(positions)} OPEN POSITIONS:")
        logger.info(f"{'-'*60}")
        for pos in positions:
            total_cost += pos["cost_usd"]
            logger.info(
                f"  {pos['outcome']:3s} @ {pos['entry_price']:.1%} "
                f"| ${pos['cost_usd']:.2f} ({pos['shares']:.1f} shares) "
                f"| {pos['whale_name'][:15]:15s} "
                f"| {pos['market_title'][:40]}..."
            )
        logger.info(f"{'-'*60}")
        logger.info(f"Total cost basis: ${total_cost:.2f}")

        if args.reset_only:
            reset_state_file(state_file)
            logger.info("State reset. On-chain positions NOT sold.")
            continue

        if not args.execute:
            logger.info("\nDRY RUN — no orders submitted. Use --execute to sell.")
            logger.info("Use --reset-only to just wipe state without selling on-chain.")
            continue

        # Initialize CLOB client
        client = init_clob_client()
        if not client:
            logger.error("Cannot sell without CLOB client. Skipping on-chain sells.")
            logger.info("You can still use --reset-only to wipe state.")
            continue

        # Sell each position
        logger.info(f"\nSELLING {len(positions)} positions...")
        sold = 0
        skipped = 0
        failed = 0

        for pos in positions:
            token_id = pos["token_id"]
            if not token_id:
                logger.warning(f"  No token_id for {pos['market_title'][:40]}. Skipping.")
                skipped += 1
                continue

            # Check actual on-chain balance
            balance = get_on_chain_balance(client, token_id)
            if balance <= 0.001:
                logger.info(
                    f"  Zero balance for {pos['outcome']} "
                    f"| {pos['market_title'][:40]}... — already settled/sold"
                )
                skipped += 1
                continue

            # Get current price
            current_price = fetch_current_price(token_id)
            if current_price <= 0:
                current_price = pos["entry_price"]
                logger.warning(f"  Could not fetch price, using entry price {current_price:.1%}")

            pnl = (current_price - pos["entry_price"]) * balance
            pnl_emoji = "+" if pnl >= 0 else ""

            logger.info(
                f"  Selling: {pos['outcome']} "
                f"@ {pos['entry_price']:.1%}→{current_price:.1%} "
                f"| {balance:.1f} shares "
                f"| P&L: {pnl_emoji}${pnl:.2f} "
                f"| {pos['market_title'][:35]}..."
            )

            ok = sell_position(client, token_id, current_price, balance)
            if ok:
                sold += 1
            else:
                failed += 1

            await asyncio.sleep(0.5)  # Rate limit

        logger.info(f"\nResults: {sold} sold, {skipped} skipped, {failed} failed")

        # Reset state file
        reset_state_file(state_file)

    logger.info("\nDone. Restart the bot to begin fresh with the new improvements.")


if __name__ == "__main__":
    asyncio.run(main())
