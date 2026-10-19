"""
The live polling loop. Polls the on-chain price every
config.LIVE_POLL_INTERVAL_SECONDS (20 min by default) and accumulates a
genuine OHLC candle across each 4-hour window (see config.py's comment on
why a single sample per window isn't good enough), finalizing and running
the actual trading logic (the orchestrator's process_candle) exactly once
per window boundary.

Polls are aligned to the clock: :00:05, :20:05 and :40:05 UTC (5 seconds
past each 20-minute mark, so a poll is never a hair before a boundary).
So each 4-hour cycle runs 5 seconds after its window closes, and the
optional `on_poll` hook (the Savings Vault keeper port, which wakes on the
hour like the real keeper) runs right after the real keeper's :00 wake.

Restarts (P10). The in-progress candle is saved after every poll through
an optional `store` (the bot keeps it in data/partial_candle.json). On startup, run_forever recovers it:
  - same window as now: keep accumulating into it (no samples lost);
  - the window that has just ended: process it right away, so a restart
    between a window's end and the next poll no longer drops that cycle
    (this lost candle 934 on 2026-10-14, a restart at 04:05 UTC);
  - anything older: discard with a warning rather than act on stale prices.
Without a store (the paper bot), a restart mid-window starts accumulating
afresh, as before.

Candle source (strategy change A, 2026-10-18). If a `candle_source` is given
(run_bot.py passes bot.swap_candles unless CANDLE_SOURCE=samples), a finished
window's candle is built from every swap in the pool instead of from the
polls' samples, so it no longer depends on poll timing or restarts. The
samples are still collected: if the chain read fails, the cycle runs on the
sampled candle and says so in the log, so a read failure never stops a cycle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from . import config
from .indicators import Candle
from .onchain import get_live_price, OnchainError

log = logging.getLogger("lifedao-bot.scheduler")


def window_start_ts(unix_seconds: int) -> int:
    dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    bucket_hour = (dt.hour // config.CANDLE_INTERVAL_HOURS) * config.CANDLE_INTERVAL_HOURS
    window_dt = dt.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
    return int(window_dt.timestamp())


@dataclass
class PartialCandleBuilder:
    window_ts: Optional[int] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    n_samples: int = 0

    def add_sample(self, price: float, this_window_ts: int) -> None:
        if self.window_ts != this_window_ts:
            self.window_ts, self.open, self.high, self.low, self.close, self.n_samples = (
                this_window_ts, price, price, price, price, 1)
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
            self.close = price
            self.n_samples += 1

    def to_candle(self) -> Candle:
        return Candle(timestamp=self.window_ts, open=self.open, high=self.high, low=self.low, close=self.close)

    def to_dict(self) -> dict:
        return {"window_ts": self.window_ts, "open": self.open, "high": self.high, "low": self.low,
                "close": self.close, "n_samples": self.n_samples}

    @classmethod
    def from_dict(cls, d: dict) -> "PartialCandleBuilder":
        return cls(window_ts=d["window_ts"], open=d["open"], high=d["high"], low=d["low"],
                   close=d["close"], n_samples=d["n_samples"])


class PartialCandleStore(Protocol):
    def load(self) -> Optional[dict]: ...
    def save(self, builder_dict: dict) -> None: ...


def _window_label(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _finish(sim, builder: PartialCandleBuilder, on_cycle_complete, candle_source=None) -> None:
    finished_candle = builder.to_candle()
    if candle_source is not None:
        try:
            finished_candle = candle_source(builder.window_ts)
        except Exception as e:  # noqa: BLE001 -- the sampled candle is the fallback; never skip a cycle
            log.error("Window %s: candle from swaps failed, using the %d-sample candle instead: %s",
                      _window_label(builder.window_ts), builder.n_samples, e)
    log.info("Window %s complete (%d samples) -- running trading logic",
             datetime.fromtimestamp(finished_candle.timestamp, tz=timezone.utc).isoformat(), builder.n_samples)
    rec = sim.process_candle(finished_candle)
    on_cycle_complete(sim, rec)


def recover_builder(sim, on_cycle_complete, store: Optional[PartialCandleStore],
                    now_ts: int, candle_source=None) -> PartialCandleBuilder:
    """Startup: pick up the candle that was being built before a restart.
    The store is expected to return None for a window that was already
    processed (run_bot.py checks it against the saved memory)."""
    if store is None:
        return PartialCandleBuilder()
    try:
        saved = store.load()
    except Exception as e:  # noqa: BLE001 -- recovery must never stop the bot starting
        log.error("Could not read the saved in-progress candle; starting a fresh one: %s", e)
        return PartialCandleBuilder()
    if not saved or saved.get("window_ts") is None:
        return PartialCandleBuilder()
    builder = PartialCandleBuilder.from_dict(saved)
    this_window = window_start_ts(now_ts)
    if builder.window_ts == this_window:
        log.info("Resuming window %s from %d saved samples", _window_label(builder.window_ts), builder.n_samples)
        return builder
    if builder.window_ts == this_window - config.CANDLE_INTERVAL_SECONDS:
        log.warning("Window %s ended while the bot was restarting -- processing it now from %d saved samples",
                    _window_label(builder.window_ts), builder.n_samples)
        _finish(sim, builder, on_cycle_complete, candle_source)
        return PartialCandleBuilder()
    log.warning("Saved in-progress candle for window %s is too old to act on -- discarded (no cycle for it)",
                _window_label(builder.window_ts))
    return PartialCandleBuilder()


def poll_once(sim, builder: PartialCandleBuilder, on_cycle_complete, price: float, now_ts: int,
              store: Optional[PartialCandleStore] = None, candle_source=None) -> PartialCandleBuilder:
    """One poll: finish the previous window if it has ended, add this sample,
    and save the in-progress candle. Returns the builder to keep using."""
    this_window = window_start_ts(now_ts)
    if builder.window_ts is not None and builder.window_ts != this_window:
        _finish(sim, builder, on_cycle_complete, candle_source)
        builder = PartialCandleBuilder()

    builder.add_sample(price, this_window)
    log.info("Sampled price $%.2f (window %s, sample #%d)", price,
             datetime.fromtimestamp(this_window, tz=timezone.utc).strftime("%H:%M"), builder.n_samples)
    if store is not None:
        try:
            store.save(builder.to_dict())
        except Exception as e:  # noqa: BLE001 -- a failed save only weakens restart recovery
            log.error("Could not save the in-progress candle (restart recovery may lose this window): %s", e)
    return builder


POLL_OFFSET_SECONDS = 5


def seconds_until_next_poll(now: float, interval: int = config.LIVE_POLL_INTERVAL_SECONDS,
                            offset: int = POLL_OFFSET_SECONDS) -> float:
    """Time to sleep until the next :00:05 / :20:05 / :40:05 (UTC) mark."""
    next_mark = ((int(now) - offset) // interval + 1) * interval + offset
    return max(1.0, next_mark - now)


def run_forever(sim, on_cycle_complete, store: Optional[PartialCandleStore] = None,
                on_poll=None, candle_source=None) -> None:
    """`on_cycle_complete(sim, cycle_record)` is called once per finalized
    4-hour candle -- run_bot.py wires this up to write the local files and
    save state. `on_poll(now_ts)`, if given, runs after every poll, after
    the trading work (its own failures are caught). Runs forever; only stops
    on process termination (Ctrl+C locally, or Railway sending SIGTERM on
    redeploy)."""
    builder = recover_builder(sim, on_cycle_complete, store, int(time.time()), candle_source)

    while True:
        try:
            price_info = get_live_price()
        except OnchainError as e:
            log.error("Live price read failed this poll, will retry next interval: %s", e)
        else:
            builder = poll_once(sim, builder, on_cycle_complete, price_info["price_usdc_per_wbtc"],
                                int(time.time()), store, candle_source)
        if on_poll is not None:
            try:
                on_poll(int(time.time()))
            except Exception as e:  # noqa: BLE001 -- the hook must never stop the trading loop
                log.error("on_poll hook failed (trading unaffected): %s", e)
        time.sleep(seconds_until_next_poll(time.time()))
