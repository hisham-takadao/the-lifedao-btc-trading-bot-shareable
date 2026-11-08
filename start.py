"""
The easy way to start The Lifedao BTC Trading Bot.

Double-click "Start Bot (Windows).bat" or "Start Bot (Mac).command" (they run
this file), or run:

    python start.py              # set up if needed, then start the bot
    python start.py --new-key    # use a different wallet

The first time, it:
  1. asks for your bot wallet's private key (hidden as you paste it) and saves
     it in the .env file on this computer only;
  2. checks the wallet on Arbitrum One: USDC, ETH for gas, and the one-time
     router approvals, and offers to send the approvals (you type YES);
  3. starts the bot.
After that it goes straight to the checks and starts the bot.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.easy_setup import (clean_key, funding_problems, set_aside, wallet_mismatch, wallet_status,  # noqa: E402
                            write_env_key)
from bot.settings import SettingsError, address_from_key, load_settings  # noqa: E402

ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
LINE = "-" * 70


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(question: str) -> str:
    try:
        return input(question)
    except EOFError:
        return ""


def key_is_set() -> bool:
    try:
        load_settings(ENV)
        return True
    except SettingsError as e:
        if "is not set" in str(e) or "isn't a valid private key" in str(e):
            return False
        say(f"Setup problem: {e}")
        sys.exit(1)


def enter_key() -> None:
    say(LINE)
    say("STEP 1 of 3: your bot wallet's private key")
    say(LINE)
    say("Use a NEW wallet made only for this bot, holding only what you can afford to lose.")
    say("In MetaMask: account menu > Account details > Show private key.")
    say("The key is saved only in the .env file in this folder. It is never sent anywhere")
    say("except to sign this wallet's own trades. Never share it with anyone.")
    say()
    while True:
        raw = getpass.getpass("Paste the private key and press Enter (nothing shows as you paste, that's normal): ")
        key = clean_key(raw)
        if not key:
            say("Nothing was pasted. Try again (right-click or Ctrl+V pastes in this window).")
            continue
        try:
            address = address_from_key(key)
        except SettingsError as e:
            say(f"That didn't work: {e}")
            continue
        say(f"\nThat key belongs to wallet {address}")
        if ask("Is this the bot's wallet? Type y for yes, n to paste another key: ").strip().lower().startswith("y"):
            write_env_key(ENV, EXAMPLE, key)
            os.environ["WALLET_PRIVATE_KEY"] = key      # this run uses it too
            os.environ.pop("WALLET_ADDRESS", None)
            say("Saved.\n")
            return


def check_data_folder(settings) -> None:
    other = wallet_mismatch(settings.data_dir, settings.wallet_address)
    if not other:
        return
    say(LINE)
    say(f"The data folder ({settings.data_dir}) holds the memory of another wallet:")
    say(f"  {other}")
    say(f"but your key is for {settings.wallet_address}.")
    say("Using it would make the bot think it holds that wallet's positions.")
    if ask("Type MOVE to set that folder aside (it's renamed, not deleted) and start fresh: ").strip() != "MOVE":
        say("Nothing changed. Run again with the other wallet's key, or type MOVE next time.")
        sys.exit(1)
    target = set_aside(settings.data_dir)
    say(f"Moved to {target}.\n")


def check_wallet(settings) -> None:
    say(LINE)
    say("STEP 2 of 3: checking your wallet on Arbitrum One (read-only, free)")
    say(LINE)
    while True:
        try:
            st = wallet_status(settings.wallet_address)
        except Exception as e:  # noqa: BLE001 -- usually the internet connection
            say(f"Couldn't read the blockchain ({e}).")
            if ask("Check your internet connection, then press Enter to try again (or type QUIT): ").strip().upper() == "QUIT":
                sys.exit(1)
            continue
        say(f"Wallet:    {settings.wallet_address}")
        say(f"BTC price: ${st['price']:,.2f}")
        say(f"USDC:      {st['usdc']:,.2f}")
        say(f"WBTC:      {st['wbtc']:.8f} (about ${st['wbtc'] * st['price']:,.2f})")
        say(f"ETH (gas): {st['eth']:.6f}")
        say(f"Approvals: {'in place' if st['approved'] else 'not yet'}")
        if st["wbtc"] > 0:
            say("\nNote: this wallet already holds WBTC. The bot only sells WBTC it bought itself.")
        problems = funding_problems(st)
        if not problems:
            say()
            break
        say("\nBefore the bot can trade:")
        for p in problems:
            say(f"  - {p}")
        say(f"Send them to {settings.wallet_address} on the Arbitrum One network.")
        answer = ask("Press Enter to check again once they've arrived (or type START to start anyway, QUIT to stop): ")
        if answer.strip().upper() == "START":
            break
        if answer.strip().upper() == "QUIT":
            sys.exit(0)
    if st["approved"] or st["eth"] <= 0:
        if not st["approved"]:
            say("The approvals need a little ETH for gas; the bot will start, and you can run this again later.\n")
        return
    say(LINE)
    say("One-time approvals")
    say(LINE)
    say("The bot trades through Uniswap's official router. Your USDC and WBTC each need a one-time")
    say("approval so the router can swap them. It moves no tokens and costs a fraction of a cent of ETH.")
    from bot.approvals import approve_both
    if approve_both(settings):
        say("\nBoth approvals are in place.\n")
    else:
        say("\nThe approvals aren't complete. The bot will start, but its trades will fail until they are.")
        say("Run this again any time to send them.\n")


def main() -> None:
    say("=" * 70)
    say("  The Lifedao BTC Trading Bot")
    say("=" * 70)
    say("It trades WBTC/USDC on Uniswap (Arbitrum One) with REAL money, automatically.")
    say("Read DISCLAIMER.md first. This is not financial advice.\n")

    if "--new-key" in sys.argv or not key_is_set():
        enter_key()
    settings = load_settings(ENV)
    check_data_folder(settings)
    check_wallet(settings)

    say(LINE)
    say("STEP 3 of 3: starting the bot")
    say(LINE)
    say("It checks prices every 20 minutes and decides every 4 hours (00, 04, 08, 12, 16, 20 UTC).")
    say("Keep this window open: closing it (or pressing Ctrl+C) stops the bot. The computer must stay")
    say("on and online. Everything it does is saved in the data folder; see README.md.\n")
    import run_bot
    run_bot.main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Start it again any time; it remembers its positions.")
