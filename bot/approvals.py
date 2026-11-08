"""
The one-time router approvals (USDC and WBTC), shared by
scripts/approve_tokens.py, the start-up wizard (start.py) and the bot's
AUTO_APPROVE setting.

The bot buys by swapping USDC for WBTC and sells by swapping WBTC back,
through Uniswap's SwapRouter02. A token contract only lets the router take
tokens from your wallet if you approve it first: one unlimited approval per
token, a fraction of a cent of ETH gas each on Arbitrum. It moves no tokens.
Each approval is simulated first (free) and skipped if already unlimited.
"""

from .live_config import MAINNET
from .live_wallet import (
    encode_erc20_approve, check_token_allowance, check_token_balance,
    simulate_call, build_and_sign_transaction, broadcast_transaction,
    wait_for_receipt, receipt_succeeded, LiveWalletError,
)

MAX_UINT256 = 2 ** 256 - 1
UNLIMITED_THRESHOLD = 2 ** 255


def is_unlimited(allowance_raw: int) -> bool:
    return allowance_raw >= UNLIMITED_THRESHOLD


def ask_yes(question: str) -> bool:
    return input(question).strip() == "YES"


def approve(settings, name: str, token: str, decimals: int, confirm=ask_yes) -> bool:
    """Approve the router for one token if needed. `confirm` decides whether
    to send (default: type YES; the bot's AUTO_APPROVE setting passes one
    that always agrees)."""
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
    if not confirm("Type YES to send it, anything else to skip: "):
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


def approve_both(settings, confirm=ask_yes) -> bool:
    """Both approvals; True when both are unlimited afterwards."""
    ok_usdc = approve(settings, "USDC", MAINNET.usdc_address, MAINNET.usdc_decimals, confirm)
    ok_wbtc = approve(settings, "WBTC", MAINNET.wbtc_address, MAINNET.wbtc_decimals, confirm)
    return ok_usdc and ok_wbtc
