"""Strategy v4 (the Trend Core) in the live bot, and the TWAP guard.
Offline: the sheet is in memory and every swap or chain read is faked."""

import dataclasses
import json
import math

import pytest

from bot import live_execution, live_orchestrator
from bot.indicators import Candle
from bot.live_config import MAINNET
from bot.live_execution import LiveFillResult, TwapDeviationError, check_twap
from bot.live_proposals import (Proposal, STATUS_APPROVED, STATUS_EXECUTED, STATUS_EXPIRED_STALE, STATUS_FAILED)
from bot.live_state_store import restore_live_state, serialize_live_state
from bot.onchain import decode_observe_twap_tick, tick_to_price
from bot.trend_core import TrendCore, WARMUP
from bot.trend_core_history import HistoryError, _continuous_until, warm_trend_core

WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"   # public Hardhat test address, holds no funds
H4 = 4 * 3600
T0 = 1_760_000_000 - 1_760_000_000 % H4


def path(n, start=100.0, drift=0.0, wiggle=0.01, t0=T0):
    out, p = [], start
    for i in range(n):
        p *= 1 + drift + (wiggle if i % 2 else -wiggle)
        out.append(Candle(timestamp=t0 + i * H4, open=p, high=p * 1.002, low=p * 0.998, close=p))
    return out


def fed(candles):
    tc = TrendCore()
    for c in candles:
        tc.update(c.high, c.low, c.close, c.timestamp)
    return tc


# ---------------------------------------------------------------------------
# The Trend Core's maths
# ---------------------------------------------------------------------------

def test_not_ready_before_768_candles_and_long_only():
    tc = fed(path(WARMUP - 1, drift=0.003))
    assert not tc.ready and tc.score() == 0 and tc.target_usd(1000) == 0
    up = fed(path(900, drift=0.003))
    assert up.ready and 0 < up.score() <= 20 and 0 < up.target_usd(1000) <= 950
    down = fed(path(900, drift=-0.003))
    assert down.score() == 0 and down.target_usd(1000) == 0


def test_pinned_values_match_the_backtested_maths():
    """Regression pin: computed with the backtest's TrendCore (backtest/src/v4.py)
    on the same synthetic path; any change to the maths breaks this."""
    tc = fed(path(1000, drift=0.0001, wiggle=0.006))
    assert tc.score() == pytest.approx(9.332802509870792, rel=1e-12)
    assert tc.target_usd(1000) == pytest.approx(830.9759080303019, rel=1e-12)
    expected = 1000 * tc.score() / 10 * 0.25 / tc.annual_vol()
    assert tc.target_usd(1000) == pytest.approx(min(expected, 950))
    assert tc.buffer_usd(1000) == pytest.approx(0.1 * 1000 * 0.25 / tc.annual_vol())
    assert all(-0.5 <= v <= 0.5 for v in tc.raw[4:])


def test_save_and_restore_continue_identically():
    candles = path(1100, drift=0.001, wiggle=0.008)
    a = fed(candles[:900])
    b = TrendCore.from_dict(json.loads(json.dumps(a.to_dict())))
    for c in candles[900:]:
        a.update(c.high, c.low, c.close, c.timestamp)
        b.update(c.high, c.low, c.close, c.timestamp)
    assert b.score() == a.score() and b.target_usd(500) == a.target_usd(500) and b.n == a.n


# ---------------------------------------------------------------------------
# Rebalancing in the live orchestrator
# ---------------------------------------------------------------------------

class FakeSheet:
    def __init__(self, rows=None, fail_writes=False):
        self.rows, self.live_trades, self.fail_writes = list(rows or []), [], fail_writes

    def get_all_proposals(self):
        return [dataclasses.replace(p) for p in self.rows]

    def write_new_proposal(self, p):
        if self.fail_writes:
            raise RuntimeError("Sheets quota exceeded")
        self.rows.append(dataclasses.replace(p))

    def update_status_by_id(self, proposal_id, new_status, **fields):
        for r in self.rows:
            if r.proposal_id == proposal_id:
                r.status = new_status
                r.error = fields.get("Error", r.error)
                return
        raise ValueError(proposal_id)

    def append_live_trade(self, p):
        self.live_trades.append(dataclasses.replace(p))


class StubCore:
    """A Trend Core with a chosen target."""

    def __init__(self, target, buffer=5.0, ready=True):
        self.t, self.b, self.ready, self.n = target, buffer, ready, 900 if ready else 500

    def update(self, *a, **k):
        pass

    def score(self):
        return 7.5

    def target_usd(self, equity):
        return self.t

    def buffer_usd(self, equity):
        return self.b


class FakeChain:
    def __init__(self, price=80_000.0, error=None):
        self.price, self.error, self.buys, self.sells = price, error, [], []

    def buy(self, network, pk, wallet, size_usd, price_at_proposal):
        self.buys.append(size_usd)
        if self.error:
            raise self.error
        q = size_usd / self.price
        return LiveFillResult(self.price, q, size_usd, 0.0, 0.0, f"0xb{len(self.buys)}", 0, 0.0, 0)

    def sell(self, network, pk, wallet, wbtc_qty, price_at_proposal):
        self.sells.append(wbtc_qty)
        if self.error:
            raise self.error
        q = math.floor(wbtc_qty * 1e8) / 1e8
        return LiveFillResult(self.price, q, q * self.price, 0.0, 0.0, f"0xs{len(self.sells)}", 0, 0.0, 0)


@pytest.fixture
def setup(monkeypatch):
    def make(target, usdc=200.0, held_qty=0.0, enabled=True, error=None, fail_writes=False, buffer=5.0):
        chain = FakeChain(error=error)
        monkeypatch.setattr(live_orchestrator, "execute_buy", chain.buy)
        monkeypatch.setattr(live_orchestrator, "execute_sell", chain.sell)
        sheet = FakeSheet(fail_writes=fail_writes)
        orch = live_orchestrator.LiveOrchestrator(WALLET, "", MAINNET, sheet, auto_execute=True, trend_core=enabled)
        orch.tc = StubCore(target, buffer)
        orch.tc_wbtc_qty, orch.tc_cost_usd = held_qty, held_qty * 75_000.0
        balance = [usdc]

        def refresh(self):
            self._cached_usdc_balance = balance[0]
            return balance[0]
        monkeypatch.setattr(live_orchestrator.LiveVault, "refresh_balance", refresh)
        orch.vault.refresh_balance()
        return orch, sheet, chain
    return make


def candle(close=80_000.0):
    return Candle(timestamp=T0, open=close, high=close, low=close, close=close)


def test_buys_the_gap_and_records_an_executed_tc_row(setup):
    orch, sheet, chain = setup(target=60.0)
    actions = []
    orch._rebalance_trend_core(candle(), actions)
    assert chain.buys == [pytest.approx(60.0)]
    assert orch.tc_wbtc_qty == pytest.approx(60.0 / 80_000)
    row = sheet.rows[-1]
    assert row.engine == "TC" and row.side == "BUY" and row.status == STATUS_EXECUTED and row.tx_hash == "0xb1"
    assert sheet.live_trades and "TC BUY executed" in actions[-1]


def test_buy_never_touches_the_50_dollar_reserve(setup):
    orch, sheet, chain = setup(target=500.0, usdc=120.0)
    orch._rebalance_trend_core(candle(), [])
    assert chain.buys == [pytest.approx(70.0)]


def test_no_trade_inside_the_buffer_or_under_10_dollars(setup):
    orch, sheet, chain = setup(target=8.0)
    orch._rebalance_trend_core(candle(), a := [])
    orch2, _, chain2 = setup(target=0.0005 * 80_000 + 4.0, held_qty=0.0005, buffer=5.0)
    orch2._rebalance_trend_core(candle(), b := [])
    assert not chain.buys and not chain2.buys and not chain2.sells
    assert "no trade" in a[-1] and "no trade" in b[-1]


def test_score_zero_sells_everything_and_books_pnl(setup):
    orch, sheet, chain = setup(target=0.0, held_qty=0.00075)
    orch._rebalance_trend_core(candle(), actions := [])
    assert chain.sells and orch.tc_wbtc_qty == 0 and orch.tc_cost_usd == 0
    assert sheet.rows[-1].side == "SELL" and sheet.rows[-1].status == STATUS_EXECUTED
    assert "realised P&L $3.75" in actions[-1]          # 0.00075 x (80,000 - 75,000)


def test_partial_sell_keeps_the_proportional_cost(setup):
    orch, sheet, chain = setup(target=40.0, held_qty=0.001)       # holds $80, wants $40
    orch._rebalance_trend_core(candle(), [])
    assert orch.tc_wbtc_qty == pytest.approx(0.0005, abs=1e-8)
    assert orch.tc_cost_usd == pytest.approx(37.5, abs=0.01)


def test_twap_spike_skips_the_trade_and_keeps_the_holding(setup):
    orch, sheet, chain = setup(target=60.0, error=TwapDeviationError("spot 3% from the 30-minute average"))
    orch._rebalance_trend_core(candle(), actions := [])
    assert orch.tc_wbtc_qty == 0
    assert sheet.rows[-1].status == STATUS_EXPIRED_STALE and "30-minute average" in sheet.rows[-1].error
    assert "skipped" in actions[-1] and not sheet.live_trades


def test_other_errors_are_recorded_as_failed(setup):
    orch, sheet, chain = setup(target=60.0, error=RuntimeError("rpc down"))
    orch._rebalance_trend_core(candle(), [])
    assert sheet.rows[-1].status == STATUS_FAILED and orch.tc_wbtc_qty == 0


def test_a_sheet_error_never_loses_a_real_trade(setup):
    orch, sheet, chain = setup(target=60.0, fail_writes=True)
    orch._rebalance_trend_core(candle(), [])
    assert chain.buys and orch.tc_wbtc_qty == pytest.approx(60.0 / 80_000)


def test_not_ready_means_no_trade(setup):
    orch, sheet, chain = setup(target=60.0)
    orch.tc = StubCore(60.0, ready=False)
    orch._rebalance_trend_core(candle(), actions := [])
    assert not chain.buys and "warming up" in actions[-1]


def test_switched_off_sells_the_remaining_holding(setup):
    orch, sheet, chain = setup(target=999.0, held_qty=0.0002, enabled=False)
    orch._rebalance_trend_core(candle(), [])
    assert chain.sells and orch.tc_wbtc_qty == 0 and sheet.rows[-1].reason == "ROLLBACK"


def test_hand_approved_tc_row_is_refused(setup):
    orch, sheet, chain = setup(target=0.0)
    sheet.rows.append(Proposal(proposal_id="x1", created_at=T0, engine="TC", side="BUY", size_usd=50.0,
                               position_id="TC", reason="REBALANCE", price_at_proposal=80_000.0,
                               status=STATUS_APPROVED))
    actions = []
    orch._process_actionable_proposals(candle(), actions, sheet.get_all_proposals())
    assert sheet.rows[0].status == STATUS_FAILED and not chain.buys


def test_trend_bot_takes_no_entries_while_the_trend_core_is_on(setup, monkeypatch):
    orch, sheet, chain = setup(target=0.0)
    calls = []
    monkeypatch.setattr(orch.tb, "check_entry", lambda *a, **k: calls.append(1))
    for c in path(5, start=80_000):
        orch.process_candle(c)
    assert not calls
    orch_off, _, _ = setup(target=0.0, enabled=False)
    monkeypatch.setattr(orch_off.tb, "check_entry", lambda *a, **k: calls.append(1))
    orch_off.process_candle(path(1, start=80_000)[0])
    assert calls


def test_account_value_includes_the_trend_core_holding(setup):
    orch, sheet, chain = setup(target=0.0, held_qty=0.001, usdc=100.0)
    tb_val, rh_val = orch._position_value(80_000.0)
    assert tb_val == pytest.approx(80.0) and rh_val == 0


def test_state_round_trip_and_old_state_without_tc(setup):
    orch, sheet, chain = setup(target=0.0, held_qty=0.0012)
    orch.tc = fed(path(800, drift=0.001))
    for c in path(3, t0=T0 + 800 * H4):
        orch.candle_history.append(c)
    state = json.loads(json.dumps(serialize_live_state(orch)))
    back = restore_live_state(state, WALLET, "", MAINNET, sheet, auto_execute=True, trend_core=True)
    assert back.tc_wbtc_qty == 0.0012 and back.tc.n == orch.tc.n and back.tc.score() == orch.tc.score()
    del state["tc"]
    old = restore_live_state(state, WALLET, "", MAINNET, sheet, auto_execute=True, trend_core=True)
    assert old.tc_wbtc_qty == 0 and not old.tc.ready


# ---------------------------------------------------------------------------
# The TWAP guard
# ---------------------------------------------------------------------------

def test_check_twap_allows_within_1pct_and_blocks_beyond():
    assert check_twap("0xpool", 80_500, twap_reader=lambda a, s: 80_000) == 80_000
    with pytest.raises(TwapDeviationError):
        check_twap("0xpool", 81_000, twap_reader=lambda a, s: 80_000)
    with pytest.raises(TwapDeviationError):
        check_twap("0xpool", 110.0, twap_reader=lambda a, s: 110_800)      # the 2025-10-20 wick


def test_check_twap_fails_closed_when_the_average_cant_be_read():
    def broken(a, s):
        raise RuntimeError("rpc timeout")
    with pytest.raises(TwapDeviationError, match="refusing to trade"):
        check_twap("0xpool", 80_000, twap_reader=broken)


def test_buy_is_refused_before_signing_on_a_spike(monkeypatch):
    monkeypatch.setattr(live_execution, "get_live_price", lambda pool: {"price_usdc_per_wbtc": 82_000.0})
    monkeypatch.setattr(live_execution, "get_twap_price", lambda pool, s: 80_000.0)

    def must_not_sign(*a, **k):
        raise AssertionError("nothing may be signed when the TWAP check fails")
    monkeypatch.setattr(live_execution, "build_and_sign_transaction", must_not_sign)
    with pytest.raises(TwapDeviationError):
        live_execution.execute_buy(MAINNET, "", WALLET, 50.0, 82_000.0)
    with pytest.raises(TwapDeviationError):
        live_execution.execute_sell(MAINNET, "", WALLET, 0.0005, 82_000.0)


def test_decode_observe_reads_the_average_tick():
    def word(v):
        return v.to_bytes(32, "big", signed=True)
    cum_then, cum_now = -1_000_000, -1_000_000 + 66_296 * 1800
    data = b"".join([word(64), word(160), word(2), word(cum_then), word(cum_now), word(2), word(1), word(2)])
    tick = decode_observe_twap_tick(data, 1800)
    assert tick == pytest.approx(66_296)
    assert tick_to_price(tick) == pytest.approx(1.0001 ** 66_296 * 100)


# ---------------------------------------------------------------------------
# Warm-up history
# ---------------------------------------------------------------------------

def test_history_must_reach_the_last_window_and_be_continuous():
    cs = path(10)
    run = _continuous_until(cs[:3] + cs[5:], cs[-1].timestamp)
    assert [c.timestamp for c in run] == [c.timestamp for c in cs[5:]]
    with pytest.raises(HistoryError):
        _continuous_until(cs[:5], cs[-1].timestamp)


def test_warm_up_falls_back_to_the_next_source():
    cs = path(800, drift=0.001)

    def broken(last_ts):
        raise RuntimeError("451 unavailable from this region")
    tc, src = warm_trend_core(cs[-1].timestamp, [("Binance", broken), ("Kraken", lambda t: cs)])
    assert src == "Kraken" and tc.ready and tc.n == 800
    with pytest.raises(HistoryError):
        warm_trend_core(cs[-1].timestamp, [("Binance", broken)])
