"""
LiveVault: a real-balance-backed stand-in for the paper bot's Vault class.
Exists for one reason -- so TrendBotEngine.check_entry() and
RangeHarvesterEngine.check_entry() (completely unchanged from the paper
bot) can call `vault.max_affordable_size(requested_usd)` exactly as they
already do, without needing to know or care that "vault" now means "read
the real USDC balance from the chain" instead of "look up a simulated
number."

Sizing math is deliberately SIMPLER than the paper Vault's, not more
complex, because real costs work differently than the simulated cost
model:
  - Gas is paid in ETH -- a completely separate balance from USDC. It
    never reduces how much USDC is available to size a trade with.
  - The pool fee (0.05%) is embedded in the swap's *output* (you get
    slightly less WBTC than a fee-less swap would give), not an extra
    USDC deduction on top of whatever amountIn you specify. Specifying
    amountIn=$20 means exactly $20 leaves the USDC balance -- nothing more.
So the only real constraint on USDC sizing is the floor itself: don't
size a trade that would leave the wallet below MIN_VAULT_BALANCE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config as paper_config
from .live_config import NetworkConfig
from .live_wallet import (
    encode_erc20_balance_of_call, simulate_call, get_eth_balance, LiveWalletError,
)


@dataclass
class LiveBalances:
    usdc: float
    wbtc: float
    eth: float


class LiveVault:
    def __init__(self, wallet_address: str, network: NetworkConfig,
                 min_vault_balance: float = paper_config.MIN_VAULT_BALANCE):
        self.wallet_address = wallet_address
        self.network = network
        self.min_vault_balance = min_vault_balance
        self._cached_usdc_balance: Optional[float] = None

    def refresh_balance(self) -> float:
        """Fetch the real, current USDC balance from chain. Must be called
        at least once per cycle, before any sizing decision -- there is no
        implicit auto-refresh, so a stale read can never silently happen."""
        raw = int.from_bytes(
            simulate_call(list(self.network.rpc_urls), self.wallet_address,
                          self.network.usdc_address, encode_erc20_balance_of_call(self.wallet_address)),
            "big",
        )
        self._cached_usdc_balance = raw / 10 ** self.network.usdc_decimals
        return self._cached_usdc_balance

    @property
    def balance(self) -> float:
        if self._cached_usdc_balance is None:
            raise LiveWalletError("LiveVault.balance read before refresh_balance() was called")
        return self._cached_usdc_balance

    def max_affordable_size(self, requested_usd: float) -> float:
        """Same signature and same job as the paper Vault's method (so TB/RH's
        check_entry works completely unmodified), but the real constraint
        here is simpler: just the floor, since gas is a separate ETH
        balance and the pool fee comes out of the swap's output, not an
        extra USDC deduction. See module docstring."""
        if requested_usd <= 0:
            return 0.0
        headroom = self.balance - self.min_vault_balance
        if headroom <= 0:
            return 0.0
        return max(0.0, min(requested_usd, headroom))

    def get_wbtc_balance_raw(self) -> int:
        if self.network.wbtc_address is None:
            raise LiveWalletError(f"No WBTC address configured for network {self.network.name}")
        return int.from_bytes(
            simulate_call(list(self.network.rpc_urls), self.wallet_address,
                          self.network.wbtc_address, encode_erc20_balance_of_call(self.wallet_address)),
            "big",
        )

    def get_eth_balance_wei(self) -> int:
        return get_eth_balance(list(self.network.rpc_urls), self.wallet_address)

    def snapshot(self) -> LiveBalances:
        """All three balances at once, for logging/reporting."""
        usdc = self.refresh_balance()
        wbtc_raw = self.get_wbtc_balance_raw()
        eth_wei = self.get_eth_balance_wei()
        return LiveBalances(
            usdc=usdc,
            wbtc=wbtc_raw / 10 ** self.network.wbtc_decimals,
            eth=eth_wei / 10 ** 18,
        )
