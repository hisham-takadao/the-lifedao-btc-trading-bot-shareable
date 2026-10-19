"""Tests for P10: a restart must not drop or repeat a 4-hour cycle. The
in-progress candle is saved each poll and recovered at startup.
Replays 2026-10-14, when a restart at 04:05 UTC lost candle 934."""

from datetime import datetime, timezone

import pytest

import run_bot as live_main
from bot.indicators import Candle
from bot.scheduler import PartialCandleBuilder, poll_once, recover_builder


def utc(h, m, day=14):
    return int(datetime(2026, 10, day, h, m, tzinfo=timezone.utc).timestamp())


class FakeSim:
    def __init__(self, last_ts=None):
        self.processed = []
        self.candle_history = [Candle(timestamp=last_ts, open=1, high=1, low=1, close=1)] if last_ts else []

    def process_candle(self, candle):
        self.processed.append(candle)
        self.candle_history.append(candle)
        return {"candle": candle.timestamp}


class MemoryStore:
    def __init__(self):
        self.saved = None
        self.fail_save = False

    def load(self):
        return self.saved

    def save(self, d):
        if self.fail_save:
            raise RuntimeError("Sheets 429")
        self.saved = dict(d)


def run_polls(sim, store, polls, builder=None, completed=None):
    builder = builder or PartialCandleBuilder()
    completed = completed if completed is not None else []
    for ts, price in polls:
        builder = poll_once(sim, builder, lambda s, rec: completed.append(rec), price, ts, store)
    return builder, completed


def test_restart_after_window_end_processes_it_now():
    """The 2026-10-14 case: samples at 03:18/03:38/03:58, restart at 04:05."""
    sim, store = FakeSim(last_ts=utc(20, 0, day=13)), MemoryStore()
    run_polls(sim, store, [(utc(3, 18), 83_400.0), (utc(3, 38), 83_550.0), (utc(3, 58), 83_480.0)])
    assert sim.processed == []                    # the window hadn't been closed out yet

    # --- restart: fresh process, nothing in memory ---
    completed = []
    builder = recover_builder(sim, lambda s, rec: completed.append(rec), store, utc(4, 5))
    assert len(sim.processed) == 1
    c = sim.processed[0]
    assert (c.timestamp, c.open, c.high, c.low, c.close) == (utc(0, 0), 83_400.0, 83_550.0, 83_400.0, 83_480.0)
    assert completed == [{"candle": utc(0, 0)}]   # cycle log / state save path ran
    assert builder.window_ts is None              # next poll starts the 04:00 window

    run_polls(sim, store, [(utc(4, 6), 83_470.0)], builder)
    assert store.saved["window_ts"] == utc(4, 0) and len(sim.processed) == 1


def test_restart_mid_window_keeps_earlier_samples():
    sim, store = FakeSim(last_ts=utc(20, 0, day=13)), MemoryStore()
    run_polls(sim, store, [(utc(0, 10), 83_000.0), (utc(0, 30), 84_100.0)])   # high before the restart

    builder = recover_builder(sim, lambda s, rec: None, store, utc(1, 0))
    assert builder.window_ts == utc(0, 0) and builder.n_samples == 2 and sim.processed == []

    run_polls(sim, store, [(utc(1, 0), 83_200.0), (utc(4, 1), 83_300.0)], builder)
    c = sim.processed[0]
    assert (c.open, c.high, c.close) == (83_000.0, 84_100.0, 83_200.0)


def test_window_from_long_ago_is_not_acted_on():
    sim, store = FakeSim(last_ts=utc(16, 0, day=13)), MemoryStore()
    store.saved = PartialCandleBuilder(window_ts=utc(20, 0, day=13), open=1.0, high=1.0, low=1.0,
                                       close=1.0, n_samples=3).to_dict()
    builder = recover_builder(sim, lambda s, rec: None, store, utc(4, 5))   # two windows later
    assert sim.processed == [] and builder.window_ts is None


def test_sheet_store_never_returns_an_already_processed_window():
    """Restart just after a cycle ran but before the next poll saved the new
    window: the saved candle is the one just processed and must not rerun."""
    class Sheet:
        saved = PartialCandleBuilder(window_ts=utc(0, 0), open=1.0, high=1.0, low=1.0, close=1.0,
                                     n_samples=12).to_dict()

        def load_partial_candle(self):
            return self.saved

    processed = FakeSim(last_ts=utc(0, 0))
    assert live_main.LocalCandleStore(Sheet(), processed).load() is None
    not_yet = FakeSim(last_ts=utc(20, 0, day=13))
    assert live_main.LocalCandleStore(Sheet(), not_yet).load()["window_ts"] == utc(0, 0)


def test_store_failures_never_stop_the_loop():
    sim, store = FakeSim(), MemoryStore()
    store.fail_save = True
    builder, _ = run_polls(sim, store, [(utc(0, 10), 1.0), (utc(0, 30), 2.0)])
    assert builder.n_samples == 2

    class Broken:
        def load(self):
            raise RuntimeError("sheet unavailable")

    assert recover_builder(sim, lambda s, rec: None, Broken(), utc(0, 40)).window_ts is None


def test_no_store_behaves_as_before():
    sim = FakeSim()
    assert recover_builder(sim, lambda s, rec: None, None, utc(4, 5)).window_ts is None
    builder, completed = run_polls(sim, None, [(utc(0, 10), 1.0), (utc(4, 10), 2.0)])
    assert len(sim.processed) == 1 and builder.window_ts == utc(4, 0)
