"""
The live orchestrator. Same spirit as the paper bot's engine.py (indicators
-> regime -> both engines, in a strict phase order every cycle), with the
one structural difference that actually matters: where the paper bot
executes a signal immediately against a simulated vault, this writes a
proposal and stops -- nothing real happens until a human approves it, and
a separate phase (checked first, each cycle) picks up whatever's been
approved or rejected since the last cycle and acts on that.

Phase order per cycle:
  0. Process actionable proposals (APPROVED / REJECTED) from the store --
     SELL before BUY, TB before RH, same reasoning as the paper bot's
     Phase A (a completed sell frees capital before a buy needs it).
  1. Refresh the real USDC balance.
  2. UTC day roll (reset circuit-breaker flags, snapshot day-open equity).
  3. Indicators + regime (identical code to the paper bot).
  4. Peaks + violator tracker (identical).
  5. TB / RH check_exits -- any signal becomes a new SELL proposal, not an
     execution.
  6. TB / RH check_entry -- skipped entirely for an engine that already
     has an unresolved BUY proposal outstanding, so the same signal
     doesn't get re-proposed every cycle while a human is still deciding.
     Section 5.4's TB-priority capital reservation is preserved the same
     way as the paper bot: temporarily debit the live vault's cached
     balance by TB's proposed size before sizing RH.
  7. Persist state.

Strategy v4 (2026-11-08): with trend_core=True the Trend Core
(bot/trend_core.py) replaces the Trend Bot's entries. After the Range
Harvester has acted, the Trend Core moves its own BTC holding towards its
target. Its trades are logged as EXECUTED/FAILED/EXPIRED_STALE rows with
engine "TC" (never PENDING/APPROVED: they're never approved by hand). Its
BTC is counted in the "TB" value columns of the cycle log.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import config
from .indicators import Candle, IndicatorEngine
from .regime import RegimeClassifier
from .trend_bot import TrendBotEngine
from .range_harvester import RangeHarvesterEngine
from .portfolio import PendingFill
from .live_config import NetworkConfig, PROPOSAL_MAX_AGE_HOURS
from .live_portfolio import LiveVault
from .live_proposals import (
    Proposal, ProposalStore, new_proposal_id, filter_actionable, has_open_entry_in_list, has_open_exit_in_list,
    find_expired,
    STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_EXECUTED, STATUS_FAILED, STATUS_REJECTED_PROCESSED,
    STATUS_EXPIRED_STALE,
)
from .live_execution import execute_buy, execute_sell, StalePriceError, ExecutionRevertedError
from .trend_core import TrendCore, MIN_TRADE_USD as TC_MIN_TRADE_USD, WARMUP as TC_WARMUP

log = logging.getLogger("lifedao-bot.live_orchestrator")


@dataclass
class LiveCycleRecord:
    """What main.py needs after each cycle, to write the Live Cycle Log row."""
    candle_index: int
    timestamp: int
    close: float
    regime_label: str
    vault_usdc: float
    tb_position_value: float
    rh_position_value: float
    combined_equity: float
    tb_open_positions: int
    rh_open_position: bool
    warmup_done: bool
    actions: list = field(default_factory=list)


class LiveOrchestrator:
    def __init__(self, wallet_address: str, private_key: str, network: NetworkConfig,
                 proposal_sheet: ProposalStore, auto_execute: bool = False, trend_core: bool = False):
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.network = network
        self.proposals = proposal_sheet
        self.auto_execute = auto_execute

        self.indicators = IndicatorEngine()
        self.regime_clf = RegimeClassifier()
        self.tb = TrendBotEngine()
        self.rh = RangeHarvesterEngine()
        self.vault = LiveVault(wallet_address, network)

        self.candle_index = -1
        self.candle_history: deque = deque(maxlen=config.STATE_CANDLE_HISTORY_SIZE)
        self._current_day = None
        self.day_open_equity: Optional[float] = None
        self.warmup_done = False
        self.current_regime = config.REGIME_RANGING

        # Strategy v4: the Trend Core. Its indicator state is fed every candle
        # either way; it trades only when enabled (or, when disabled, to sell
        # off a holding it still has).
        self.trend_core_enabled = trend_core
        self.tc = TrendCore()
        self.tc_wbtc_qty = 0.0       # WBTC bought by the Trend Core and not yet sold
        self.tc_cost_usd = 0.0       # USDC paid for that WBTC (for realised P&L)

    # -----------------------------------------------------------------
    def replay_candle_indicators_only(self, candle: Candle) -> None:
        """Used only when restoring state after a restart -- see
        state_store.py's paper-bot equivalent for the full reasoning.
        Rebuilds indicator/regime/violator-tracker state without touching
        proposals, positions, or the vault."""
        self.candle_index += 1
        snap = self.indicators.update(candle)
        self.current_regime = self.regime_clf.update(snap).regime
        self.rh.update_violator_tracker(self.candle_index, candle.close, snap.bb_lower, snap.rsi)
        self.warmup_done = self.indicators.n_candles_seen >= config.WARMUP_CANDLES
        self.candle_history.append(candle)
        self.tc.update(candle.high, candle.low, candle.close, candle.timestamp)

    # -----------------------------------------------------------------
    def _position_value(self, current_close: float) -> tuple[float, float]:
        """(TB + Trend Core value, RH value). The Trend Core replaces the Trend
        Bot, so its BTC is reported in the TB columns."""
        tb_value = sum(p.wbtc_qty * current_close for p in self.tb.positions) + self.tc_wbtc_qty * current_close
        rh_value = self.rh.position.wbtc_qty * current_close if self.rh.position else 0.0
        return tb_value, rh_value

    def _find_position(self, position_id: str):
        for p in self.tb.positions:
            if p.position_id == position_id:
                return "TB", p
        if self.rh.position is not None and self.rh.position.position_id == position_id:
            return "RH", self.rh.position
        return None, None

    # -----------------------------------------------------------------
    def process_candle(self, candle: Candle) -> LiveCycleRecord:
        self.candle_index += 1
        idx = self.candle_index
        actions: list[str] = []

        # ---- Determine day-roll status and reset circuit-breaker flags
        # BEFORE processing proposals (see bot/engine.py's process_candle
        # for the full reasoning -- same fix applied there): a SELL
        # executing on the first candle of a new UTC day records its P&L
        # via record_realized_pnl() in Phase 0 below, and on_day_roll()
        # unconditionally zeroes realized_losses_today, so resetting first
        # ensures that fill accumulates into the correct, fresh day. ----
        candle_day = datetime.fromtimestamp(candle.timestamp, tz=timezone.utc).date()
        is_new_day = self._current_day is None or candle_day != self._current_day
        if is_new_day:
            self._current_day = candle_day
            self.tb.on_day_roll()
            self.rh.on_day_roll()

        # ---- Phase 0: act on whatever a human has decided since last cycle ----
        # One read of all proposals for this entire cycle --
        # reused below for the entry-duplicate checks too, rather than each
        # check independently re-reading the store (see module docstring
        # update / filter_actionable's docstring for why this matters).
        all_proposals = self.proposals.get_all_proposals()
        self._expire_old_proposals(candle, all_proposals, actions)
        self._process_actionable_proposals(candle, actions, all_proposals)
        self._release_orphaned_exits(all_proposals, actions)
        self.candle_history.append(candle)

        # ---- Refresh real balance ----
        self.vault.refresh_balance()

        # ---- Day-open equity snapshot, taken AFTER Phase 0 so it reflects
        # any execution that happened right at this candle's open ----
        if is_new_day:
            tb_val, rh_val = self._position_value(candle.open)
            self.day_open_equity = self.vault.balance + tb_val + rh_val

        # ---- Indicators + regime ----
        snap = self.indicators.update(candle)
        self.tc.update(candle.high, candle.low, candle.close, candle.timestamp)
        regime_state = self.regime_clf.update(snap)
        regime = regime_state.regime
        self.current_regime = regime
        self.warmup_done = self.indicators.n_candles_seen >= config.WARMUP_CANDLES

        # ---- Peaks + violator tracker ----
        self.tb.update_peaks(candle.close)
        self.rh.update_peaks(candle.close)
        self.rh.update_violator_tracker(idx, candle.close, snap.bb_lower, snap.rsi)

        day_open_equity = self.day_open_equity or 0.0

        # ---- Exits: signal -> new proposal. In auto mode, executed
        # immediately below; otherwise sits as PENDING until approved. ----
        new_proposals_this_cycle: list[Proposal] = []
        tb_exit_fills = self.tb.check_exits(idx, snap, regime, candle.close, day_open_equity)
        rh_exit_fills = self.rh.check_exits(idx, snap, regime, candle.close, day_open_equity)
        for pf in tb_exit_fills + rh_exit_fills:
            p = self._write_exit_proposal(pf, candle)
            new_proposals_this_cycle.append(p)
            actions.append(f"{pf.engine} exit signal ({pf.exit_reason}) -> proposal written"
                            + (", auto-executing" if self.auto_execute else ", awaiting approval"))

        # ---- Entries: signal -> new proposal, skipped if one's already outstanding ----
        # Note: any proposal(s) just written above for exits/entries this
        # same cycle deliberately aren't reflected in `all_proposals` (it
        # was fetched before Phase 0's writes) -- has_open_entry_in_list
        # only needs to know about proposals from *previous* cycles here,
        # since check_entry can only ever produce at most one new proposal
        # per engine per cycle regardless.
        tb_val_now, rh_val_now = self._position_value(candle.close)
        combined_equity_now = self.vault.balance + tb_val_now + rh_val_now

        tb_entry_fill = None
        if not self.trend_core_enabled and not has_open_entry_in_list(all_proposals, "TB"):
            tb_entry_fill = self.tb.check_entry(idx, snap, regime, candle.close, combined_equity_now,
                                                 self.vault, self.warmup_done)

        # Section 5.4: reserve TB's proposed size before sizing RH, same as the paper bot.
        reserved = tb_entry_fill.size_usd if tb_entry_fill else 0.0
        if reserved:
            self.vault._cached_usdc_balance -= reserved
        rh_entry_fill = None
        if not has_open_entry_in_list(all_proposals, "RH"):
            rh_entry_fill = self.rh.check_entry(idx, snap, regime, candle.close, combined_equity_now,
                                                 self.vault, self.warmup_done)
        if reserved:
            self.vault._cached_usdc_balance += reserved

        if tb_entry_fill:
            p = self._write_entry_proposal(tb_entry_fill, candle)
            new_proposals_this_cycle.append(p)
            actions.append(f"TB entry signal (${tb_entry_fill.size_usd:,.2f}) -> proposal written"
                            + (", auto-executing" if self.auto_execute else ", awaiting approval"))
        if rh_entry_fill:
            p = self._write_entry_proposal(rh_entry_fill, candle)
            new_proposals_this_cycle.append(p)
            actions.append(f"RH entry signal (${rh_entry_fill.size_usd:,.2f}) -> proposal written"
                            + (", auto-executing" if self.auto_execute else ", awaiting approval"))

        # ---- Auto-execute mode: run every proposal just written through the
        # exact same execution path a human approval would trigger, within
        # this same cycle. new_proposals_this_cycle is already in the right
        # order (exits were appended before entries, and within exits TB
        # came before RH), matching Section 5.4's SELL-before-BUY,
        # TB-before-RH ordering -- no separate sort needed here. ----
        if self.auto_execute:
            for p in new_proposals_this_cycle:
                if p.side == "BUY":
                    self._handle_approved_buy(p, candle, actions)
                else:
                    self._handle_approved_sell(p, candle, actions)

            # The balance was read before these swaps; re-read it so the cycle
            # log, Live Summary and vault idle-USDC reflect them (P9). Positions
            # are already updated in memory, so a failed read must not abort
            # the cycle: that would skip saving state after a real trade.
            if new_proposals_this_cycle:
                try:
                    self.vault.refresh_balance()
                except Exception as e:  # noqa: BLE001
                    log.warning("Post-trade USDC balance refresh failed; cycle log shows the pre-trade balance: %s", e)
                    actions.append("USDC balance not re-read after trading -- Vault USDC/equity in this row are pre-trade")

        # ---- Strategy v4: the Trend Core acts after the Range Harvester ----
        if self.trend_core_enabled or self.tc_wbtc_qty > 0:
            if self.auto_execute:
                self._rebalance_trend_core(candle, actions)
            else:
                actions.append("Trend Core needs AUTO_EXECUTE=true to trade; it did nothing")

        tb_val_final, rh_val_final = self._position_value(candle.close)
        return LiveCycleRecord(
            candle_index=idx, timestamp=candle.timestamp, close=candle.close,
            regime_label=config.REGIME_LABELS[regime],
            vault_usdc=self.vault.balance, tb_position_value=tb_val_final, rh_position_value=rh_val_final,
            combined_equity=self.vault.balance + tb_val_final + rh_val_final,
            tb_open_positions=len(self.tb.positions) + (1 if self.tc_wbtc_qty > 0 else 0), rh_open_position=self.rh.position is not None,
            warmup_done=self.warmup_done, actions=actions,
        )

    # -----------------------------------------------------------------
    def _write_entry_proposal(self, pending_fill: PendingFill, candle: Candle) -> Proposal:
        p = Proposal(
            proposal_id=new_proposal_id(), created_at=candle.timestamp,
            engine=pending_fill.engine, side="BUY", size_usd=pending_fill.size_usd,
            position_id=None, reason="ENTRY", price_at_proposal=candle.close,
            notes=f"{pending_fill.engine} entry signal at ${candle.close:,.2f}"
                  + (" [AUTO mode]" if self.auto_execute else ""),
            meta={**pending_fill.meta, "adx_tier": pending_fill.adx_tier},
            status=STATUS_APPROVED if self.auto_execute else STATUS_PENDING,
        )
        self.proposals.write_new_proposal(p)
        return p

    def _write_exit_proposal(self, pending_fill: PendingFill, candle: Candle) -> Proposal:
        position = pending_fill.position
        p = Proposal(
            proposal_id=new_proposal_id(), created_at=candle.timestamp,
            engine=pending_fill.engine, side="SELL", size_usd=0.0,
            position_id=position.position_id, reason=pending_fill.exit_reason,
            price_at_proposal=candle.close,
            notes=(f"{pending_fill.engine} exit ({pending_fill.exit_reason}). Position opened at "
                   f"${position.entry_fill_price:,.2f}, {position.wbtc_qty:.8f} WBTC."
                   + (" [AUTO mode]" if self.auto_execute else "")),
            status=STATUS_APPROVED if self.auto_execute else STATUS_PENDING,
        )
        self.proposals.write_new_proposal(p)
        return p

    # -----------------------------------------------------------------
    def _process_actionable_proposals(self, candle: Candle, actions: list[str], all_proposals: list) -> None:
        actionable = filter_actionable(all_proposals)
        order_key = {("TB", "SELL"): 0, ("TB", "BUY"): 1, ("RH", "SELL"): 2, ("RH", "BUY"): 3}
        ordered = sorted(actionable, key=lambda p: order_key.get((p.engine, p.side), 99))

        for proposal in ordered:
            if proposal.engine not in ("TB", "RH"):
                # Trend Core rows are written already resolved; one edited by hand must not trade.
                self.proposals.update_status_by_id(proposal.proposal_id, STATUS_FAILED,
                                                    Error="Only TB/RH proposals can be approved by hand")
                actions.append(f"Proposal {proposal.proposal_id} ({proposal.engine}) ignored: "
                               f"Trend Core trades aren't approved by hand")
                continue
            if proposal.status == STATUS_REJECTED:
                self._handle_rejection(proposal)
                actions.append(f"Proposal {proposal.proposal_id} ({proposal.engine} {proposal.side}) was rejected -- reverted to active monitoring")
            elif proposal.status == STATUS_APPROVED:
                if proposal.side == "BUY":
                    self._handle_approved_buy(proposal, candle, actions)
                else:
                    self._handle_approved_sell(proposal, candle, actions)

    def _expire_old_proposals(self, candle: Candle, all_proposals: list, actions: list[str]) -> None:
        """Mark PENDING/APPROVED proposals older than PROPOSAL_MAX_AGE_HOURS as
        EXPIRED_STALE, in the store and in `all_proposals`, before anything
        else reads them this cycle. So an expired APPROVED proposal is never
        executed late, an expired BUY stops blocking its engine's entries,
        and an expired SELL lets _release_orphaned_exits free its position
        for fresh exit checks. Age is measured from the
        proposal's Created At to this candle's timestamp, both window starts."""
        expired, unreadable = find_expired(all_proposals, candle.timestamp, PROPOSAL_MAX_AGE_HOURS * 3600)
        for p in expired:
            age_h = (candle.timestamp - p.created_at) / 3600
            note = (f"Expired: still {p.status} {age_h:.0f}h after it was written "
                    f"(limit {PROPOSAL_MAX_AGE_HOURS}h)")
            self.proposals.update_status_by_id(p.proposal_id, STATUS_EXPIRED_STALE, Error=note)
            p.status = STATUS_EXPIRED_STALE
            actions.append(f"Proposal {p.proposal_id} ({p.engine} {p.side}) expired after {age_h:.0f}h unresolved")
        for p in unreadable:
            log.warning("Proposal %s (%s %s) is %s but its Created At couldn't be read -- not expiring it",
                        p.proposal_id, p.engine, p.side, p.status)

    def _release_orphaned_exits(self, all_proposals: list, actions: list[str]) -> None:
        """Clear pending_exit on any position with no PENDING/APPROVED SELL in
        the store, so check_exits evaluates it again this cycle. Covers
        positions frozen by a failed sell under code older than the
        2026-09-24 retry fix: that frozen flag sits in Live State and is
        restored on every restart, and nothing else would ever clear it
        Runs after Phase 0, so a sell approved
        and executed this cycle has already removed or released its
        position. `all_proposals` was read before Phase 0, so a SELL that
        Phase 0 just executed still shows APPROVED here; that only skips a
        position Phase 0 has already dealt with."""
        positions = [("TB", p) for p in self.tb.positions]
        if self.rh.position is not None:
            positions.append(("RH", self.rh.position))
        for engine_name, position in positions:
            if position.pending_exit and not has_open_exit_in_list(all_proposals, position.position_id):
                position.pending_exit = False
                log.warning("%s position %s had pending_exit set with no open SELL proposal -- released",
                            engine_name, position.position_id)
                actions.append(f"{engine_name} position {position.position_id} had no open SELL proposal "
                                f"-- released for exit checks")

    def _handle_rejection(self, proposal: Proposal) -> None:
        if proposal.side == "SELL" and proposal.position_id:
            _, position = self._find_position(proposal.position_id)
            if position is not None:
                position.pending_exit = False   # let check_exits re-evaluate it fresh next cycle
        self.proposals.update_status_by_id(proposal.proposal_id, STATUS_REJECTED_PROCESSED)

    def _handle_approved_buy(self, proposal: Proposal, candle: Candle, actions: list[str]) -> None:
        try:
            fill_result = execute_buy(self.network, self.private_key, self.wallet_address,
                                       proposal.size_usd, proposal.price_at_proposal)
        except StalePriceError as e:
            self.proposals.update_status_by_id(proposal.proposal_id, "EXPIRED_STALE", Error=str(e))
            actions.append(f"BUY proposal {proposal.proposal_id} expired (stale price): {e}")
            return
        except ExecutionRevertedError as e:
            self.proposals.update_status_by_id(proposal.proposal_id, STATUS_FAILED, Error=str(e), **{"Tx Hash": e.tx_hash})
            actions.append(f"BUY proposal {proposal.proposal_id} reverted on-chain: {e}")
            return
        except Exception as e:   # noqa: BLE001 -- any other failure must still be recorded, not silently lost
            self.proposals.update_status_by_id(proposal.proposal_id, STATUS_FAILED, Error=str(e))
            actions.append(f"BUY proposal {proposal.proposal_id} failed: {e}")
            return

        engine_obj = self.tb if proposal.engine == "TB" else self.rh
        pending_fill = PendingFill(engine=proposal.engine, side="BUY", signal_candle_index=self.candle_index,
                                    size_usd=proposal.size_usd, adx_tier=proposal.meta.get("adx_tier", 0),
                                    meta=proposal.meta)
        position = engine_obj.open_position(pending_fill, fill_result, self.candle_index, candle.timestamp)

        self.proposals.update_status_by_id(proposal.proposal_id, STATUS_EXECUTED, **{
            "Tx Hash": fill_result.tx_hash, "Actual Fill Price": fill_result.fill_price,
            "Actual Amount": fill_result.wbtc_qty,
        })
        proposal.tx_hash, proposal.actual_fill_price = fill_result.tx_hash, fill_result.fill_price
        proposal.actual_amount, proposal.executed_at = fill_result.wbtc_qty, candle.timestamp
        self.proposals.append_live_trade(proposal)
        actions.append(f"BUY executed: {proposal.engine} bought {fill_result.wbtc_qty:.8f} WBTC "
                        f"@ ${fill_result.fill_price:,.2f} (tx {fill_result.tx_hash})")

    def _handle_approved_sell(self, proposal: Proposal, candle: Candle, actions: list[str]) -> None:
        engine_name, position = self._find_position(proposal.position_id)
        if position is None:
            self.proposals.update_status_by_id(proposal.proposal_id, STATUS_FAILED,
                                                Error="Position not found (already closed?)")
            actions.append(f"SELL proposal {proposal.proposal_id} failed: position not found")
            return

        try:
            fill_result = execute_sell(self.network, self.private_key, self.wallet_address,
                                        position.wbtc_qty, proposal.price_at_proposal)
        except StalePriceError as e:
            self.proposals.update_status_by_id(proposal.proposal_id, "EXPIRED_STALE", Error=str(e))
            position.pending_exit = False   # let check_exits re-propose the exit next cycle
            actions.append(f"SELL proposal {proposal.proposal_id} expired (stale price): {e}")
            return
        except ExecutionRevertedError as e:
            self.proposals.update_status_by_id(proposal.proposal_id, STATUS_FAILED, Error=str(e), **{"Tx Hash": e.tx_hash})
            position.pending_exit = False   # let check_exits re-propose the exit next cycle
            actions.append(f"SELL proposal {proposal.proposal_id} reverted on-chain: {e}")
            return
        except Exception as e:   # noqa: BLE001
            self.proposals.update_status_by_id(proposal.proposal_id, STATUS_FAILED, Error=str(e))
            position.pending_exit = False   # let check_exits re-propose the exit next cycle
            actions.append(f"SELL proposal {proposal.proposal_id} failed: {e}")
            return

        engine_obj = self.tb if engine_name == "TB" else self.rh
        net_pnl = fill_result.usd_value - position.size_usd
        engine_obj.record_realized_pnl(net_pnl)
        engine_obj.apply_fill_side_effects(proposal.reason, self.candle_index)
        if engine_name == "TB":
            self.tb.positions = [p for p in self.tb.positions if p.position_id != position.position_id]
        else:
            self.rh.position = None

        self.proposals.update_status_by_id(proposal.proposal_id, STATUS_EXECUTED, **{
            "Tx Hash": fill_result.tx_hash, "Actual Fill Price": fill_result.fill_price,
            "Actual Amount": fill_result.usd_value,
        })
        proposal.tx_hash, proposal.actual_fill_price = fill_result.tx_hash, fill_result.fill_price
        proposal.actual_amount, proposal.executed_at = fill_result.usd_value, candle.timestamp
        self.proposals.append_live_trade(proposal)
        actions.append(f"SELL executed: {engine_name} sold {position.wbtc_qty:.8f} WBTC "
                        f"@ ${fill_result.fill_price:,.2f} (tx {fill_result.tx_hash}), net P&L ${net_pnl:,.2f}")

    # -----------------------------------------------------------------
    def tc_status(self, close: float) -> dict:
        """The Trend Core's current numbers, for the cycle notes and the summary."""
        tb_val, rh_val = self._position_value(close)
        equity = self.vault.balance + tb_val + rh_val
        held = self.tc_wbtc_qty * close
        return {"ready": self.tc.ready, "candles": self.tc.n, "score": self.tc.score(),
                "target": self.tc.target_usd(equity) if self.trend_core_enabled else 0.0,
                "held": held, "equity": equity}

    def _rebalance_trend_core(self, candle: Candle, actions: list[str]) -> None:
        """Move the Trend Core's holding towards its target (identical rules to
        the backtest's TrendCoreRun.rebalance). Disabled with a holding left:
        target 0, sell it all."""
        close = candle.close
        if self.trend_core_enabled and not self.tc.ready:
            actions.append(f"Trend Core warming up ({self.tc.n}/{TC_WARMUP} candles); no trade")
            return
        st = self.tc_status(close)
        equity, held, target = st["equity"], st["held"], st["target"]
        gap = target - held
        info = f"Trend Core: score {st['score']:.1f}/20, target ${target:,.2f}, holding ${held:,.2f}"
        rollback = not self.trend_core_enabled
        if not rollback and (abs(gap) <= self.tc.buffer_usd(equity) or abs(gap) < TC_MIN_TRADE_USD):
            actions.append(info + " -- no trade (within the buffer)")
            return
        side = "BUY" if gap > 0 else "SELL"
        size, qty, sell_all = 0.0, 0.0, False
        if side == "BUY":
            size = self.vault.max_affordable_size(gap)
            if size < TC_MIN_TRADE_USD:
                actions.append(info + f" -- no trade (only ${size:,.2f} USDC free above the reserve)")
                return
        else:
            sell_all = rollback or target <= 0
            qty = self.tc_wbtc_qty if sell_all else min(self.tc_wbtc_qty, -gap / close)
            if qty <= 0:
                return
        p = Proposal(proposal_id=new_proposal_id(), created_at=candle.timestamp, engine="TC", side=side,
                     size_usd=size, position_id="TC", reason="ROLLBACK" if rollback else "REBALANCE",
                     price_at_proposal=close, notes=info,
                     meta={"score": round(st["score"], 3), "target_usd": round(target, 2), "held_usd": round(held, 2)})
        try:
            if side == "BUY":
                fill = execute_buy(self.network, self.private_key, self.wallet_address, size, close)
                self.tc_wbtc_qty += fill.wbtc_qty
                self.tc_cost_usd += fill.usd_value
                msg = f"TC BUY executed: {fill.wbtc_qty:.8f} WBTC for ${fill.usd_value:,.2f} @ ${fill.fill_price:,.2f}"
            else:
                before = self.tc_wbtc_qty
                # +0.1 satoshi: the amount is truncated to whole satoshis, and float rounding could drop one
                fill = execute_sell(self.network, self.private_key, self.wallet_address,
                                    (round(qty * 1e8) + 0.1) / 1e8, close)
                frac = min(1.0, fill.wbtc_qty / before) if before > 0 else 1.0
                pnl = fill.usd_value - self.tc_cost_usd * frac
                if sell_all or before - fill.wbtc_qty < 1e-8:
                    self.tc_wbtc_qty, self.tc_cost_usd = 0.0, 0.0
                else:
                    self.tc_wbtc_qty = before - fill.wbtc_qty
                    self.tc_cost_usd *= (1 - frac)
                msg = (f"TC SELL executed: {fill.wbtc_qty:.8f} WBTC for ${fill.usd_value:,.2f} @ "
                       f"${fill.fill_price:,.2f}, realised P&L ${pnl:,.2f}")
        except StalePriceError as e:     # includes the TWAP guard
            p.status, p.error = STATUS_EXPIRED_STALE, str(e)
            actions.append(info + f" -- {side} skipped: {e}")
            self._log_tc_row(p, executed=False)
            return
        except Exception as e:   # noqa: BLE001 -- recorded, retried next cycle
            p.status, p.error = STATUS_FAILED, str(e)
            actions.append(info + f" -- {side} failed: {e}")
            self._log_tc_row(p, executed=False)
            return
        p.status, p.tx_hash, p.actual_fill_price = STATUS_EXECUTED, fill.tx_hash, fill.fill_price
        p.actual_amount, p.executed_at = (fill.wbtc_qty if side == "BUY" else fill.usd_value), candle.timestamp
        actions.append(info + f" -- {msg} (tx {fill.tx_hash})")
        self._log_tc_row(p, executed=True)
        try:
            self.vault.refresh_balance()
        except Exception as e:  # noqa: BLE001
            log.warning("Post-trade USDC balance refresh failed after a Trend Core trade: %s", e)

    def _log_tc_row(self, p: Proposal, executed: bool) -> None:
        """Write the Trend Core's row (and Live Trades row). A storage error must
        never lose a real trade: the holding is already updated in memory and
        is saved with the state at the end of the cycle."""
        try:
            self.proposals.write_new_proposal(p)
            if executed:
                self.proposals.append_live_trade(p)
        except Exception as e:  # noqa: BLE001
            log.error("Couldn't write the Trend Core %s row to the proposals file (trade state is kept): %s", p.side, e)
