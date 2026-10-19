"""
Shared capital pool, positions, and the transaction cost model.
Tech doc Sections 5 (Shared Capital Pool) and 7 (Vault + Cost Model).

Everything here is *mechanical* -- fills, fees, slippage, vault accrual,
equity accounting. The actual trading rules (when to enter, when to exit)
live in trend_bot.py and range_harvester.py; those modules call into this
one to actually execute what they decide.

Two resolved ambiguities worth flagging up front:

1. Section 7.4 spells out the outlay/proceeds formula using only pool_fee
   and gas_swap ("USDC outlay on entry = ... + pool_fee + gas_swap"), but
   Section 7.2's cost table separately lists gas_approval (entry) and
   gas_vault_deposit/withdrawal (entry/exit) as costs "applied to every
   fill." Rather than silently drop two of the four stated cost
   components, this implementation charges all four -- pool_fee +
   gas_approval + gas_swap + gas_vault_withdrawal on entry, pool_fee +
   gas_swap + gas_vault_deposit on exit. If the narrower Section 7.4
   reading was intended, `entry_cost_usd` / `exit_cost_usd` below are the
   only two places to change.

2. Section 8.4's TB sizing formula caps size_usd at
   `vault_balance - min_vault_after_fill`, with no allowance for the fees
   layered on top at fill time. Taken literally, a fill at exactly that
   cap would leave the vault below the $50 floor once fees are paid --
   which would break the Section 12.5 invariant that vault balance must
   never dip below $50 "at any fill candle." Since that invariant uses
   explicit MUST-hold language, this implementation reserves a fee buffer
   inside the vault-capacity term of the sizing calculation (both
   engines) so the floor holds even after fees. See `max_affordable_size()`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import config


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def _next_position_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class TBPosition:
    entry_signal_candle_index: int
    entry_fill_candle_index: int
    entry_fill_price: float
    wbtc_qty: float
    size_usd: float
    adx_at_entry: float
    rsi_at_entry: float
    adx_tier: int                  # 2 or 3 -> which sizing tier fired
    entry_slippage_pct: float
    entry_fees_usd: float
    entry_timestamp: int = 0
    peak_close: float = 0.0
    trailing_armed: bool = False
    pending_exit: bool = False
    position_id: str = field(default_factory=_next_position_id)

    def __post_init__(self):
        if self.peak_close == 0.0:
            self.peak_close = self.entry_fill_price


@dataclass
class RHPosition:
    entry_signal_candle_index: int
    entry_fill_candle_index: int
    entry_fill_price: float          # VWAP == T1 fill price (single tranche only)
    wbtc_qty: float
    size_usd: float
    entry_regime: int
    rsi_violator: float
    adx_at_signal: float
    atr_at_signal: float
    entry_slippage_pct: float
    entry_fees_usd: float
    entry_timestamp: int = 0
    peak_price: float = 0.0
    handoff: bool = False
    mid_trail: bool = False
    trailing_armed: bool = False
    pending_exit: bool = False
    position_id: str = field(default_factory=_next_position_id)

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_fill_price


@dataclass
class PendingFill:
    """A fill scheduled on the signal candle, executed at the *next*
    candle's open (Section 6, Phase A note: never on the same candle as
    the signal)."""
    engine: str            # "TB" | "RH"
    side: str               # "BUY" | "SELL"
    signal_candle_index: int
    size_usd: float = 0.0                 # BUY only
    adx_tier: int = 0                      # BUY (TB) only, for logging
    position: Optional[object] = None      # SELL only -- the position being closed
    exit_reason: str = ""                  # SELL only
    meta: dict = field(default_factory=dict)


@dataclass
class FillResult:
    """What actually happened when a PendingFill executed, for logging."""
    engine: str
    side: str
    fill_price: float
    slippage_pct: float
    wbtc_qty: float
    usd_value: float           # size_usd (BUY) or raw proceeds before fees (SELL)
    fees_usd: float
    net_cash_flow: float        # negative = cash left the vault, positive = cash returned
    exit_reason: str = ""
    position: Optional[object] = None


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

class CostModel:
    def __init__(self, seed: int = config.RNG_BASE_SEED):
        # Section 7.3: each engine gets its own independent RNG stream, both
        # initialised from the same seed formula.
        self.rng = {
            "TB": np.random.default_rng(seed),
            "RH": np.random.default_rng(seed),
        }

    def draw_slippage_pct(self, engine: str) -> float:
        return float(self.rng[engine].uniform(config.SLIPPAGE_MIN_PCT, config.SLIPPAGE_MAX_PCT))

    @staticmethod
    def buy_fill_price(open_price: float, slippage_pct: float) -> float:
        return open_price * (1 + slippage_pct / 100)

    @staticmethod
    def sell_fill_price(open_price: float, slippage_pct: float) -> float:
        return open_price * (1 - slippage_pct / 100)

    @staticmethod
    def entry_cost_usd(size_usd: float) -> float:
        pool_fee = size_usd * config.POOL_FEE_PCT / 100
        gas = config.GAS_APPROVAL_USD + config.GAS_SWAP_USD + config.GAS_VAULT_WITHDRAWAL_USD
        return pool_fee + gas

    @staticmethod
    def exit_cost_usd(raw_proceeds_usd: float) -> float:
        pool_fee = raw_proceeds_usd * config.POOL_FEE_PCT / 100
        gas = config.GAS_SWAP_USD + config.GAS_VAULT_DEPOSIT_USD
        return pool_fee + gas

    @staticmethod
    def max_entry_fee_estimate(size_usd: float) -> float:
        """Upper bound used when reserving room in the vault-capacity sizing
        term -- see module docstring, point 2."""
        return size_usd * config.POOL_FEE_PCT / 100 + (
            config.GAS_APPROVAL_USD + config.GAS_SWAP_USD + config.GAS_VAULT_WITHDRAWAL_USD
        )


# ---------------------------------------------------------------------------
# Vault / shared pool
# ---------------------------------------------------------------------------

class Vault:
    def __init__(self, starting_capital: float, cost_model: Optional[CostModel] = None):
        self.balance = starting_capital
        self.costs = cost_model or CostModel()
        self.cum_vault_yield = 0.0

    def accrue_yield(self) -> float:
        """Section 7.1 -- unconditional per-candle compounding on whatever
        is currently sitting idle."""
        if self.balance > 0:
            new_balance = self.balance * config.VAULT_PER_CANDLE_FACTOR
            self.cum_vault_yield += new_balance - self.balance
            self.balance = new_balance
        return self.balance

    def max_affordable_size(self, requested_usd: float) -> float:
        """Caps a requested tranche size so that, after this fill's fees are
        deducted, the vault still won't dip below the $50 floor. See module
        docstring point 2 for why this is a hair stricter than the literal
        Section 8.4 formula."""
        if requested_usd <= 0:
            return 0.0
        # Fees scale (very slightly) with size_usd via the pool fee, so solve
        # size + size*fee_pct/100 + flat_gas <= balance - min_vault directly
        # rather than iterating.
        flat_gas = config.GAS_APPROVAL_USD + config.GAS_SWAP_USD + config.GAS_VAULT_WITHDRAWAL_USD
        headroom = self.balance - config.MIN_VAULT_BALANCE - flat_gas
        if headroom <= 0:
            return 0.0
        max_size = headroom / (1 + config.POOL_FEE_PCT / 100)
        return max(0.0, min(requested_usd, max_size))

    def execute_buy(self, engine: str, size_usd: float, open_price: float,
                     signal_candle_index: int) -> FillResult:
        slippage_pct = self.costs.draw_slippage_pct(engine)
        fill_price = self.costs.buy_fill_price(open_price, slippage_pct)
        wbtc_qty = size_usd / fill_price
        fees = self.costs.entry_cost_usd(size_usd)
        total_outlay = size_usd + fees
        self.balance -= total_outlay
        return FillResult(engine=engine, side="BUY", fill_price=fill_price,
                           slippage_pct=slippage_pct, wbtc_qty=wbtc_qty,
                           usd_value=size_usd, fees_usd=fees,
                           net_cash_flow=-total_outlay)

    def execute_sell(self, engine: str, position, open_price: float,
                      exit_reason: str) -> FillResult:
        slippage_pct = self.costs.draw_slippage_pct(engine)
        fill_price = self.costs.sell_fill_price(open_price, slippage_pct)
        raw_proceeds = position.wbtc_qty * fill_price
        fees = self.costs.exit_cost_usd(raw_proceeds)
        net_proceeds = raw_proceeds - fees
        self.balance += net_proceeds
        return FillResult(engine=engine, side="SELL", fill_price=fill_price,
                           slippage_pct=slippage_pct, wbtc_qty=position.wbtc_qty,
                           usd_value=raw_proceeds, fees_usd=fees,
                           net_cash_flow=net_proceeds, exit_reason=exit_reason,
                           position=position)


@dataclass
class PortfolioState:
    """One snapshot of the whole shared-pool picture, for a given candle's
    closing price -- everything the equity curve / cycle log needs."""
    vault_balance: float
    tb_position_value: float
    rh_position_value: float
    combined_equity: float
    tb_n_positions: int
    rh_n_tranches: int


def mark_to_market(tb_positions: list, rh_position: Optional[RHPosition], current_close: float) -> tuple[float, float]:
    tb_value = sum(p.wbtc_qty * current_close for p in tb_positions)
    rh_value = rh_position.wbtc_qty * current_close if rh_position else 0.0
    return tb_value, rh_value


def snapshot(vault: Vault, tb_positions: list, rh_position: Optional[RHPosition],
             current_close: float) -> PortfolioState:
    tb_value, rh_value = mark_to_market(tb_positions, rh_position, current_close)
    combined = vault.balance + tb_value + rh_value
    return PortfolioState(
        vault_balance=vault.balance,
        tb_position_value=tb_value,
        rh_position_value=rh_value,
        combined_equity=combined,
        tb_n_positions=len(tb_positions),
        rh_n_tranches=1 if rh_position else 0,
    )
