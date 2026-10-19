"""Tests for P2: unresolved proposals expire after PROPOSAL_MAX_AGE_HOURS
instead of blocking an engine (BUY) or a position (SELL) forever.
Offline: fake sheet and faked chain calls, as in test_frozen_exits."""

from datetime import datetime, timezone

import pytest

from bot.live_config import MAINNET, PROPOSAL_MAX_AGE_HOURS
from bot.live_proposals import (
    PENDING_TRADES_HEADERS, Proposal, parse_sheet_timestamp, proposal_to_row, row_to_proposal,
    STATUS_APPROVED, STATUS_EXPIRED_STALE, STATUS_PENDING,
)
from bot.live_state_store import restore_live_state

from test_frozen_exits import (   # shared fakes and the old saved state
    FakeProposalSheet, POS_A, WALLET, chain, next_candle, old_saved_state, sheet_as_today,
)

HOUR = 3600


def utc(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Reading Created At back from the sheet
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cell", [
    "2026-07-28 04:00:00",     # as written
    "2026-07-28 4:00:00",      # Sheets' default display of a parsed date-time
    "2026-07-28 04:00",
    "7/28/2026 4:00:00",       # US-locale display
    46231.166666666664,        # serial day number (unformatted read)
    utc(2026, 7, 28, 4),       # unix seconds
])
def test_parse_sheet_timestamp_formats(cell):
    assert parse_sheet_timestamp(cell) == utc(2026, 7, 28, 4)


@pytest.mark.parametrize("cell", ["", None, "yesterday", "28/07/2026 04:00:00", "2026-13-01 00:00:00"])
def test_parse_sheet_timestamp_unreadable(cell):
    assert parse_sheet_timestamp(cell) is None


def _sheet_row(proposal, created_at_cell=None):
    """A proposal as get_all_records() would return its row."""
    row = dict(zip(PENDING_TRADES_HEADERS, proposal_to_row(proposal)))
    if created_at_cell is not None:
        row["Created At (UTC)"] = created_at_cell
    return row


def test_created_at_round_trips_through_the_sheet():
    p = Proposal(proposal_id="abc", created_at=utc(2026, 10, 2, 4), engine="TB", side="BUY", size_usd=40.0,
                 position_id=None, reason="ENTRY", price_at_proposal=82_000.0)
    assert row_to_proposal(_sheet_row(p)).created_at == utc(2026, 10, 2, 4)


# ---------------------------------------------------------------------------
# Expiry inside a cycle
# ---------------------------------------------------------------------------

def from_sheet(pid, side, status, created_at_cell, engine="TB", position_id=None):
    p = Proposal(proposal_id=pid, created_at=0, engine=engine, side=side,
                 size_usd=130.65 if side == "BUY" else 0.0, position_id=position_id,
                 reason="ENTRY" if side == "BUY" else "MAX_HOLD", price_at_proposal=82_800.0, status=status)
    return row_to_proposal(_sheet_row(p, created_at_cell))


def restore_flat(sheet, auto_execute=True):
    """The old saved state, but with no open positions."""
    state = old_saved_state()
    state["tb_positions"] = []
    return restore_live_state(state, WALLET, "unused-in-tests", MAINNET, sheet, auto_execute=auto_execute)


def record_entry_checks(monkeypatch, orch):
    calls = []
    for name in ("tb", "rh"):
        engine = getattr(orch, name)
        monkeypatch.setattr(engine, "check_entry", lambda *a, _n=name, **k: calls.append(_n))
    return calls


def test_july_rh_buy_expires_and_rh_checks_entries_again(chain, monkeypatch):
    """The real P2 case: RH BUY 9417141fb7c5 written 2026-07-28, never answered."""
    sheet = FakeProposalSheet([from_sheet("9417141fb7c5", "BUY", STATUS_PENDING, "2026-07-28 4:00:00", engine="RH")])
    orch = restore_flat(sheet)
    calls = record_entry_checks(monkeypatch, orch)

    rec = orch.process_candle(next_candle())

    row = sheet.by_id("9417141fb7c5")
    assert row.status == STATUS_EXPIRED_STALE
    assert "Expired" in row.error and f"limit {PROPOSAL_MAX_AGE_HOURS}h" in row.error
    assert "rh" in calls and "tb" in calls
    assert any("9417141fb7c5" in a and "expired" in a for a in rec.actions)
    assert chain.buys == [] and chain.sells == []


def test_recent_pending_buy_still_blocks(chain, monkeypatch):
    candle = next_candle()
    sheet = FakeProposalSheet([from_sheet("fresh0000001", "BUY", STATUS_PENDING,
                                          candle.timestamp - 4 * HOUR, engine="RH")])
    orch = restore_flat(sheet, auto_execute=False)
    calls = record_entry_checks(monkeypatch, orch)

    orch.process_candle(candle)

    assert sheet.by_id("fresh0000001").status == STATUS_PENDING
    assert "rh" not in calls and "tb" in calls


@pytest.mark.parametrize("age_hours, expires", [(PROPOSAL_MAX_AGE_HOURS - 4, False), (PROPOSAL_MAX_AGE_HOURS, True)])
def test_expiry_boundary(chain, monkeypatch, age_hours, expires):
    candle = next_candle()
    sheet = FakeProposalSheet([from_sheet("edge00000001", "BUY", STATUS_PENDING,
                                          candle.timestamp - age_hours * HOUR, engine="RH")])
    orch = restore_flat(sheet, auto_execute=False)
    record_entry_checks(monkeypatch, orch)

    orch.process_candle(candle)

    assert (sheet.by_id("edge00000001").status == STATUS_EXPIRED_STALE) is expires


def test_old_approved_buy_is_never_executed_late(chain, monkeypatch):
    candle = next_candle()
    sheet = FakeProposalSheet([from_sheet("late00000001", "BUY", STATUS_APPROVED,
                                          candle.timestamp - 48 * HOUR, engine="TB")])
    orch = restore_flat(sheet)
    record_entry_checks(monkeypatch, orch)

    orch.process_candle(candle)

    assert sheet.by_id("late00000001").status == STATUS_EXPIRED_STALE
    assert chain.buys == []


def test_old_pending_sell_expires_and_position_is_re_evaluated(chain):
    """Manual mode: an unanswered SELL must not leave a position without
    exit checks (the P1 failure, by another route)."""
    candle = next_candle()
    sheet = sheet_as_today()
    sheet.rows = [p for p in sheet.rows if p.side == "SELL"]   # just the two FAILED sells
    sheet.rows.append(from_sheet("oldsell00001", "SELL", STATUS_PENDING,
                                 candle.timestamp - 30 * HOUR, position_id=POS_A))
    state = old_saved_state()
    orch = restore_live_state(state, WALLET, "unused-in-tests", MAINNET, sheet, auto_execute=False)

    orch.process_candle(candle)

    assert sheet.by_id("oldsell00001").status == STATUS_EXPIRED_STALE
    fresh = [p for p in sheet.sells_for(POS_A) if p.status == STATUS_PENDING]
    assert len(fresh) == 1 and fresh[0].proposal_id != "oldsell00001"
    assert chain.sells == []


def test_unreadable_created_at_is_left_alone(chain, monkeypatch):
    sheet = FakeProposalSheet([from_sheet("nodate000001", "BUY", STATUS_PENDING, "", engine="RH")])
    orch = restore_flat(sheet, auto_execute=False)
    calls = record_entry_checks(monkeypatch, orch)

    orch.process_candle(next_candle())

    assert sheet.by_id("nodate000001").status == STATUS_PENDING
    assert "rh" not in calls
