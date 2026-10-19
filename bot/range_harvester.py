"""
Range Harvester (Engine B), tech doc Section 9.

The trickiest part of this engine is the exit state machine (F1-F7): a
position can be in one of three modes -- plain, handoff (regime flipped to
Trending Up while holding), or mid-trail (price touched the SMA20 midline).
Handoff and mid-trail are mutually exclusive and, once entered, each has its
own suspension rules for the other exits (Section 9.5's per-check notes on
when F6/F7 apply). This module follows that state machine directly rather
than trying to collapse it into fewer branches -- the branches map 1:1 to
F1..F7 so it's checkable line-by-line against the spec.

Peak-price tracking note: unlike the Trend Bot (whose spec text ties
peak_close tracking explicitly to *after* the trailing stop arms), the RH
spec's Phase D instruction ("update peak_price for all open positions") is
unconditional, and F6's arming sub-phase explicitly *resets* peak_price
rather than starting it from scratch -- which only makes sense if it was
already being tracked. So here peak_price updates every candle from entry
onward regardless of handoff/mid_trail state, with an explicit reset only
at F6's arming moment. See update_peaks() and _position_exit_reason().
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from . import config
from .indicators import IndicatorSnapshot
from .portfolio import RHPosition, PendingFill


class ViolatorTracker:
    """Tracks the last `lookback` candles' (close, lower-BB, RSI), so entry
    logic can look back for a "violator" candle (Section 9.4, condition 4/5)
    without keeping the full candle history around."""

    def __init__(self, lookback: int = config.RANGE_HARVESTER_CONFIG.reclaim_lookback_candles):
        self.lookback = lookback
        self.history: deque = deque(maxlen=lookback)

    def find_violator(self):
        """Most recent candle (closest to now) whose own close was below its
        own lower BB, or None. Iterates newest-first so ties resolve to the
        most recent, per Section 9.4's note."""
        for entry in reversed(self.history):
            _, close, bb_lower, _ = entry
            if bb_lower is not None and close < bb_lower:
                return entry
        return None

    def push(self, candle_index: int, close: float, bb_lower: Optional[float], rsi: Optional[float]) -> None:
        self.history.append((candle_index, close, bb_lower, rsi))


class RangeHarvesterEngine:
    def __init__(self, cfg: config.RangeHarvesterConfig = config.RANGE_HARVESTER_CONFIG):
        self.cfg = cfg
        self.position: Optional[RHPosition] = None
        self.circuit_breaker_active = False
        self.realized_losses_today = 0.0
        self.cooldown_until_candle_index: Optional[int] = None
        self.violator_tracker = ViolatorTracker(cfg.reclaim_lookback_candles)
        self.capital_conflict_skips: list[dict] = []

    # ---- Phase C -----------------------------------------------------
    def on_day_roll(self) -> None:
        self.circuit_breaker_active = False
        self.realized_losses_today = 0.0

    # ---- Phase D -------------------------------------------------------
    def update_peaks(self, current_close: float) -> None:
        if self.position is not None:
            self.position.peak_price = max(self.position.peak_price, current_close)

    def update_violator_tracker(self, candle_index: int, current_close: float,
                                 bb_lower: Optional[float], rsi: Optional[float]) -> None:
        self.violator_tracker.push(candle_index, current_close, bb_lower, rsi)

    def in_cooldown(self, candle_index: int) -> bool:
        return self.cooldown_until_candle_index is not None and candle_index <= self.cooldown_until_candle_index

    # ---- Phase F: exits --------------------------------------------------
    def check_exits(self, candle_index: int, snap: IndicatorSnapshot, regime: int,
                     current_close: float, day_open_equity: float) -> list[PendingFill]:
        if self.position is None or self.position.pending_exit:
            return []

        # F1 -- account-level circuit breaker
        if not self.circuit_breaker_active and day_open_equity > 0:
            if self.realized_losses_today >= self.cfg.circuit_breaker_pct / 100 * day_open_equity:
                self.circuit_breaker_active = True
                self.position.pending_exit = True
                return [PendingFill(engine="RH", side="SELL", signal_candle_index=candle_index,
                                     position=self.position, exit_reason="CIRCUIT_BREAKER")]

        if self.circuit_breaker_active:
            return []

        reason = self._position_exit_reason(self.position, snap, regime, current_close, candle_index)
        if reason:
            self.position.pending_exit = True
            return [PendingFill(engine="RH", side="SELL", signal_candle_index=candle_index,
                                 position=self.position, exit_reason=reason)]
        return []

    def _position_exit_reason(self, p: RHPosition, snap: IndicatorSnapshot, regime: int,
                               current_close: float, candle_index: int) -> Optional[str]:
        # F2 -- regime-driven close
        if regime == config.REGIME_TRENDING_DOWN:
            return "REGIME_DOWN_CLOSE"

        # F3 -- handoff latch (state change only, not itself an exit)
        if regime == config.REGIME_TRENDING_UP and not p.handoff and not p.mid_trail:
            p.handoff = True
            p.trailing_armed = True

        # F4 -- handoff trail exit (only while handoff is active)
        if p.handoff and snap.atr_pct is not None:
            trail_level = p.peak_price * (1 - self.cfg.trailing_atr_multiplier * snap.atr_pct / 100)
            if current_close <= trail_level:
                return "HANDOFF_TRAIL"

        # F5 -- hard stop loss, runs regardless of handoff/mid_trail state
        stop_price = p.entry_fill_price * (1 - self.cfg.stop_loss_pct / 100)
        if current_close <= stop_price:
            return "STOP_LOSS"

        if not p.handoff:
            # F6 -- touch-mid-then-trail
            if snap.bb_mid is not None:
                if not p.mid_trail and current_close >= snap.bb_mid:
                    p.mid_trail = True
                    p.trailing_armed = True
                    p.peak_price = current_close   # explicit reset, Section 9.5 F6 sub-phase 6a
                elif p.mid_trail and snap.atr_pct is not None:
                    trail_level = p.peak_price * (1 - self.cfg.trailing_atr_multiplier * snap.atr_pct / 100)
                    if current_close <= trail_level:
                        return "TRAIL_AFTER_MID"

            # F7 -- max hold (suspended only by handoff, NOT by mid_trail)
            if (candle_index - p.entry_fill_candle_index + 1) >= self.cfg.max_hold_candles:
                return "MAX_HOLD"

        return None

    def record_realized_pnl(self, net_pnl: float) -> None:
        if net_pnl < 0:
            self.realized_losses_today += -net_pnl

    def apply_fill_side_effects(self, exit_reason: str, fill_candle_index: int) -> None:
        # Section 9.6: cooldown follows only F1/F2/F5. Not F3 (not an exit),
        # F4, F6, or F7.
        if exit_reason in ("CIRCUIT_BREAKER", "REGIME_DOWN_CLOSE", "STOP_LOSS"):
            self.cooldown_until_candle_index = fill_candle_index + self.cfg.cooldown_candles

    # ---- Phase H: entry --------------------------------------------------
    def check_entry(self, candle_index: int, snap: IndicatorSnapshot, regime: int,
                     current_close: float, combined_equity: float, vault, warmup_done: bool) -> Optional[PendingFill]:
        if self.position is not None:
            return None   # single tranche only, Section 9.2
        if not warmup_done or self.circuit_breaker_active or self.in_cooldown(candle_index):
            return None
        if any(v is None for v in (snap.adx, snap.rsi, snap.atr_pct, snap.bb_lower, snap.bb_mid, snap.rolling_low_60)):
            return None

        # Conditions 1-3, 6-7 of Section 9.4 / pre-check 9.3.1
        if regime not in (config.REGIME_RANGING, config.REGIME_QUIET):
            return None
        if not (snap.adx < self.cfg.adx_entry_ceiling):
            return None
        if not (current_close > snap.bb_lower):   # reclaim: not currently below band
            return None
        if not (snap.atr_pct <= self.cfg.atr_entry_ceiling_pct):
            return None
        gap_pct = abs(snap.bb_lower - snap.rolling_low_60) / current_close * 100
        if not (gap_pct <= self.cfg.boundary_tolerance_pct):
            return None

        # Condition 4-5: violator lookback + RSI
        violator = self.violator_tracker.find_violator()
        if violator is None:
            return None
        _, _, _, v_rsi = violator
        if v_rsi is None or not (v_rsi < self.cfg.rsi_violator_ceiling):
            return None

        # Intended size, needed before the cost-worthiness check (Section 10.7:
        # gas as % of tranche is size-dependent, so evaluate per-user/per-size)
        requested = self.cfg.tranche_frac_combined * combined_equity
        size_usd = vault.max_affordable_size(requested)
        if size_usd <= 0:
            self.capital_conflict_skips.append({
                "candle": candle_index, "engine": "RH", "regime": regime,
                "intended_usd": requested, "vault_available_usd": vault.balance,
            })
            return None

        # Pre-check 9.3.2: cost-worthiness
        half_width_pct = (snap.bb_mid - snap.bb_lower) / current_close * 100
        gas_total = (config.GAS_APPROVAL_USD + config.GAS_SWAP_USD
                     + config.GAS_VAULT_DEPOSIT_USD + config.GAS_VAULT_WITHDRAWAL_USD)
        gas_pct_of_tranche = gas_total / size_usd * 100
        round_trip_cost_pct = 2 * config.POOL_FEE_PCT + 2 * config.SLIPPAGE_MAX_PCT + gas_pct_of_tranche
        if not (half_width_pct > self.cfg.cost_worthiness_multiple * round_trip_cost_pct):
            return None

        return PendingFill(engine="RH", side="BUY", signal_candle_index=candle_index, size_usd=size_usd,
                            meta={"entry_regime": regime, "rsi_violator": v_rsi,
                                  "adx_at_signal": snap.adx, "atr_at_signal": snap.atr_pct})

    def open_position(self, pending_fill: PendingFill, fill_result, candle_index: int, timestamp: int = 0) -> RHPosition:
        pos = RHPosition(
            entry_signal_candle_index=pending_fill.signal_candle_index,
            entry_fill_candle_index=candle_index,
            entry_fill_price=fill_result.fill_price,
            wbtc_qty=fill_result.wbtc_qty,
            size_usd=fill_result.usd_value,
            entry_regime=pending_fill.meta.get("entry_regime", config.REGIME_RANGING),
            rsi_violator=pending_fill.meta.get("rsi_violator", 0.0),
            adx_at_signal=pending_fill.meta.get("adx_at_signal", 0.0),
            atr_at_signal=pending_fill.meta.get("atr_at_signal", 0.0),
            entry_slippage_pct=fill_result.slippage_pct,
            entry_fees_usd=fill_result.fees_usd,
            entry_timestamp=timestamp,
        )
        self.position = pos
        return pos
