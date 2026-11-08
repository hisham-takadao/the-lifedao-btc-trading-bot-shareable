"""Strategy change A: candles built from every swap (bot/swap_candles.py),
and the scheduler's use of them with the sampled candle as fallback.
Offline: the chain is faked."""

import pytest

from bot import swap_candles as sc
from bot.indicators import Candle
from bot.onchain import price_from_sqrt_price_x96
from bot.scheduler import PartialCandleBuilder, poll_once, recover_builder

W = 1_792_281_600                     # 2026-10-18 00:00 UTC
H4 = 4 * 3600
Q = 2 ** 96


def sp(price):
    """sqrtPriceX96 for a USDC-per-WBTC price (token0 WBTC 8 dp, token1 USDC 6 dp)."""
    return int((price / 100) ** 0.5 * Q)


def px(price):
    return price_from_sqrt_price_x96(sp(price))


def tx(n):
    """A made-up transaction hash."""
    return "0x" + format(n, "064x")


# ---------------------------------------------------------------------------
# The candle itself
# ---------------------------------------------------------------------------

def test_candle_uses_last_swap_before_start_and_every_transaction_inside():
    before = [(100, 1, sp(80_000), tx(1)), (101, 0, sp(80_100), tx(2))]     # the later one is the open
    inside = [(105, 2, sp(80_500), tx(3)), (103, 0, sp(79_900), tx(4)), (110, 0, sp(80_200), tx(5))]
    c = sc.build_candle(W, before, inside)
    assert c.timestamp == W
    assert c.open == px(80_100)
    assert c.high == px(80_500) and c.low == px(79_900)
    assert c.close == px(80_200)                                      # last by block/log order


def test_window_without_swaps_keeps_the_standing_price():
    c = sc.build_candle(W, [(100, 0, sp(80_000), tx(1))], [])
    assert (c.open, c.high, c.low, c.close) == (px(80_000),) * 4


def test_no_swap_before_the_window_is_an_error():
    with pytest.raises(sc.OnchainError):
        sc.build_candle(W, [], [(105, 0, sp(80_000), tx(1))])


def test_dip_and_recover_inside_one_transaction_is_ignored():
    """One transaction pushes the pool from ~$110,800 to $110 and back (as
    0xef6923493d... did on 2025-10-20): only its final price counts."""
    before = [(100, 0, sp(110_800), tx(1))]
    inside = [(105, 3, sp(110_900), tx(2)),
              (106, 17, sp(110.86), tx(9)), (106, 24, sp(110.26), tx(9)), (106, 30, sp(110_801), tx(9)),
              (107, 0, sp(110_700), tx(3))]
    c = sc.build_candle(W, before, inside)
    assert c.low == px(110_700) and c.high == px(110_900)
    assert c.close == px(110_700) and c.open == px(110_800)


def test_a_transaction_ending_at_an_extreme_still_counts():
    """The rule drops only prices *between* a transaction's swaps; a
    transaction that leaves the pool at a new low sets the low."""
    before = [(100, 0, sp(80_000), tx(1))]
    inside = [(105, 0, sp(81_000), tx(2)), (105, 1, sp(79_000), tx(2)),     # dips, ends at 79,000
              (106, 0, sp(85_000), tx(3)), (106, 4, sp(80_500), tx(3))]     # spikes, ends at 80,500
    c = sc.build_candle(W, before, inside)
    assert c.low == px(79_000) and c.high == px(80_500) and c.close == px(80_500)


def test_last_swap_per_transaction_keeps_chain_order():
    a, b = tx(7), tx(8)
    swaps = [(106, 2, 3, b), (105, 9, 2, a), (105, 1, 1, a), (106, 5, 4, b.upper()), (107, 0, 5, a)]
    # the same hash in another block is another transaction (can't happen on chain; keyed by block anyway)
    assert sc.last_swap_per_transaction(swaps) == [(105, 9, 2, a), (106, 5, 4, b.upper()), (107, 0, 5, a)]


# ---------------------------------------------------------------------------
# Reading it from a (fake) chain
# ---------------------------------------------------------------------------

class FakeChain:
    """Blocks every 0.5 s from W - 6h, plus Swap logs at chosen blocks."""

    def __init__(self, swaps):
        self.t0, self.b0 = W - 6 * 3600, 1_000_000
        self.latest = self.b0 + 2 * (H4 + 6 * 3600 + 600)
        self.swaps = swaps                       # {block: [price or (price, tx_hash), ...]}
        self.calls = []

    def ts(self, b):
        return self.t0 + (b - self.b0) // 2

    def block_at(self, t):                       # a block whose timestamp is exactly t
        return self.b0 + 2 * (t - self.t0)

    def rpc(self, method, params, timeout=30.0):
        self.calls.append(method)
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(self.ts(int(params[0], 16)))}
        if method == "eth_getLogs":
            f = params[0]
            a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            out = []
            for blk in sorted(self.swaps):
                if a <= blk <= b:
                    for i, p in enumerate(self.swaps[blk]):
                        p, h = p if isinstance(p, tuple) else (p, tx(blk * 1000 + i))   # default: own tx
                        data = "0x" + "0" * 128 + format(sp(p), "064x") + "0" * 128
                        out.append({"address": sc.config.POOL_ADDRESS.lower(), "blockNumber": hex(blk),
                                    "logIndex": hex(i), "data": data, "transactionHash": h})
            return out
        raise AssertionError(method)


def test_candle_for_window_from_the_chain(monkeypatch):
    ch = FakeChain({})
    ch.swaps = {
        ch.block_at(W - 3600): [80_000.0],
        ch.block_at(W) - 1: [80_050.0],          # last swap before the start (same second as W? no: W-1)
        ch.block_at(W): [80_060.0],              # in the start second: the open, and inside the window
        ch.block_at(W + 3600): [81_000.0, 79_500.0],
        ch.block_at(W + 7200): [(80_100.0, tx(42)), (110.0, tx(42)), (80_200.0, tx(42))],   # dip inside one tx
        ch.block_at(W + H4) - 1: [80_400.0],     # last second of the window
        ch.block_at(W + H4): [99_999.0],         # the next window's first second: excluded
    }
    monkeypatch.setattr(sc, "_rpc", ch.rpc)
    c, n = sc.candle_for_window(W)
    assert n == 7
    assert c.open == px(80_060.0)
    assert c.high == px(81_000.0) and c.low == px(79_500.0)
    assert c.close == px(80_400.0)


def test_first_block_at_or_after(monkeypatch):
    ch = FakeChain({})
    monkeypatch.setattr(sc, "_rpc", ch.rpc)
    cache = {}
    assert sc.first_block_at_or_after(W, ch.latest, cache) == ch.block_at(W)
    assert sc.first_block_at_or_after(W + 1, ch.latest, cache) == ch.block_at(W + 1)


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------

class FakeSim:
    def __init__(self):
        self.processed = []
        self.candle_history = []

    def process_candle(self, candle):
        self.processed.append(candle)
        return {}


def polls(sim, source):
    b = PartialCandleBuilder()
    for k, price in enumerate([100.0, 105.0, 95.0]):
        b = poll_once(sim, b, lambda s, r: None, price, W + k * 1200 + 5, None, source)
    return poll_once(sim, b, lambda s, r: None, 101.0, W + H4 + 5, None, source)


def test_scheduler_uses_the_swap_candle_when_given():
    sim = FakeSim()
    swap_candle = Candle(timestamp=W, open=1.0, high=2.0, low=0.5, close=1.5)
    polls(sim, lambda w: swap_candle)
    assert sim.processed == [swap_candle]


def test_scheduler_falls_back_to_samples_if_the_chain_read_fails():
    sim = FakeSim()

    def broken(w):
        raise sc.OnchainError("endpoint down")
    polls(sim, broken)
    c = sim.processed[0]
    assert (c.open, c.high, c.low, c.close) == (100.0, 105.0, 95.0, 95.0)      # the sampled candle


def test_scheduler_without_a_source_is_unchanged():
    sim = FakeSim()
    polls(sim, None)
    c = sim.processed[0]
    assert (c.open, c.high, c.low, c.close) == (100.0, 105.0, 95.0, 95.0)


def test_restart_recovery_also_uses_the_swap_candle():
    class Store:
        def load(self):
            return {"window_ts": W, "open": 1, "high": 1, "low": 1, "close": 1, "n_samples": 3}
    sim = FakeSim()
    swap_candle = Candle(timestamp=W, open=1.0, high=2.0, low=0.5, close=1.5)
    recover_builder(sim, lambda s, r: None, Store(), W + H4 + 60, lambda w: swap_candle)
    assert sim.processed == [swap_candle]
