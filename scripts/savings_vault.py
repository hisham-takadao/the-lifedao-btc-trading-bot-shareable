"""
Savings Vault tool (optional feature; WHITELISTED WALLETS ONLY).

It only works if the Savings Vault's developers (TakafulDAO) have registered
your wallet as a member of the vault. `status` tells you whether yours is.

    python scripts/savings_vault.py status
    python scripts/savings_vault.py plan
    python scripts/savings_vault.py deposit 2
    python scripts/savings_vault.py withdraw 1
    python scripts/savings_vault.py withdraw-all

status        read-only: what the wallet has in the vault, its limits, fees, pause state.
plan          read-only: what the bot would deposit right now (the same check the bot runs).
deposit X     deposit X USDC (exact approval, then deposit). For the small first test.
withdraw X    withdraw X USDC-worth (any USDT paid out is swapped to USDC).
withdraw-all  EMERGENCY: take everything out (as far as the vault's 24h limits allow).

Only the Savings Vault 0x6d1bD94Cd572e51Aa018aE6Bf909BA61F8F8d4f9 can be used.
Settings come from your .env file (or environment variables), like the bot:
WALLET_PRIVATE_KEY is read from there and never printed. `status` and `plan`
only read; the others show what they'll do and send nothing unless you type YES.

If the bot is running at the same time, it sees these changes at its next
cycle and books them as outside deposits/withdrawals (its yield figure stays
right). Avoid the first 10 minutes after 00/04/08/12/16/20 UTC, when the bot
itself may be sending transactions.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.live_config import MAINNET
from bot.settings import SettingsError, load_settings as load_bot_settings
from bot.savings_vault import (
    SavingsVault, SavingsSettings, RpcChain, SavingsError, USD, SAVINGS_VAULT_ADDRESS,
)


def build(need_key: bool, mode: str) -> SavingsVault:
    try:
        bot_settings = load_bot_settings()      # .env; rejects any other SAVINGS_VAULT_ADDRESS
    except SettingsError as e:
        print(f"Setup problem: {e}")
        sys.exit(1)
    key = bot_settings.private_key if need_key else None
    address = bot_settings.wallet_address
    base = bot_settings.savings
    settings = SavingsSettings(mode=mode, max_deposit_usd=base.max_deposit_usd, min_deposit_usd=base.min_deposit_usd,
                               cash_buffer_usd=base.cash_buffer_usd, min_eth=base.min_eth,
                               expected_idle_days=base.expected_idle_days)
    v = SavingsVault(RpcChain(MAINNET, address, key), address, settings, MAINNET)
    for w in v.verify():
        print("WARNING:", w)
    return v


def show(v: SavingsVault) -> None:
    st = v.status
    print(f"Wallet:                 {v.wallet}")
    print(f"Savings Vault:          {SAVINGS_VAULT_ADDRESS} (code {st.implementation}, "
          f"{'as reviewed' if st.implementation_ok else 'CHANGED since review'})")
    print(f"Registered member:      {'yes' if st.member else 'NO'}")
    print(f"Vault paused:           {'YES' if st.paused else 'no'}")
    print(f"Your shares:            {st.shares} (worth ${st.value_usd:,.6f}; share price {st.share_price:.6f})")
    print(f"Withdrawable now:       ${v.withdrawable_raw() / USD:,.2f}"
          + (f"  (blocked: {v.withdraw_blocked_reason()})" if v.withdraw_blocked_reason() else ""))
    print(f"24h withdrawal room:    you ${min(st.user_remaining_raw, 10**15) / USD:,.2f}, "
          f"whole vault ${min(st.global_remaining_raw, 10**15) / USD:,.2f} (the bot keeps $500 of that for others)")
    print("Large-withdrawal approval: " + ("off" if not st.approval_threshold_raw
                                           else f"withdrawals of ${st.approval_threshold_raw / USD:,.2f}+ wait for the team"))
    print(f"Wallet:                 {st.wallet_usdc_raw / USD:,.6f} USDC, {st.wallet_usdt_raw / USD:,.6f} USDT, "
          f"{st.eth_wei / 1e18:.6f} ETH")
    apy = v.apy()
    print(f"Vault yield (since launch): {apy:.2%}/yr" if apy is not None else "Vault yield: n/a")
    gas = v.round_trip_gas_usd()
    print(f"Gas for a deposit + later withdrawal: about ${gas:,.3f}" if gas is not None else "Gas: n/a")


def confirm(text: str) -> bool:
    print()
    print(text)
    return input("Type YES to send, anything else to cancel: ").strip() == "YES"


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("status", "plan", "deposit", "withdraw", "withdraw-all"):
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd in ("status", "plan"):
        v = build(need_key=False, mode="dry_run")
        show(v)
        if cmd == "plan":
            amount, why = v.plan_deposit(v.status.wallet_usdc_raw, 0.0)
            print()
            print(f"The bot would deposit ${amount / USD:,.2f} now." if amount else f"The bot would not deposit: {why}")
        return

    v = build(need_key=True, mode="on" if cmd != "withdraw-all" else "withdraw_all")
    show(v)
    actions: list = []
    try:
        if cmd == "deposit":
            amount = int(round(float(sys.argv[2]) * USD))
            blocked = v.deposit_blocked_reason()
            if blocked:
                print(f"\nCan't deposit: {blocked}")
                sys.exit(1)
            if amount <= 0 or amount > v.status.wallet_usdc_raw:
                print("\nThe amount must be positive and at most the wallet's USDC.")
                sys.exit(1)
            if not confirm(f"Deposit {amount / USD:,.6f} USDC: two transactions (approve exactly that amount, then "
                           f"deposit). Gas about $0.05."):
                print("Cancelled. Nothing was sent.")
                return
            shares = v.deposit(amount, actions)
            print("Deposited." if shares else "The deposit did NOT go through (see below).")
        elif cmd == "withdraw":
            amount = int(round(float(sys.argv[2]) * USD))
            if not confirm(f"Withdraw ${amount / USD:,.6f} from the vault (part may arrive as USDT, then swapped to "
                           "USDC). Gas about $0.10."):
                print("Cancelled. Nothing was sent.")
                return
            v.withdraw(amount, actions, reason="by the owner")
            v.convert_usdt(actions)
        else:
            if not confirm(f"EMERGENCY: take everything out of the vault (${v.value_usd:,.2f}) as far as its 24h "
                           "limits allow, and swap any USDT to USDC."):
                print("Cancelled. Nothing was sent.")
                return
            v.withdraw_all(actions)
    except SavingsError as e:
        print(f"\nNot done: {e}")
    for a in actions:
        print(" -", a)
    for r in v.log_rows:
        print(f" [{r['Status']}] {r['Action']} ${r['Amount ($)']}: {r['Notes']} {r['Tx Hash']}")
    print()
    v.refresh()
    show(v)


if __name__ == "__main__":
    main()
