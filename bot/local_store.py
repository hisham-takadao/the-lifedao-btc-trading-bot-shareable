"""
Local-file storage for everything the bot records, in DATA_DIR (default
./data). It replaces the Google Sheet of the original bot.

  state.json          the bot's memory (open positions, cooldowns, recent
                      candles). Don't edit or delete it while you have open
                      positions: the bot would forget them.
  state.json.bak      the previous version, kept in case a write is cut short
  partial_candle.json the 4-hour candle being built, so a restart loses nothing
  proposals.csv       every signal and what happened to it
  trades.csv          every executed swap (with its transaction hash)
  cycle_log.csv       one row per 4-hour cycle: price, regime, balances, equity
                      (the last two columns, "Savings Vault ($)" and "Savings
                      Yield ($)", were added with the optional Savings Vault;
                      rows written before that simply don't have them)
  savings_log.csv     only if the optional Savings Vault is used: every
                      deposit, withdrawal, USDT swap and alert
  summary.json        the latest overview, rewritten each cycle
  bot.log             the bot's log (written by run_bot.py)

The .csv files open in Excel or any spreadsheet program. Files are written
whole to a temporary file and then swapped in, so a crash or power cut never
leaves a half-written file.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .live_proposals import (
    LIVE_CYCLE_LOG_HEADERS, LIVE_TRADES_HEADERS, PENDING_TRADES_HEADERS, SAVINGS_LOG_HEADERS, Proposal,
    proposal_to_row,
    row_to_proposal,
)


def _ts(unix_seconds: Optional[int]) -> str:
    if not unix_seconds:
        return ""
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class LocalStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proposals_path = self.dir / "proposals.csv"
        self.trades_path = self.dir / "trades.csv"
        self.cycle_log_path = self.dir / "cycle_log.csv"
        self.state_path = self.dir / "state.json"
        self.partial_path = self.dir / "partial_candle.json"
        self.summary_path = self.dir / "summary.json"
        self.savings_log_path = self.dir / "savings_log.csv"
        for path, headers in ((self.proposals_path, PENDING_TRADES_HEADERS),
                              (self.trades_path, LIVE_TRADES_HEADERS),
                              (self.cycle_log_path, LIVE_CYCLE_LOG_HEADERS)):
            if not path.exists():
                _atomic_write(path, self._csv_text([headers]))
        self._upgrade_cycle_log_header()

    def _upgrade_cycle_log_header(self) -> None:
        """A cycle_log.csv from an older version has the shorter header: add
        the new column names to its first line once. Existing rows are left
        exactly as they were (they just have no value in the new columns)."""
        text = self.cycle_log_path.read_text(encoding="utf-8")
        first, _, rest = text.partition(chr(10))
        old = next(csv.reader([first.rstrip(chr(13))]), [])
        if old != LIVE_CYCLE_LOG_HEADERS and old == LIVE_CYCLE_LOG_HEADERS[:len(old)]:
            _atomic_write(self.cycle_log_path, self._csv_text([LIVE_CYCLE_LOG_HEADERS]) + rest)

    @staticmethod
    def _csv_text(rows: list[list]) -> str:
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        return buf.getvalue()

    def _append(self, path: Path, row: list) -> None:
        with path.open("a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(row)
            f.flush()
            os.fsync(f.fileno())

    # ---- proposals ---------------------------------------------------
    def _read_proposal_rows(self) -> list[dict]:
        with self.proposals_path.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def get_all_proposals(self) -> list[Proposal]:
        return [row_to_proposal(r) for r in self._read_proposal_rows()]

    def write_new_proposal(self, p: Proposal) -> None:
        self._append(self.proposals_path, proposal_to_row(p))

    def update_status_by_id(self, proposal_id: str, new_status: str, **fields) -> None:
        rows = self._read_proposal_rows()
        for r in rows:
            if r["Proposal ID"] == proposal_id:
                r["Status"] = new_status
                for k, v in fields.items():
                    if k in r:
                        r[k] = "" if v is None else v
                break
        else:
            raise ValueError(f"Proposal {proposal_id} not found in {self.proposals_path.name}")
        _atomic_write(self.proposals_path,
                      self._csv_text([PENDING_TRADES_HEADERS] + [[r.get(h, "") for h in PENDING_TRADES_HEADERS]
                                                                 for r in rows]))

    def append_live_trade(self, p: Proposal) -> None:
        self._append(self.trades_path, [
            p.proposal_id, p.engine, p.side, _ts(p.executed_at), p.tx_hash or "",
            round(p.actual_fill_price, 2) if p.actual_fill_price else "",
            round(p.actual_amount, 8) if p.actual_amount else "", p.reason, p.position_id or ""])

    # ---- cycle log and summary ----------------------------------------
    def append_cycle_log(self, rec) -> None:
        self._append(self.cycle_log_path, [
            _ts(rec.timestamp), rec.candle_index, rec.regime_label, round(rec.close, 2),
            round(rec.vault_usdc, 2), round(rec.tb_position_value, 2), rec.tb_open_positions,
            round(rec.rh_position_value, 2), "Yes" if rec.rh_open_position else "No",
            round(rec.combined_equity, 2), "; ".join(rec.actions) if rec.actions else "",
            round(getattr(rec, "savings_value", 0.0), 2), round(getattr(rec, "savings_yield", 0.0), 4)])

    def append_savings_log(self, rows: list[dict]) -> None:
        """Rows from the optional Savings Vault (bot/savings_vault.py)."""
        if not self.savings_log_path.exists():
            _atomic_write(self.savings_log_path, self._csv_text([SAVINGS_LOG_HEADERS]))
        for r in rows:
            self._append(self.savings_log_path, [r.get(h, "") for h in SAVINGS_LOG_HEADERS])

    def update_summary(self, summary: dict) -> None:
        _atomic_write(self.summary_path, json.dumps(summary, indent=2))

    # ---- memory --------------------------------------------------------
    def save_state(self, state_dict: dict) -> None:
        if self.state_path.exists():
            _atomic_write(self.dir / "state.json.bak", self.state_path.read_text(encoding="utf-8"))
        _atomic_write(self.state_path, json.dumps(state_dict))

    def load_state(self) -> Optional[dict]:
        if not self.state_path.exists():
            return None
        text = self.state_path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else None

    def save_partial_candle(self, builder_dict: dict) -> None:
        _atomic_write(self.partial_path, json.dumps(builder_dict))

    def load_partial_candle(self) -> Optional[dict]:
        if not self.partial_path.exists():
            return None
        text = self.partial_path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else None
