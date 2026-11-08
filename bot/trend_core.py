"""
The Trend Core (strategy v4). Holds BTC in proportion to a 0-20 trend
score, sized to a 25% annual volatility target (Robert Carver, Systematic
Trading). Replaces the Trend Bot's entries; the Range Harvester keeps
running beside it. TREND_CORE=off switches back to the Trend Bot.

The maths is identical to the original project's backtest;
tests/test_trend_core.py pins it.

  volatility  EW standard deviation of 4-hour returns, span 150 candles
  measures    EMA gaps 24/96, 48/192, 96/384, 192/768 candles, divided by
              price x daily volatility; breakouts over 60, 120, 240, 480
              candles ((close - range midpoint) / range, smoothed EMA N/4)
  score       each x its fixed scalar, capped +-20; equal weights; x the
              diversification multiplier; capped +-20; negative -> 0
  target      equity x score/10 x 25% / annual volatility, at most
              equity - $50; trade only if the gap exceeds 10% of the
              average position (equity x 25% / annual vol) and $10

SCALARS and FDM were measured once on Binance BTC 2024-10-17 -> 2026-10-17
in the original project's backtest and are fixed.
"""

from __future__ import annotations

import math
from collections import deque

EWMAC = [(24, 96), (48, 192), (96, 384), (192, 768)]
BREAKOUT = [60, 120, 240, 480]
N_MEASURES = len(EWMAC) + len(BREAKOUT)
VOL_SPAN = 150
WARMUP = 768
VOL_TARGET = 0.25
CAP = 20.0
BUFFER = 0.10
MIN_TRADE_USD = 10.0
RESERVE_USD = 50.0
CANDLES_PER_DAY = 6

SCALARS = [9.62115898032182, 6.417948432826757, 4.163078412453901, 2.8850194277120798,
           47.07804541689855, 45.62057864716794, 42.87634311025859, 39.05542640092419]
FDM = 1.2176872941827444


def _alpha(span: float) -> float:
    return 2.0 / (span + 1.0)


class TrendCore:
    """The score's moving parts, updated one finished 4-hour candle at a time."""

    def __init__(self, scalars=SCALARS, fdm: float = FDM):
        self.scalars = list(scalars) if scalars is not None else None
        self.fdm = fdm
        self.n = 0
        self.last_ts = None
        self.ema: dict[int, float] = {}
        self.highs: deque = deque(maxlen=max(BREAKOUT))
        self.lows: deque = deque(maxlen=max(BREAKOUT))
        self.bo_smooth: dict[int, float] = {}
        self.prev_close = None
        self.ret_mean = 0.0
        self.ret_var = None
        self.close = None
        self.raw = None

    # -- state ------------------------------------------------------------
    def update(self, high: float, low: float, close: float, ts: int | None = None):
        """Feed one finished candle; returns the 8 raw measures (or None
        until the warm-up is complete or volatility is still zero)."""
        self.n += 1
        self.last_ts = ts
        self.close = close
        if self.prev_close is not None and self.prev_close > 0:
            r = close / self.prev_close - 1
            a = _alpha(VOL_SPAN)
            if self.ret_var is None:
                self.ret_mean, self.ret_var = r, 0.0
            else:
                d = r - self.ret_mean
                self.ret_mean += a * d
                self.ret_var = (1 - a) * (self.ret_var + a * d * d)
        self.prev_close = close
        for span in sorted({s for pair in EWMAC for s in pair}):
            prev = self.ema.get(span)
            self.ema[span] = close if prev is None else prev + _alpha(span) * (close - prev)
        self.highs.append(high)
        self.lows.append(low)
        for n in BREAKOUT:
            hs, ls = list(self.highs)[-n:], list(self.lows)[-n:]
            hi, lo = max(hs), min(ls)
            raw0 = (close - (hi + lo) / 2) / (hi - lo) if hi > lo else 0.0
            prev = self.bo_smooth.get(n)
            self.bo_smooth[n] = raw0 if prev is None else prev + _alpha(n / 4) * (raw0 - prev)
        self.raw = self._raw()
        return self.raw

    @property
    def ready(self) -> bool:
        return self.raw is not None

    def daily_vol(self):
        if self.ret_var is None or self.ret_var <= 0:
            return None
        return math.sqrt(self.ret_var) * math.sqrt(CANDLES_PER_DAY)

    def annual_vol(self):
        d = self.daily_vol()
        return None if d is None else d * math.sqrt(365)

    def _raw(self):
        dv = self.daily_vol()
        if self.n < WARMUP or dv is None:
            return None
        out = [(self.ema[f] - self.ema[s]) / (self.close * dv) for f, s in EWMAC]
        out += [self.bo_smooth[n] for n in BREAKOUT]
        return out

    # -- score and target -------------------------------------------------
    def scaled(self):
        if self.raw is None or self.scalars is None:
            return None
        return [max(-CAP, min(CAP, v * k)) for v, k in zip(self.raw, self.scalars)]

    def score(self) -> float:
        sc = self.scaled()
        if sc is None:
            return 0.0
        combined = self.fdm * sum(sc) / N_MEASURES
        return max(0.0, min(CAP, combined))

    def target_usd(self, equity: float) -> float:
        av = self.annual_vol()
        if av is None or av <= 0:
            return 0.0
        t = equity * self.score() / 10 * VOL_TARGET / av
        return max(0.0, min(t, equity - RESERVE_USD))

    def buffer_usd(self, equity: float) -> float:
        av = self.annual_vol()
        return 0.0 if not av else BUFFER * equity * VOL_TARGET / av

    # -- persistence --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "n": self.n, "last_ts": self.last_ts,
            "ema": {str(k): v for k, v in self.ema.items()},
            "highs": list(self.highs), "lows": list(self.lows),
            "bo_smooth": {str(k): v for k, v in self.bo_smooth.items()},
            "prev_close": self.prev_close, "ret_mean": self.ret_mean, "ret_var": self.ret_var,
            "close": self.close,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrendCore":
        tc = cls()
        tc.n, tc.last_ts = d["n"], d.get("last_ts")
        tc.ema = {int(k): v for k, v in d["ema"].items()}
        tc.highs.extend(d["highs"])
        tc.lows.extend(d["lows"])
        tc.bo_smooth = {int(k): v for k, v in d["bo_smooth"].items()}
        tc.prev_close, tc.ret_mean, tc.ret_var, tc.close = d["prev_close"], d["ret_mean"], d["ret_var"], d["close"]
        tc.raw = tc._raw()
        return tc
