"""
The bot's settings, read from environment variables or a local `.env` file.

Only WALLET_PRIVATE_KEY is required. The wallet address is worked out from
the key, so the two can never disagree. The key is held in memory only to
sign transactions: it is never printed, logged or written to any file.

Optional settings (see .env.example):
  DATA_DIR         where the bot keeps its memory and logs (default: ./data)
  MAX_TRADE_USD    an upper limit on any single buy, in USD (default: none)
  CANDLE_SOURCE    "swaps" (default): candles from every trade in the pool;
                   "samples": candles from 20-minute price samples
  COINGECKO_API_KEY  only used once, for the history warm-up on a fresh start
  WALLET_ADDRESS   optional; if set, must match the address of the key
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]


class SettingsError(RuntimeError):
    pass


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from a .env file into the environment. Values
    already set in the environment win, so a hosting service's variables
    override the file. Lines starting with # are ignored."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    private_key: str
    wallet_address: str
    data_dir: Path
    max_trade_usd: Optional[float]
    candle_source: str
    coingecko_api_key: Optional[str]

    def __repr__(self) -> str:      # never show the key, even by accident
        return (f"Settings(wallet_address={self.wallet_address!r}, data_dir={str(self.data_dir)!r}, "
                f"max_trade_usd={self.max_trade_usd!r}, candle_source={self.candle_source!r})")

    __str__ = __repr__


def address_from_key(private_key: str) -> str:
    from eth_account import Account
    try:
        return Account.from_key(private_key).address
    except Exception:            # noqa: BLE001 -- don't echo anything about the key
        raise SettingsError("WALLET_PRIVATE_KEY isn't a valid private key (expected 64 hex characters, "
                            "optionally starting with 0x).") from None


def load_settings(env_file: Optional[Path] = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env")
    key = os.environ.get("WALLET_PRIVATE_KEY", "").strip()
    if not key or key.lower().startswith("your"):
        raise SettingsError("WALLET_PRIVATE_KEY is not set. Copy .env.example to .env and put your "
                            "wallet's private key in it (see README.md).")
    address = address_from_key(key)
    given = os.environ.get("WALLET_ADDRESS", "").strip()
    if given and given.lower() != address.lower():
        raise SettingsError(f"WALLET_ADDRESS ({given}) doesn't match the address of WALLET_PRIVATE_KEY "
                            f"({address}). Remove WALLET_ADDRESS or correct it.")

    cap_raw = os.environ.get("MAX_TRADE_USD", "").strip()
    try:
        cap = float(cap_raw) if cap_raw else None
    except ValueError:
        raise SettingsError(f"MAX_TRADE_USD must be a number of dollars, got {cap_raw!r}") from None
    if cap is not None and cap <= 0:
        raise SettingsError("MAX_TRADE_USD must be above 0 (or leave it empty for no limit)")

    source = os.environ.get("CANDLE_SOURCE", "swaps").strip().lower() or "swaps"
    if source not in ("swaps", "samples"):
        raise SettingsError(f"CANDLE_SOURCE must be 'swaps' or 'samples', got {source!r}")

    data_dir = Path(os.environ.get("DATA_DIR", "").strip() or ROOT / "data")
    return Settings(private_key=key, wallet_address=address, data_dir=data_dir, max_trade_usd=cap,
                    candle_source=source, coingecko_api_key=os.environ.get("COINGECKO_API_KEY") or None)
