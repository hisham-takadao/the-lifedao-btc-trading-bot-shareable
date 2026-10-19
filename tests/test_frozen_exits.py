"""Regression tests for P1 / bug 12: TB positions frozen with
pending_exit=True by a sell that failed under pre-2026-09-24 code.

The state here is shaped exactly as the old code saved it to Live State
(the two real positions from 2026-09-22), and is restored through
restore_live_state, the same path live_main.py uses after a restart. The
earlier fix for bug 11 was only tested against failures happening after
it was deployed, which is how this case slipped through.
"""

import dataclasses
import math
from datetime import datetime, timezone

import pytest

from bot import live_orchestrator
from bot.live_config import MAINNET
from bot.live_execution import ExecutionRevertedError, LiveFillResult
from bot.live_proposals import (
    Proposal, has_open_exit_in_list,
    STATUS_APPROVED, STATUS_EXECUTED, STATUS_FAILED, STATUS_PENDING,
)
from bot.live_state_store import restore_live_state, serialize_live_state
from bot.indicators import Candle

WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"   # public Hardhat test address, holds no funds
FOUR_HOURS = 4 * 3600
STATE_CANDLE_INDEX = 861
STATE_TS = int(datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc).timestamp())
NEXT_CLOSE = 82_814.12

POS_A = "e4cb7b58907f"
POS_B = "6e08b4e67d0e"
QTY = {POS_A: 0.00058676, POS_B: 0.00035491}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeProposalSheet:
    """In-memory stand-in for ProposalSheet (only what the orchestrator uses)."""

    def __init__(self, proposals=None):
        self.rows = list(proposals or [])
        self.live_trades = []

    def get_all_proposals(self):
        # A fresh copy each read, like re-reading the real sheet.
        return [dataclasses.replace(p) for p in self.rows]

    def write_new_proposal(self, p):
        self.rows.append(dataclasses.replace(p))

    def update_status_by_id(self, proposal_id, new_status, **fields):
        for row in self.rows:
            if row.proposal_id == proposal_id:
                row.status = new_status
                if "Error" in fields:
                    row.error = fields["Error"]
                return
        raise ValueError(f"Proposal {proposal_id} not found")

    def append_live_trade(self, p):
        self.live_trades.append(dataclasses.replace(p))

    def by_id(self, proposal_id):
        return next(p for p in self.rows if p.proposal_id == proposal_id)

    def sells_for(self, position_id):
        return [p for p in self.rows if p.side == "SELL" and p.position_id == position_id]


class FakeChain:
    """Records sells instead of sending them. `fail_next` makes the next
    N sells raise the same on-chain revert the 2026-09-23 sells hit."""

    def __init__(self):
        self.sells = []
        self.buys = []
        self.fail_next = 0

    def execute_sell(self, network, private_key, wallet_address, wbtc_qty, price_at_proposal):
        self.sells.append(wbtc_qty)
        if self.fail_next:
            self.fail_next -= 1
            raise ExecutionRevertedError("execution reverted: STF", "0xdead")
        return LiveFillResult(fill_price=NEXT_CLOSE, wbtc_qty=wbtc_qty, usd_value=wbtc_qty * NEXT_CLOSE,
                              slippage_pct=0.0, fees_usd=0.0, tx_hash=f"0xsell{len(self.sells)}",
                              gas_used=0, gas_cost_eth=0.0, block_number=0)

    def execute_buy(self, *args, **kwargs):
        self.buys.append(args)
        raise AssertionError("no BUY expected in these tests")


@pytest.fixture
def chain(monkeypatch):
    fake = FakeChain()
    monkeypatch.setattr(live_orchestrator, "execute_sell", fake.execute_sell)
    monkeypatch.setattr(live_orchestrator, "execute_buy", fake.execute_buy)

    def fake_refresh(self):
        self._cached_usdc_balance = 120.445033
        return self._cached_usdc_balance
    monkeypatch.setattr(live_orchestrator.LiveVault, "refresh_balance", fake_refresh)
    return fake


# ---------------------------------------------------------------------------
# State as saved by the old code
# ---------------------------------------------------------------------------

def _candle_history(n=420):
    """Deterministic, gently oscillating 4h candles ending at STATE_TS."""
    candles = []
    for i in range(n):
        ts = STATE_TS - (n - 1 - i) * FOUR_HOURS
        close = 85_000 + 1_500 * math.sin(i / 9) + 400 * math.sin(i / 2.3)
        open_ = 85_000 + 1_500 * math.sin((i - 1) / 9) + 400 * math.sin((i - 1) / 2.3)
        candles.append({"timestamp": ts, "open": open_, "high": max(open_, close) * 1.002,
                        "low": min(open_, close) * 0.998, "close": close, "volume": 0.0})
    return candles


def _frozen_tb_position(position_id, qty, price, size, fill_index, ts):
    """Every TBPosition field, as dataclasses.asdict wrote it to Live State."""
    return {
        "entry_signal_candle_index": fill_index - 1, "entry_fill_candle_index": fill_index,
        "entry_fill_price": price, "wbtc_qty": qty, "size_usd": size,
        "adx_at_entry": 43.0, "rsi_at_entry": 60.0, "adx_tier": 2,
        "entry_slippage_pct": 0.0, "entry_fees_usd": 0.0, "entry_timestamp": ts,
        "peak_close": price, "trailing_armed": False,
        "pending_exit": True,          # left set by the failed 2026-09-23 sell
        "position_id": position_id,
    }


def old_saved_state():
    return {
        "version": 1,
        "candle_index": STATE_CANDLE_INDEX,
        "current_day": "2026-10-02",
        "day_open_equity": 200.21,
        "tb_positions": [
            _frozen_tb_position(POS_A, QTY[POS_A], 85_639.87, 50.25, 801,
                                int(datetime(2026, 9, 22, 0, tzinfo=timezone.utc).timestamp())),
            _frozen_tb_position(POS_B, QTY[POS_B], 85_388.18, 30.31, 802,
                                int(datetime(2026, 9, 22, 4, tzinfo=timezone.utc).timestamp())),
        ],
        "tb_circuit_breaker_active": False,
        "tb_realized_losses_today": 0.0,
        "tb_cooldown_until_candle_index": None,
        "rh_position": None,
        "rh_circuit_breaker_active": False,
        "rh_realized_losses_today": 0.0,
        "rh_cooldown_until_candle_index": None,
        "rh_violator_history": [],
        "candle_history": _candle_history(),
    }


def _proposal(pid, side, status, engine="TB", position_id=None, reason="MAX_HOLD"):
    return Proposal(proposal_id=pid, created_at=0, engine=engine, side=side,
                    size_usd=130.65 if side == "BUY" else 0.0, position_id=position_id,
                    reason="ENTRY" if side == "BUY" else reason, price_at_proposal=85_000.0, status=status)


def sheet_as_today():
    """Pending Trades as it stands: both 2026-09-23 sells FAILED, plus the
    stale manual-mode RH BUY (P2) still PENDING."""
    return FakeProposalSheet([
        _proposal("9417141fb7c5", "BUY", STATUS_PENDING, engine="RH"),
        _proposal("35f8b760db31", "SELL", STATUS_FAILED, position_id=POS_A),
        _proposal("ca125f4a5060", "SELL", STATUS_FAILED, position_id=POS_B),
    ])


def next_candle(k=1):
    ts = STATE_TS + k * FOUR_HOURS
    return Candle(timestamp=ts, open=83_000.0, high=83_200.0, low=82_600.0, close=NEXT_CLOSE, volume=0.0)


def restore(sheet, auto_execute=True):
    return restore_live_state(old_saved_state(), WALLET, "unused-in-tests", MAINNET, sheet,
                              auto_execute=auto_execute)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_restored_frozen_positions_are_sold_on_first_cycle(chain):
    sheet = sheet_as_today()
    orch = restore(sheet)
    assert all(p.pending_exit for p in orch.tb.positions)   # the frozen state really was restored

    rec = orch.process_candle(next_candle())

    assert sorted(chain.sells) == sorted(QTY.values())
    assert chain.buys == []
    assert orch.tb.positions == []
    for pid in (POS_A, POS_B):
        new_sells = [p for p in sheet.sells_for(pid) if p.status != STATUS_FAILED]
        assert len(new_sells) == 1
        assert new_sells[0].status == STATUS_EXECUTED
    assert {t.position_id for t in sheet.live_trades} == {POS_A, POS_B}
    assert sum("released for exit checks" in a for a in rec.actions) == 2
    assert serialize_live_state(orch)["tb_positions"] == []


def test_open_pending_sell_keeps_position_waiting(chain):
    """Manual mode: a SELL still awaiting a human decision must not be
    duplicated. The other position, with no open SELL, is released."""
    sheet = sheet_as_today()
    sheet.rows.append(_proposal("aaaaaaaaaaaa", "SELL", STATUS_PENDING, position_id=POS_A))
    orch = restore(sheet, auto_execute=False)

    orch.process_candle(next_candle())

    pos = {p.position_id: p for p in orch.tb.positions}
    assert pos[POS_A].pending_exit is True
    assert [p.proposal_id for p in sheet.sells_for(POS_A) if p.status == STATUS_PENDING] == ["aaaaaaaaaaaa"]
    assert pos[POS_B].pending_exit is True               # released, then re-flagged by a new exit signal
    assert len([p for p in sheet.sells_for(POS_B) if p.status == STATUS_PENDING]) == 1
    assert chain.sells == []


def test_approved_sell_in_sheet_is_executed_once(chain):
    sheet = sheet_as_today()
    sheet.rows.append(_proposal("bbbbbbbbbbbb", "SELL", STATUS_APPROVED, position_id=POS_A))
    orch = restore(sheet)

    orch.process_candle(next_candle())

    assert sorted(chain.sells) == sorted(QTY.values())   # one sell per position, no duplicate for A
    assert sheet.by_id("bbbbbbbbbbbb").status == STATUS_EXECUTED
    assert len([p for p in sheet.sells_for(POS_A) if p.status == STATUS_EXECUTED]) == 1
    assert orch.tb.positions == []


def test_sell_that_fails_again_is_retried_next_cycle(chain):
    sheet = sheet_as_today()
    orch = restore(sheet)
    chain.fail_next = 2

    orch.process_candle(next_candle(1))
    assert len(orch.tb.positions) == 2
    assert not any(p.pending_exit for p in orch.tb.positions)

    # Restart between cycles, as a redeploy would.
    orch = restore_live_state(serialize_live_state(orch), WALLET, "unused-in-tests", MAINNET, sheet,
                              auto_execute=True)
    orch.process_candle(next_candle(2))
    assert orch.tb.positions == []
    assert len(chain.sells) == 4


def test_has_open_exit_in_list():
    proposals = [
        _proposal("1", "SELL", STATUS_FAILED, position_id=POS_A),
        _proposal("2", "SELL", STATUS_EXECUTED, position_id=POS_B),
        _proposal("3", "BUY", STATUS_PENDING, engine="RH"),
    ]
    assert not has_open_exit_in_list(proposals, POS_A)
    assert not has_open_exit_in_list(proposals, POS_B)
    proposals.append(_proposal("4", "SELL", STATUS_PENDING, position_id=POS_A))
    proposals.append(_proposal("5", "SELL", STATUS_APPROVED, position_id=POS_B))
    assert has_open_exit_in_list(proposals, POS_A)
    assert has_open_exit_in_list(proposals, POS_B)
