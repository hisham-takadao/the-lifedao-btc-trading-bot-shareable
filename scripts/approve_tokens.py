"""
One-time setup: let the Uniswap router trade this wallet's USDC and WBTC.

    python scripts/approve_tokens.py

The bot buys by swapping USDC for WBTC and sells by swapping WBTC back, through
Uniswap's SwapRouter02. A token contract only lets the router take tokens from
your wallet if you approve it first. This script sends up to two small
transactions, an unlimited approval for USDC and one for WBTC, costing a
fraction of a cent of ETH gas each on Arbitrum. It does NOT move any tokens.

For each token it shows the current approval, skips it if it's already
unlimited, runs a free simulation first, and asks you to type YES before
sending anything. Safe to run again.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.approvals import approve_both
from bot.settings import SettingsError, load_settings


def main():
    try:
        settings = load_settings()
    except SettingsError as e:
        print(f"Setup problem: {e}")
        sys.exit(1)
    print(f"Wallet (from your key): {settings.wallet_address}")
    print("Network: Arbitrum One")
    ok = approve_both(settings)
    print()
    print("Both approvals are in place. You can start the bot." if ok
          else "Not all approvals are in place yet; run this script again when ready.")


if __name__ == "__main__":
    main()
