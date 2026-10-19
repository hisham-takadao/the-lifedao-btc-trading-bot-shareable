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

from bot.live_config import MAINNET
from bot.live_wallet import (
    encode_erc20_approve, check_token_allowance, check_token_balance,
    simulate_call, build_and_sign_transaction, broadcast_transaction,
    wait_for_receipt, receipt_succeeded, LiveWalletError,
)
from bot.settings import SettingsError, load_settings

MAX_UINT256 = 2 ** 256 - 1
UNLIMITED_THRESHOLD = 2 ** 255


def is_unlimited(allowance_raw: int) -> bool:
    return allowance_raw >= UNLIMITED_THRESHOLD


def approve(settings, name: str, token: str, decimals: int) -> bool:
    rpc = list(MAINNET.rpc_urls)
    me = settings.wallet_address
    scale = 10 ** decimals
    balance = check_token_balance(rpc, token, me)
    allowance = check_token_allowance(rpc, token, me, MAINNET.router_address)
    print(f"\n--- {name} ({token}) ---")
    print(f"Balance:   {balance / scale:,.{decimals}f}")
    if is_unlimited(allowance):
        print("Approval:  unlimited already. Nothing to do.")
        return True
    print(f"Approval:  {allowance / scale:,.{decimals}f}")
    data = encode_erc20_approve(spender=MAINNET.router_address, amount=MAX_UINT256)
    try:
        result = simulate_call(rpc, me, token, data)
    except LiveWalletError as e:
        print(f"Simulation FAILED, nothing was sent: {e}")
        return False
    if int.from_bytes(result, "big") != 1:
        print(f"Simulation returned an unexpected result ({result.hex()}), nothing was sent.")
        return False
    print("Simulation OK: the approval would succeed.")
    print(f"This sends ONE REAL transaction approving Uniswap's SwapRouter02 ({MAINNET.router_address})")
    print(f"to trade this wallet's {name} with no limit. It costs a little ETH gas and moves no {name}.")
    if input("Type YES to send it, anything else to skip: ").strip() != "YES":
        print("Skipped. Nothing was sent.")
        return False
    try:
        signed = build_and_sign_transaction(rpc_urls=rpc, chain_id=MAINNET.chain_id,
                                            private_key_hex=settings.private_key, to_address=token, data=data)
        tx_hash = broadcast_transaction(rpc, signed.raw_hex)
        print(f"Sent: {tx_hash}\nWaiting for confirmation...")
        receipt = wait_for_receipt(rpc, tx_hash)
        if not receipt_succeeded(receipt):
            print("FAILED: the transaction was mined but reverted.")
            return False
        print(f"SUCCESS: confirmed in block {int(receipt['blockNumber'], 16)}. View it: https://arbiscan.io/tx/{tx_hash}")
    except LiveWalletError as e:
        print(f"FAILED: {e}")
        return False
    return is_unlimited(check_token_allowance(rpc, token, me, MAINNET.router_address))


def main():
    try:
        settings = load_settings()
    except SettingsError as e:
        print(f"Setup problem: {e}")
        sys.exit(1)
    print(f"Wallet (from your key): {settings.wallet_address}")
    print("Network: Arbitrum One")
    ok_usdc = approve(settings, "USDC", MAINNET.usdc_address, MAINNET.usdc_decimals)
    ok_wbtc = approve(settings, "WBTC", MAINNET.wbtc_address, MAINNET.wbtc_decimals)
    print()
    print("Both approvals are in place. You can start the bot: python run_bot.py" if ok_usdc and ok_wbtc
          else "Not all approvals are in place yet; run this script again when ready.")


if __name__ == "__main__":
    main()
