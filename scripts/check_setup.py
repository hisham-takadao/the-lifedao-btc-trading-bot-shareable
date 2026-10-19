"""
Read-only setup check. Sends nothing and costs nothing.

    python scripts/check_setup.py

Shows the wallet address worked out from your private key, your USDC, WBTC
and ETH balances on Arbitrum One, whether the Uniswap router is approved to
trade your USDC and WBTC, and the current BTC price, then says what (if
anything) is still needed before starting the bot.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.live_config import MAINNET
from bot.live_wallet import check_router_approvals, get_eth_balance
from bot.onchain import get_live_price, verify_pool
from bot.settings import SettingsError, load_settings


def main():
    try:
        s = load_settings()
    except SettingsError as e:
        print(f"Setup problem: {e}")
        sys.exit(1)
    rpc = list(MAINNET.rpc_urls)
    verify_pool(MAINNET.pool_address)
    price = get_live_price(MAINNET.pool_address)["price_usdc_per_wbtc"]
    a = check_router_approvals(rpc, MAINNET.router_address, s.wallet_address, MAINNET.usdc_address, MAINNET.wbtc_address)
    eth = get_eth_balance(rpc, s.wallet_address) / 1e18
    usdc, wbtc = a["usdc_balance"] / 1e6, a["wbtc_balance"] / 1e8
    unlimited = lambda x: x >= 2 ** 255
    print(f"Wallet (from your key): {s.wallet_address}")
    print(f"BTC price (pool):       ${price:,.2f}")
    print(f"USDC:                   {usdc:,.2f}")
    print(f"WBTC:                   {wbtc:.8f} (~${wbtc * price:,.2f})")
    print(f"ETH (for gas):          {eth:.6f}")
    print(f"USDC approval:          {'unlimited' if unlimited(a['usdc_allowance']) else a['usdc_allowance'] / 1e6}")
    print(f"WBTC approval:          {'unlimited' if unlimited(a['wbtc_allowance']) else a['wbtc_allowance'] / 1e8}")
    print(f"Data folder:            {s.data_dir}")
    print(f"Max trade size:         {'$%.2f' % s.max_trade_usd if s.max_trade_usd else 'no limit'}")
    todo = []
    if usdc < 60:
        todo.append("Add USDC on Arbitrum One. The bot always keeps $50 in USDC; trade sizes are a share of the rest.")
    if eth < 0.0003:
        todo.append("Add a little ETH on Arbitrum One for gas (0.001 ETH is plenty).")
    if not (unlimited(a["usdc_allowance"]) and unlimited(a["wbtc_allowance"])):
        todo.append("Run `python scripts/approve_tokens.py` once (two small transactions).")
    if wbtc > 0:
        todo.append("Note: this wallet holds WBTC. The bot never sells WBTC it didn't buy; a dedicated wallet "
                    "holding only USDC is best.")
    print()
    print("Ready to start: python run_bot.py" if not [t for t in todo if not t.startswith("Note")] else "Still needed:")
    for t in todo:
        print(f"  - {t}")


if __name__ == "__main__":
    main()
