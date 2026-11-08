"""
Helpers for the start-up wizard (start.py) and the bot's own safety checks:
saving the private key to .env, reading the wallet's balances and approvals,
remembering which wallet a data folder belongs to, and keeping the computer
awake while the bot runs.

The private key is only ever written to the local .env file (which is in
.gitignore). It is never printed or logged.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .live_config import MAINNET

MIN_USDC_TO_TRADE = 60.0       # the bot always keeps $50 in USDC
MIN_ETH_FOR_GAS = 0.0003       # a few hundred swaps' worth on Arbitrum
UNLIMITED = 2 ** 255
WALLET_FILE = "wallet.txt"


# ---------------------------------------------------------------------------
# The key in .env
# ---------------------------------------------------------------------------

def clean_key(raw: str) -> str:
    """What people paste: strip spaces, quotes and line breaks."""
    return raw.strip().strip("\"'").strip()


def write_env_key(env_path: Path, example_path: Path, key: str) -> None:
    """Put WALLET_PRIVATE_KEY=<key> in .env, keeping every other line. A new
    .env starts from .env.example. On Mac/Linux only the owner can read it."""
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif example_path.exists():
        lines = example_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    out, done = [], False
    for line in lines:
        if line.strip().startswith("WALLET_PRIVATE_KEY=") and not done:
            out.append(f"WALLET_PRIVATE_KEY={key}")
            done = True
        elif line.strip().startswith("WALLET_PRIVATE_KEY="):
            continue
        else:
            out.append(line)
    if not done:
        out.insert(0, f"WALLET_PRIVATE_KEY={key}")
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)


# ---------------------------------------------------------------------------
# Which wallet a data folder belongs to
# ---------------------------------------------------------------------------

def data_wallet(data_dir: Path) -> Optional[str]:
    f = data_dir / WALLET_FILE
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def has_memory(data_dir: Path) -> bool:
    return (data_dir / "state.json").exists()


def wallet_mismatch(data_dir: Path, address: str) -> Optional[str]:
    """The other wallet's address if this data folder holds the memory of a
    different wallet, else None."""
    saved = data_wallet(data_dir)
    if saved and has_memory(data_dir) and saved.lower() != address.lower():
        return saved
    return None


def remember_wallet(data_dir: Path, address: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / WALLET_FILE).write_text(address + "\n", encoding="utf-8")


def set_aside(data_dir: Path, now=time.time) -> Path:
    """Rename the data folder (never delete it) so a new wallet starts fresh."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    target = data_dir.with_name(f"{data_dir.name}-old-{stamp}")
    data_dir.rename(target)
    return target


# ---------------------------------------------------------------------------
# Balances and approvals (read-only)
# ---------------------------------------------------------------------------

def wallet_status(address: str) -> dict:
    from .live_wallet import check_router_approvals, get_eth_balance
    from .onchain import get_live_price
    rpc = list(MAINNET.rpc_urls)
    price = get_live_price(MAINNET.pool_address)["price_usdc_per_wbtc"]
    a = check_router_approvals(rpc, MAINNET.router_address, address, MAINNET.usdc_address, MAINNET.wbtc_address)
    return {
        "price": price,
        "usdc": a["usdc_balance"] / 10 ** MAINNET.usdc_decimals,
        "wbtc": a["wbtc_balance"] / 10 ** MAINNET.wbtc_decimals,
        "eth": get_eth_balance(rpc, address) / 1e18,
        "approved": a["usdc_allowance"] >= UNLIMITED and a["wbtc_allowance"] >= UNLIMITED,
    }


def funding_problems(st: dict) -> list[str]:
    out = []
    if st["usdc"] < MIN_USDC_TO_TRADE:
        out.append(f"USDC: {st['usdc']:,.2f}. Add USDC on Arbitrum One: at least $60, because $50 always stays in "
                   f"reserve.")
    if st["eth"] < MIN_ETH_FOR_GAS:
        out.append(f"ETH: {st['eth']:.6f}. Add a little ETH on Arbitrum One for gas: 0.001 ETH is plenty.")
    return out


# ---------------------------------------------------------------------------
# Keep the computer awake
# ---------------------------------------------------------------------------

def keep_awake() -> Optional[str]:
    """Stop the computer going to sleep while the bot runs (the screen may
    still turn off). Returns a short description, or None where it doesn't
    apply (Linux servers, Railway). Ends automatically when the bot stops."""
    try:
        if sys.platform == "win32":
            import ctypes
            es_continuous, es_system_required = 0x80000000, 0x00000001
            if ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required):
                return "Windows won't go to sleep while the bot is running"
        elif sys.platform == "darwin":
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "your Mac won't go to sleep while the bot is running (closing the lid still sleeps it)"
    except Exception:            # noqa: BLE001 -- a convenience, never a reason to stop
        return None
    return None
