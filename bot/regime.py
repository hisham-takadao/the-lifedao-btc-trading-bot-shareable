"""
Market regime classifier, per the tech doc, Section 4.

Runs once per candle, on the shared IndicatorSnapshot, and produces exactly
one of the four regime codes in config.REGIME_*. Both engines read this same
output -- neither one re-derives it.

Two behaviors that are easy to get wrong and are called out explicitly in
the spec (Section 4.2 and its notes), so this implementation is built
around them rather than around the more "obvious" naive version:

  1. Entering Trending Up/Down needs 2 consecutive qualifying candles.
     Exiting back to Ranging needs zero -- it's immediate the moment the
     ADX/EMA condition no longer holds. So a broken trend and a freshly
     confirmed opposite trend are two separate events, not one flip; if
     the confirmed trend breaks on candle N and the opposite direction
     only starts its own 2-candle count on candle N, the regime spends
     exactly one candle back at Ranging before the new trend can confirm.
  2. Quiet has no confirmation delay in either direction. It becomes
     active the instant the 18-consecutive-candle condition is met, and
     deactivates the instant it's broken -- and while it's overriding the
     displayed label, the Up/Down trend confirmation counters underneath
     keep running rather than freezing. So if a trend was already
     confirmed before Quiet kicked in, the label can snap straight back
     to that trend the moment Quiet ends, with no fresh 2-candle wait.
     (The spec doesn't spell out this particular interaction explicitly;
     this is the more literal reading of "Quiet overrides the
     classification" as opposed to "Quiet pauses the classification",
     and it's the one implemented here. Flagging it since it's the kind
     of edge case worth watching in the cycle log if it ever fires.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config
from .indicators import IndicatorSnapshot


@dataclass
class RegimeState:
    """Exposed for logging -- lets the cycle log show *why* a regime is
    what it is, not just the final code."""
    regime: int
    quiet_streak: int
    pending_direction: Optional[str]
    pending_count: int
    confirmed_trend: Optional[str]


class RegimeClassifier:
    def __init__(self):
        self.consecutive_quiet_streak = 0
        self.confirmed_trend: Optional[str] = None   # None | "UP" | "DOWN"
        self.pending_direction: Optional[str] = None
        self.pending_count = 0

    def update(self, snap: IndicatorSnapshot) -> RegimeState:
        if snap.bb_width_pct is None or snap.adx is None or snap.ema_fast is None or snap.ema_slow is None:
            # Still warming up -- default to Ranging, matches "all candles
            # not matching the above conditions are Ranging" as the safe
            # fallback when we simply don't have enough data yet to evaluate
            # the other three.
            return RegimeState(config.REGIME_RANGING, self.consecutive_quiet_streak,
                                self.pending_direction, self.pending_count, self.confirmed_trend)

        # --- Priority 1: Quiet streak (Section 4.1) ---
        if snap.bb_width_pct < config.QUIET_BB_WIDTH_PCT:
            self.consecutive_quiet_streak += 1
        else:
            self.consecutive_quiet_streak = 0
        is_quiet = self.consecutive_quiet_streak >= config.QUIET_CONSECUTIVE_CANDLES

        # --- Priority 2/3: trend condition, with 2-candle confirmation to ENTER ---
        raw_signal: Optional[str] = None
        if snap.adx > config.TREND_ADX_THRESHOLD:
            if snap.ema_fast > snap.ema_slow:
                raw_signal = "UP"
            elif snap.ema_fast < snap.ema_slow:
                raw_signal = "DOWN"

        if raw_signal is not None and raw_signal == self.pending_direction:
            self.pending_count += 1
        elif raw_signal is not None:
            self.pending_direction = raw_signal
            self.pending_count = 1
        else:
            self.pending_direction = None
            self.pending_count = 0

        # Immediate exit the instant the CONFIRMED trend's own condition breaks
        if self.confirmed_trend == "UP" and raw_signal != "UP":
            self.confirmed_trend = None
        elif self.confirmed_trend == "DOWN" and raw_signal != "DOWN":
            self.confirmed_trend = None

        # New trend confirms after 2 consecutive matching candles
        if self.pending_count >= 2 and self.pending_direction is not None:
            self.confirmed_trend = self.pending_direction

        # --- Final label: Quiet overrides trend; Ranging is the default ---
        if is_quiet:
            regime = config.REGIME_QUIET
        elif self.confirmed_trend == "UP":
            regime = config.REGIME_TRENDING_UP
        elif self.confirmed_trend == "DOWN":
            regime = config.REGIME_TRENDING_DOWN
        else:
            regime = config.REGIME_RANGING

        return RegimeState(regime, self.consecutive_quiet_streak,
                            self.pending_direction, self.pending_count, self.confirmed_trend)
