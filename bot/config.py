"""
Central configuration for the lifedao Combined Portfolio simulation.

Every number in this file is taken directly from "lifedao Combined Portfolio -
Technical Algorithm Design Specification v1.0" (the tech doc). Section
references in comments point back to where each value comes from, so anyone
checking this against the spec later doesn't have to hunt for it.

One deliberate resolution of a conflict in the source document:
Section 8.2 and Section 8.5 (E1) both state the Trend Bot's circuit breaker
is 7% of day-opening combined equity. Appendix A's quick-reference table
lists it as 5.0%. We use 7% here because it's stated twice in the body text
and matches the Range Harvester's own breaker. If the 5% figure was actually
intended, change TB_CIRCUIT_BREAKER_PCT below -- nothing else references
the old value.
"""

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Network / on-chain identity (Section 1, System Overview)
# ---------------------------------------------------------------------------
CHAIN_NAME = "Arbitrum One"
CHAIN_ID = 42161
EXCHANGE = "Uniswap V3"
POOL_ADDRESS = "0x0E4831319A50228B9e450861297aB92dee15B44F"
TOKEN0_WBTC = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"   # 8 decimals
TOKEN1_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"   # 6 decimals
POOL_FEE_UNITS = 500                                          # Uniswap fee units (500 / 1e6 = 0.05%)
WBTC_DECIMALS = 8
USDC_DECIMALS = 6

# Verified live against the pool contract on 2026-07-15: token0/token1/fee
# all matched the spec exactly. The bot re-checks these on every startup
# (see onchain.py: verify_pool()) and refuses to run if they ever don't --
# same "abort and alert on mismatch" philosophy as the source data checksum
# check in Section 2.1.

# ---------------------------------------------------------------------------
# Candle / timing (Section 1, Section 2.2, Section 2.4)
# ---------------------------------------------------------------------------
CANDLE_INTERVAL_HOURS = 4
CANDLE_INTERVAL_SECONDS = CANDLE_INTERVAL_HOURS * 3600
UTC_CANDLE_BOUNDARIES = (0, 4, 8, 12, 16, 20)   # hours, per Section 2.2
WARMUP_CANDLES = 300                             # Section 2.4: 50 days x 6 candles/day
# Extra buffer beyond the bare minimum warm-up so EMA-50 / ADX-14 / the
# 60-candle Donchian window have actually converged, not just "exist". This
# only affects how much history we bootstrap at first startup.
BOOTSTRAP_BUFFER_CANDLES = 120
BOOTSTRAP_TOTAL_CANDLES = WARMUP_CANDLES + BOOTSTRAP_BUFFER_CANDLES   # 420 candles = 70 days

# How many recent candles get saved to the "State" tab for crash/restart
# recovery. On restart, these are replayed through a fresh indicator engine
# + regime classifier + violator tracker to rebuild their derived state
# exactly -- comfortably more than the slowest indicator here (EMA-50 /
# the 60-candle Donchian window) needs to fully reconverge.
STATE_CANDLE_HISTORY_SIZE = 500

# How often the live loop polls the on-chain price *within* each 4-hour
# window. Polling only once per window would make every live candle have
# open=high=low=close (zero intra-candle range), which would silently
# understate ATR and break the +DM/-DM inputs to ADX -- both of which gate
# real entry/exit decisions. Polling every 20 minutes gives ~12 samples per
# window, enough for a genuine (if coarse) high/low range, at a call volume
# (~72/day) that's trivial for a free public RPC endpoint.
LIVE_POLL_INTERVAL_SECONDS = 20 * 60

# ---------------------------------------------------------------------------
# Indicators (Section 3)
# ---------------------------------------------------------------------------
EMA_FAST_PERIOD = 20
EMA_SLOW_PERIOD = 50
RSI_PERIOD = 14                 # Wilder smoothing
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_PERIOD = 14                 # Wilder smoothing
ATR_PERIOD = 14
BB_PERIOD = 20
BB_STDDEV = 2.0
ROLLING_WINDOW = 60              # Donchian / rolling high-low, Section 9.2

# ---------------------------------------------------------------------------
# Regime classifier (Section 4)
# ---------------------------------------------------------------------------
QUIET_BB_WIDTH_PCT = 5.0
QUIET_CONSECUTIVE_CANDLES = 18
TREND_ADX_THRESHOLD = 40
REGIME_CONFIRM_CANDLES = 2       # only for entering Trending Up/Down

# Regime integer codes (Section 4.3) -- used in log output
REGIME_RANGING = 0
REGIME_TRENDING_UP = 1
REGIME_TRENDING_DOWN = 2
REGIME_QUIET = 3
REGIME_LABELS = {
    REGIME_RANGING: "RANGING",
    REGIME_TRENDING_UP: "TRENDING_UP",
    REGIME_TRENDING_DOWN: "TRENDING_DOWN",
    REGIME_QUIET: "QUIET",
}

# ---------------------------------------------------------------------------
# Shared capital pool (Section 5, Section 7.1)
# ---------------------------------------------------------------------------
VAULT_APY_PCT = 3.5
CANDLES_PER_YEAR = 365.25 * 24 / CANDLE_INTERVAL_HOURS   # 2191.5
VAULT_PER_CANDLE_FACTOR = (1 + VAULT_APY_PCT / 100) ** (1 / CANDLES_PER_YEAR)
MIN_VAULT_BALANCE = 50.0     # both engines, Section 5.3 / 8.2 / 9.2
ENTRY_PRIORITY = ("TB", "RH")          # Section 5.4: TB wins simultaneous signals

# ---------------------------------------------------------------------------
# Transaction cost model (Section 7.2 - 7.4)
# ---------------------------------------------------------------------------
POOL_FEE_PCT = 0.05             # per leg
GAS_APPROVAL_USD = 0.02
GAS_SWAP_USD = 0.02
GAS_VAULT_DEPOSIT_USD = 0.02
GAS_VAULT_WITHDRAWAL_USD = 0.03
SLIPPAGE_MIN_PCT = 0.0
SLIPPAGE_MAX_PCT = 0.10
RNG_BASE_SEED = 42               # Section 7.3; + a per-run tier index if we ever run multiple tiers

# ---------------------------------------------------------------------------
# Trend Bot / Engine A (Section 8, "Run 13 Configuration")
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TrendBotConfig:
    adx_entry_floor: float = 42.0
    rsi_entry_low: float = 45.0
    rsi_entry_high: float = 75.0
    atr_entry_ceiling_pct: float = 8.0
    ema_fast: int = EMA_FAST_PERIOD
    ema_slow: int = EMA_SLOW_PERIOD
    # ADX tiers -> position size as a fraction of combined equity (Section 8.4)
    size_tier_1_floor: float = 25.0   # never reached: entry requires ADX > 42
    size_tier_1_frac: float = 0.15
    size_tier_2_floor: float = 35.0
    size_tier_2_frac: float = 0.20
    size_tier_3_floor: float = 50.0
    size_tier_3_frac: float = 0.25
    max_concurrent_positions: int = 2
    max_exposure_frac: float = 0.40
    stop_loss_pct: float = 5.0
    trailing_activation_pct: float = 3.0
    trailing_atr_multiplier: float = 1.5
    trend_invalidation_adx_floor: float = 20.0
    max_hold_candles: int = 12
    cooldown_after_stop_candles: int = 6
    circuit_breaker_pct: float = 7.0     # see module docstring re: Appendix A conflict
    min_vault_after_fill: float = MIN_VAULT_BALANCE


# ---------------------------------------------------------------------------
# Range Harvester / Engine B (Section 9, "Run 7 Configuration")
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RangeHarvesterConfig:
    adx_entry_ceiling: float = 25.0
    reclaim_lookback_candles: int = 3
    rsi_violator_ceiling: float = 35.0
    atr_entry_ceiling_pct: float = 6.0
    boundary_tolerance_pct: float = 2.0
    cost_worthiness_multiple: float = 3.0
    donchian_period: int = ROLLING_WINDOW
    tranche_frac_standalone: float = 0.78     # not used in combined mode; kept for reference
    tranche_frac_combined: float = 0.65        # Section 10.3 override, combined_config.yaml
    max_tranches: int = 1
    stop_loss_pct: float = 7.0
    trailing_atr_multiplier: float = 1.5
    max_hold_candles: int = 30
    cooldown_candles: int = 12
    circuit_breaker_pct: float = 7.0
    min_vault_after_fill: float = MIN_VAULT_BALANCE
    bb_period: int = BB_PERIOD
    bb_stddev: float = BB_STDDEV


TREND_BOT_CONFIG = TrendBotConfig()
RANGE_HARVESTER_CONFIG = RangeHarvesterConfig()


# ---------------------------------------------------------------------------
# Simulation-level settings (not part of the algorithm spec itself --
# these are choices for how *this* paper-trading deployment runs)
# ---------------------------------------------------------------------------
DEFAULT_STARTING_CAPITAL = 10000.0   # matches whitepaper's "User C" headline tier
