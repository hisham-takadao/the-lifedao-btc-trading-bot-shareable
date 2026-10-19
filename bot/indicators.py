"""
Incremental technical indicators, per the tech doc, Section 3.

Design choice: every indicator here is a small stateful object with a single
`update(candle)` method that consumes ONE new candle and returns the new
value (or None while still warming up). This is deliberate, not just a style
preference:

  - It naturally supports both modes the bot needs: replaying ~420 historical
    candles once at startup (bootstrap) and then being fed one new live
    candle every 4 hours forever after. Same code path, no separate
    "backtest engine" vs "live engine" to keep in sync.
  - It matches the spec's own framing in Section 3: "computed once per
    candle using only data up to and including that candle (no look-ahead)."
    An incremental updater can *only* see what it's been fed so far -- there
    is no array to accidentally index into the future with.

A resolved ambiguity, noted here because it affects every EMA-based value in
the system: Section 3's table labels EMA Fast/Slow as "Exponential, standard
Wilder-style", which is a bit of a contradiction in standard TA terminology
-- "EMA" conventionally means alpha = 2/(N+1), while "Wilder smoothing"
(explicitly used elsewhere in the same table for RSI/ADX/ATR) means
alpha = 1/N. Since EMA20/EMA50 crossovers and MACD's constituent EMAs are
universally defined with the standard 2/(N+1) constant in every mainstream
platform and MACD's own formula here ("EMA(12) - EMA(26)") only makes sense
under that convention, this module uses:
  - Standard EMA (2/(N+1))  for: EMA fast/slow, MACD's two EMAs, MACD signal
  - Wilder smoothing (1/N)  for: RSI, ADX (+DM/-DM/DX), ATR
If that reading is wrong, `StandardEMA` and `WilderMA` below are the only
two places the constant is defined -- swap what EMAFast/EMASlow are built
from and everything downstream follows.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass
class Candle:
    timestamp: int    # unix seconds, candle OPEN time
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class StandardEMA:
    """Exponential moving average, alpha = 2/(N+1), SMA-seeded."""

    def __init__(self, period: int):
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self._seed_buffer: list[float] = []
        self.value: Optional[float] = None

    def update(self, x: float) -> Optional[float]:
        if self.value is not None:
            self.value = self.alpha * x + (1 - self.alpha) * self.value
            return self.value
        self._seed_buffer.append(x)
        if len(self._seed_buffer) >= self.period:
            self.value = sum(self._seed_buffer) / self.period
        return self.value


class WilderMA:
    """Wilder smoothing, alpha = 1/N, SMA-seeded (RSI / ADX / ATR use this)."""

    def __init__(self, period: int):
        self.period = period
        self._seed_buffer: list[float] = []
        self.value: Optional[float] = None

    def update(self, x: float) -> Optional[float]:
        if self.value is not None:
            self.value = (self.value * (self.period - 1) + x) / self.period
            return self.value
        self._seed_buffer.append(x)
        if len(self._seed_buffer) >= self.period:
            self.value = sum(self._seed_buffer) / self.period
        return self.value


class RollingWindow:
    """Fixed-size window of raw values, for SMA/stddev/rolling-high-low."""

    def __init__(self, size: int):
        self.size = size
        self.buf: deque = deque(maxlen=size)

    def update(self, x: float) -> None:
        self.buf.append(x)

    @property
    def ready(self) -> bool:
        return len(self.buf) == self.size

    def mean(self) -> Optional[float]:
        if not self.ready:
            return None
        return sum(self.buf) / self.size

    def population_stddev(self) -> Optional[float]:
        if not self.ready:
            return None
        m = self.mean()
        return (sum((v - m) ** 2 for v in self.buf) / self.size) ** 0.5

    def max(self) -> Optional[float]:
        return max(self.buf) if self.buf else None

    def min(self) -> Optional[float]:
        return min(self.buf) if self.buf else None


@dataclass
class IndicatorSnapshot:
    """Everything the regime classifier and both engines need, for one candle."""

    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    rsi: Optional[float] = None
    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    atr_abs: Optional[float] = None
    atr_pct: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_mid: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_width_pct: Optional[float] = None
    rolling_high_60: Optional[float] = None
    rolling_low_60: Optional[float] = None

    @property
    def ready(self) -> bool:
        """True once every indicator has a real value (i.e. warm-up is over)."""
        return all(
            v is not None
            for v in (
                self.ema_fast, self.ema_slow, self.rsi, self.macd_line,
                self.macd_signal, self.adx, self.atr_pct, self.bb_upper,
                self.bb_mid, self.bb_lower, self.rolling_high_60, self.rolling_low_60,
            )
        )


class IndicatorEngine:
    """
    Feed candles one at a time via `update(candle)`; get back an
    IndicatorSnapshot. Internally keeps every piece of state each indicator
    needs between calls.
    """

    def __init__(self):
        self.ema_fast = StandardEMA(config.EMA_FAST_PERIOD)
        self.ema_slow = StandardEMA(config.EMA_SLOW_PERIOD)

        self._macd_ema_fast = StandardEMA(config.MACD_FAST)
        self._macd_ema_slow = StandardEMA(config.MACD_SLOW)
        self._macd_signal_ema = StandardEMA(config.MACD_SIGNAL)

        self._rsi_avg_gain = WilderMA(config.RSI_PERIOD)
        self._rsi_avg_loss = WilderMA(config.RSI_PERIOD)

        self._atr_wilder = WilderMA(config.ATR_PERIOD)
        self._plus_dm_wilder = WilderMA(config.ADX_PERIOD)
        self._minus_dm_wilder = WilderMA(config.ADX_PERIOD)
        self._dx_wilder = WilderMA(config.ADX_PERIOD)   # DX -> ADX

        self._bb_window = RollingWindow(config.BB_PERIOD)
        self._high_window = RollingWindow(config.ROLLING_WINDOW)
        self._low_window = RollingWindow(config.ROLLING_WINDOW)

        self._prev_close: Optional[float] = None
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None

        self.n_candles_seen = 0

    def update(self, candle: Candle) -> IndicatorSnapshot:
        close, high, low = candle.close, candle.high, candle.low
        self.n_candles_seen += 1

        ema_fast = self.ema_fast.update(close)
        ema_slow = self.ema_slow.update(close)

        macd_fast = self._macd_ema_fast.update(close)
        macd_slow = self._macd_ema_slow.update(close)
        macd_line = None
        macd_signal = None
        if macd_fast is not None and macd_slow is not None:
            macd_line = macd_fast - macd_slow
            macd_signal = self._macd_signal_ema.update(macd_line)

        # --- RSI ---
        rsi = None
        if self._prev_close is not None:
            delta = close - self._prev_close
            gain = max(delta, 0.0)
            loss = max(-delta, 0.0)
            avg_gain = self._rsi_avg_gain.update(gain)
            avg_loss = self._rsi_avg_loss.update(loss)
            if avg_gain is not None and avg_loss is not None:
                if avg_gain + avg_loss == 0:
                    rsi = 50.0
                elif avg_loss == 0:
                    rsi = 100.0
                else:
                    rs = avg_gain / avg_loss
                    rsi = 100.0 - (100.0 / (1.0 + rs))

        # --- True Range / ATR ---
        atr_abs = None
        atr_pct = None
        plus_di = None
        minus_di = None
        adx = None
        if self._prev_close is not None:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
            atr_abs = self._atr_wilder.update(tr)
            if atr_abs is not None and close != 0:
                atr_pct = atr_abs / close * 100.0

            up_move = high - self._prev_high
            down_move = self._prev_low - low
            plus_dm_raw = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm_raw = down_move if (down_move > up_move and down_move > 0) else 0.0
            plus_dm = self._plus_dm_wilder.update(plus_dm_raw)
            minus_dm = self._minus_dm_wilder.update(minus_dm_raw)

            if atr_abs is not None and atr_abs > 0 and plus_dm is not None and minus_dm is not None:
                plus_di = 100.0 * plus_dm / atr_abs
                minus_di = 100.0 * minus_dm / atr_abs
                di_sum = plus_di + minus_di
                dx = 0.0 if di_sum == 0 else 100.0 * abs(plus_di - minus_di) / di_sum
                adx = self._dx_wilder.update(dx)

        # --- Bollinger Bands ---
        self._bb_window.update(close)
        bb_mid = self._bb_window.mean()
        bb_upper = bb_lower = bb_width_pct = None
        if bb_mid is not None:
            sd = self._bb_window.population_stddev()
            bb_upper = bb_mid + config.BB_STDDEV * sd
            bb_lower = bb_mid - config.BB_STDDEV * sd
            if close != 0:
                bb_width_pct = (bb_upper - bb_lower) / close * 100.0

        # --- Rolling 60-candle high/low (Donchian) ---
        self._high_window.update(high)
        self._low_window.update(low)
        rolling_high_60 = self._high_window.max() if self._high_window.ready else None
        rolling_low_60 = self._low_window.min() if self._low_window.ready else None

        self._prev_close = close
        self._prev_high = high
        self._prev_low = low

        return IndicatorSnapshot(
            ema_fast=ema_fast, ema_slow=ema_slow, rsi=rsi,
            macd_line=macd_line, macd_signal=macd_signal,
            adx=adx, plus_di=plus_di, minus_di=minus_di,
            atr_abs=atr_abs, atr_pct=atr_pct,
            bb_upper=bb_upper, bb_mid=bb_mid, bb_lower=bb_lower, bb_width_pct=bb_width_pct,
            rolling_high_60=rolling_high_60, rolling_low_60=rolling_low_60,
        )
