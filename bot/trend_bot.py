"""
Trend Bot (Engine A), tech doc Section 8.

Owns its own open positions, cooldown, and circuit-breaker state. The
orchestrator (engine.py) calls, in this order, once per candle:

  update_peaks(close)                     -- Phase D
  check_exits(...)  -> list[PendingFill]   -- Phase E
  check_entry(...)  -> PendingFill | None  -- Phase G

and separately calls `on_day_roll()` once, on the first candle of each new
UTC day (Phase C), before any of the above.
"""

from __future__ import annotations

from typing import Optional

from . import config
from .indicators import IndicatorSnapshot
from .portfolio import TBPosition, PendingFill


class TrendBotEngine:
    def __init__(self, cfg: config.TrendBotConfig = config.TREND_BOT_CONFIG):
        self.cfg = cfg
        self.positions: list[TBPosition] = []
        self.circuit_breaker_active = False
        self.realized_losses_today = 0.0
        self.cooldown_until_candle_index: Optional[int] = None
        self.capital_conflict_skips: list[dict] = []

    # ---- Phase C -----------------------------------------------------
    def on_day_roll(self) -> None:
        self.circuit_breaker_active = False
        self.realized_losses_today = 0.0

    # ---- Phase D -------------------------------------------------------
    def update_peaks(self, current_close: float) -> None:
        for p in self.positions:
            if p.trailing_armed:
                p.peak_close = max(p.peak_close, current_close)

    def in_cooldown(self, candle_index: int) -> bool:
        return self.cooldown_until_candle_index is not None and candle_index <= self.cooldown_until_candle_index

    # ---- Phase E: exits --------------------------------------------------
    def check_exits(self, candle_index: int, snap: IndicatorSnapshot, regime: int,
                     current_close: float, day_open_equity: float) -> list[PendingFill]:
        fills: list[PendingFill] = []
        open_positions = [p for p in self.positions if not p.pending_exit]
        if not open_positions:
            return fills

        # E1 -- account-level circuit breaker, checked once, closes everything
        if not self.circuit_breaker_active and day_open_equity > 0:
            if self.realized_losses_today >= self.cfg.circuit_breaker_pct / 100 * day_open_equity:
                self.circuit_breaker_active = True
                for p in open_positions:
                    p.pending_exit = True
                    fills.append(PendingFill(engine="TB", side="SELL", signal_candle_index=candle_index,
                                              position=p, exit_reason="CIRCUIT_BREAKER"))
                return fills

        if self.circuit_breaker_active:
            return fills   # no un-pending positions should remain, but be defensive

        # E2 -- trend override, closes everything
        if regime == config.REGIME_TRENDING_DOWN:
            for p in open_positions:
                p.pending_exit = True
                fills.append(PendingFill(engine="TB", side="SELL", signal_candle_index=candle_index,
                                          position=p, exit_reason="TREND_OVERRIDE"))
            return fills

        # E3-E6, per position, first match wins
        for p in open_positions:
            reason = self._per_position_exit_reason(p, snap, current_close, candle_index)
            if reason:
                p.pending_exit = True
                fills.append(PendingFill(engine="TB", side="SELL", signal_candle_index=candle_index,
                                          position=p, exit_reason=reason))
        return fills

    def _per_position_exit_reason(self, p: TBPosition, snap: IndicatorSnapshot, current_close: float,
                                   candle_index: int) -> Optional[str]:
        # E3 -- stop loss (evaluated on CLOSE; fill executes at next open, so
        # gap-through losses beyond the nominal 5% are possible and NOT capped)
        stop_price = p.entry_fill_price * (1 - self.cfg.stop_loss_pct / 100)
        if current_close <= stop_price:
            return "STOP_LOSS"

        # E4 -- trailing stop
        if not p.trailing_armed:
            if current_close >= p.entry_fill_price * (1 + self.cfg.trailing_activation_pct / 100):
                p.trailing_armed = True
                p.peak_close = current_close
                # can't fire the same candle it arms on -- trail_level < peak_close by construction
        else:
            if snap.atr_pct is not None:
                trail_level = p.peak_close * (1 - self.cfg.trailing_atr_multiplier * snap.atr_pct / 100)
                if current_close <= trail_level:
                    return "TRAILING_STOP"

        # E5 -- trend invalidation (1-candle confirmation, i.e. immediate)
        if snap.adx is not None and snap.adx < self.cfg.trend_invalidation_adx_floor:
            return "TREND_INVALIDATION"
        if snap.ema_fast is not None and snap.ema_slow is not None and snap.ema_fast < snap.ema_slow:
            return "TREND_INVALIDATION"
        if snap.ema_fast is not None and current_close < snap.ema_fast:
            return "TREND_INVALIDATION"

        # E6 -- max hold. Fill candle counts as the first candle of the hold,
        # matching Section 10.9 ("clock starts at the fill candle").
        if (candle_index - p.entry_fill_candle_index + 1) >= self.cfg.max_hold_candles:
            return "MAX_HOLD"

        return None

    def record_realized_pnl(self, net_pnl: float) -> None:
        if net_pnl < 0:
            self.realized_losses_today += -net_pnl

    def apply_fill_side_effects(self, exit_reason: str, fill_candle_index: int) -> None:
        """Cooldown only follows a stop-loss exit (Appendix A / Section 8.2)."""
        if exit_reason == "STOP_LOSS":
            self.cooldown_until_candle_index = fill_candle_index + self.cfg.cooldown_after_stop_candles

    # ---- Phase G: entry --------------------------------------------------
    def check_entry(self, candle_index: int, snap: IndicatorSnapshot, regime: int,
                     current_close: float, combined_equity: float, vault, warmup_done: bool) -> Optional[PendingFill]:
        if not warmup_done or self.circuit_breaker_active or self.in_cooldown(candle_index):
            return None
        if len(self.positions) >= self.cfg.max_concurrent_positions:
            return None
        if any(v is None for v in (snap.adx, snap.ema_fast, snap.ema_slow, snap.rsi, snap.macd_line, snap.macd_signal, snap.atr_pct)):
            return None

        conditions = (
            regime == config.REGIME_TRENDING_UP,
            snap.adx > self.cfg.adx_entry_floor,
            snap.ema_fast > snap.ema_slow,
            current_close > snap.ema_fast,
            self.cfg.rsi_entry_low <= snap.rsi <= self.cfg.rsi_entry_high,
            snap.macd_line > snap.macd_signal,
            snap.atr_pct <= self.cfg.atr_entry_ceiling_pct,
        )
        if not all(conditions):
            return None

        if snap.adx >= self.cfg.size_tier_3_floor:
            frac, tier = self.cfg.size_tier_3_frac, 3
        elif snap.adx >= self.cfg.size_tier_2_floor:
            frac, tier = self.cfg.size_tier_2_frac, 2
        else:
            frac, tier = self.cfg.size_tier_1_frac, 1   # never reached given adx_entry_floor=42

        current_tb_value = sum(pos.wbtc_qty * current_close for pos in self.positions)
        max_allowed = self.cfg.max_exposure_frac * combined_equity - current_tb_value
        requested = min(frac * combined_equity, max_allowed)
        if requested <= 0:
            return None

        size_usd = vault.max_affordable_size(requested)
        if size_usd <= 0:
            # Capital-conflict skip: we wanted to size a position but the
            # vault didn't have room (most likely because RH has capital
            # deployed). Log it (Section 11.3) rather than silently dropping it.
            self.capital_conflict_skips.append({
                "candle": candle_index, "engine": "TB", "regime": regime,
                "intended_usd": requested, "vault_available_usd": vault.balance,
            })
            return None

        return PendingFill(engine="TB", side="BUY", signal_candle_index=candle_index,
                            size_usd=size_usd, adx_tier=tier,
                            meta={"adx_at_entry": snap.adx, "rsi_at_entry": snap.rsi})

    def open_position(self, pending_fill: PendingFill, fill_result, candle_index: int, timestamp: int = 0) -> TBPosition:
        pos = TBPosition(
            entry_signal_candle_index=pending_fill.signal_candle_index,
            entry_fill_candle_index=candle_index,
            entry_fill_price=fill_result.fill_price,
            wbtc_qty=fill_result.wbtc_qty,
            size_usd=fill_result.usd_value,
            adx_at_entry=pending_fill.meta.get("adx_at_entry", 0.0),
            rsi_at_entry=pending_fill.meta.get("rsi_at_entry", 0.0),
            adx_tier=pending_fill.adx_tier,
            entry_slippage_pct=fill_result.slippage_pct,
            entry_fees_usd=fill_result.fees_usd,
            entry_timestamp=timestamp,
        )
        self.positions.append(pos)
        return pos
