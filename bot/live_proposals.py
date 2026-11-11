"""
Trade proposals: the record of every signal the strategy produces and what
happened to it. In this shareable version they are stored in local files
(bot/local_store.py) instead of a Google Sheet, and the bot runs in fully
automatic mode, so each proposal is written as APPROVED and executed in the
same cycle. The lifecycle below is kept because the strategy code uses it: A signal from the (unmodified)
Trend Bot / Range Harvester decision code becomes a row here with
status=PENDING; nothing executes for real until a human edits that cell to
APPROVED; a separate pass in the executor picks up approved rows and only
then builds a real transaction.

Row lifecycle: PENDING -> (human edits the Status cell) -> APPROVED or
REJECTED -> (next executor cycle) -> EXECUTED / FAILED / EXPIRED_STALE.
A row still PENDING or APPROVED PROPOSAL_MAX_AGE_HOURS after it was written
is marked EXPIRED_STALE too, so it can't block its engine or position forever.
REJECTED rows get marked REJECTED_PROCESSED once the executor has reacted
to them (clearing any internal "don't re-propose this" tracking) so they
aren't picked up twice.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_REJECTED_PROCESSED = "REJECTED_PROCESSED"
STATUS_EXECUTED = "EXECUTED"
STATUS_FAILED = "FAILED"
STATUS_EXPIRED_STALE = "EXPIRED_STALE"


LIVE_CYCLE_LOG_HEADERS = [
    "Timestamp (UTC)", "Candle #", "Regime", "Close ($)", "Vault USDC ($)",
    "TB Value ($)", "TB #Pos", "RH Value ($)", "RH In Pos?", "Combined Equity ($)", "Notes",
    # Added with the optional Savings Vault, after "Notes" so older rows keep
    # their columns. "Vault USDC" is the wallet's cash; Combined Equity
    # includes the Savings Vault.
    "Savings Vault ($)", "Savings Yield ($)",
]
SAVINGS_LOG_HEADERS = ["Timestamp (UTC)", "Cycle #", "Mode", "Action", "Amount ($)", "Status", "Tx Hash",
                       "Vault Balance ($)", "Notes"]

PENDING_TRADES_HEADERS = [
    "Proposal ID", "Created At (UTC)", "Engine", "Side", "Size (USD)", "Position ID",
    "Reason", "Price at Proposal", "Notes", "Status", "Tx Hash",
    "Actual Fill Price", "Actual Amount", "Executed At (UTC)", "Error", "Meta (JSON)",
]
LIVE_TRADES_HEADERS = [
    "Proposal ID", "Engine", "Side", "Executed At (UTC)", "Tx Hash", "Fill Price",
    "Amount", "Reason", "Position ID",
]


@dataclass
class Proposal:
    proposal_id: str
    created_at: int
    engine: str            # "TB" | "RH"
    side: str              # "BUY" | "SELL"
    size_usd: float        # meaningful for BUY
    position_id: Optional[str]   # meaningful for SELL -- which open position this closes
    reason: str            # exit_reason for SELL, "ENTRY" for BUY
    price_at_proposal: float
    notes: str = ""
    status: str = STATUS_PENDING
    tx_hash: Optional[str] = None
    actual_fill_price: Optional[float] = None
    actual_amount: Optional[float] = None
    executed_at: Optional[int] = None
    error: Optional[str] = None
    meta: dict = field(default_factory=dict)   # carries TB/RH-specific fill metadata through to execution


def new_proposal_id() -> str:
    return uuid.uuid4().hex[:12]


def _ts(unix_seconds: Optional[int]) -> str:
    if not unix_seconds:
        return ""
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def proposal_to_row(p: Proposal) -> list:
    return [
        p.proposal_id, _ts(p.created_at), p.engine, p.side,
        round(p.size_usd, 2) if p.size_usd else "", p.position_id or "",
        p.reason, round(p.price_at_proposal, 2), p.notes, p.status,
        p.tx_hash or "", round(p.actual_fill_price, 2) if p.actual_fill_price else "",
        round(p.actual_amount, 8) if p.actual_amount else "", _ts(p.executed_at), p.error or "",
        json.dumps(p.meta) if p.meta else "",
    ]


_ISO_TS = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")
_US_TS = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d{2})(?::(\d{2}))?$")
_SHEETS_EPOCH_DAYS = 25569   # Sheets serial day number of 1970-01-01


def parse_sheet_timestamp(value) -> Optional[int]:
    """Read a 'Created At (UTC)' value back to unix seconds, or None if it
    can't be read (accepts "YYYY-MM-DD HH:MM:SS", the US M/D/YYYY form, unix
    seconds, or a spreadsheet serial day number, in case a file was edited in
    a spreadsheet program). Day-first dates are deliberately not guessed."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if value > 1e9:   # already unix seconds
            return int(value)
        return int(round((value - _SHEETS_EPOCH_DAYS) * 86400))
    text = str(value).strip()
    m = _ISO_TS.match(text)
    if m:
        y, mo, d, h, mi, s = m.groups()
    else:
        m = _US_TS.match(text)
        if not m:
            return None
        mo, d, y, h, mi, s = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def row_to_proposal(row: dict) -> Proposal:
    """`row` is a dict keyed by header name (one row of the proposals file)."""
    def _float_or_none(v):
        return float(v) if v not in (None, "", "None") else None

    meta_raw = row.get("Meta (JSON)", "")
    meta = json.loads(meta_raw) if meta_raw else {}

    return Proposal(
        proposal_id=row["Proposal ID"],
        created_at=parse_sheet_timestamp(row.get("Created At (UTC)")) or 0,   # 0 = unreadable
        engine=row["Engine"], side=row["Side"],
        size_usd=_float_or_none(row["Size (USD)"]) or 0.0,
        position_id=row["Position ID"] or None,
        reason=row["Reason"], price_at_proposal=_float_or_none(row["Price at Proposal"]) or 0.0,
        notes=row.get("Notes", ""), status=row["Status"],
        tx_hash=row.get("Tx Hash") or None,
        actual_fill_price=_float_or_none(row.get("Actual Fill Price")),
        actual_amount=_float_or_none(row.get("Actual Amount")),
        executed_at=None, error=row.get("Error") or None,
        meta=meta,
    )


def filter_actionable(all_proposals: list[Proposal]) -> list[Proposal]:
    """Rows a human has actually decided on (APPROVED or REJECTED) that the
    executor hasn't reacted to yet. Pure function -- operates on an
    already-fetched list, so callers making multiple checks per cycle (see
    LiveOrchestrator) can fetch once and reuse it, rather than re-reading
    the whole store for each check. In the original Google Sheets version a
    full read was an API call with its own rate limit -- calling it several times
    per cycle is how this crashed during bootstrap (hundreds of candles
    replayed in a tight loop, each doing 2-3 full reads, blew straight
    through the per-minute quota)."""
    return [p for p in all_proposals if p.status in (STATUS_APPROVED, STATUS_REJECTED)]


def has_open_entry_in_list(all_proposals: list[Proposal], engine: str) -> bool:
    """Same reasoning as filter_actionable -- pure function over an
    already-fetched list."""
    unresolved = (STATUS_PENDING, STATUS_APPROVED)
    return any(p.engine == engine and p.side == "BUY" and p.status in unresolved for p in all_proposals)


def find_expired(all_proposals: list[Proposal], now_ts: int, max_age_seconds: int) -> tuple[list, list]:
    """Split unresolved (PENDING/APPROVED) proposals into those at least
    max_age_seconds old and those whose Created At couldn't be read (left
    alone, but worth reporting). Pure function over an already-fetched list."""
    unresolved = [p for p in all_proposals if p.status in (STATUS_PENDING, STATUS_APPROVED)]
    expired = [p for p in unresolved if p.created_at and now_ts - p.created_at >= max_age_seconds]
    unreadable = [p for p in unresolved if not p.created_at]
    return expired, unreadable


def has_open_exit_in_list(all_proposals: list[Proposal], position_id: str) -> bool:
    """True if a not-yet-resolved SELL proposal exists for this position.
    Same reasoning as filter_actionable -- pure function over an
    already-fetched list."""
    unresolved = (STATUS_PENDING, STATUS_APPROVED)
    return any(p.side == "SELL" and p.position_id == position_id and p.status in unresolved
               for p in all_proposals)


class ProposalStore(Protocol):
    """Where proposals, trades, the cycle log, the summary and the bot's
    memory are kept. bot/local_store.py implements it with local files."""

    def get_all_proposals(self) -> list[Proposal]: ...
    def write_new_proposal(self, p: Proposal) -> None: ...
    def update_status_by_id(self, proposal_id: str, new_status: str, **fields) -> None: ...
    def append_live_trade(self, p: Proposal) -> None: ...
