"""
Serializes/restores LiveOrchestrator state -- same approach as the paper
bot's state_store.py: indicator/regime/violator-tracker state is rebuilt by
replaying saved candle history, while positions/cooldowns/flags (things
that can't be re-derived from price alone) are restored directly.

Deliberately NOT included here: proposals. Those live durably in the
proposals and trades files already -- persisting them again in
this state blob would be redundant and create two sources of truth. If the
bot restarts, get_actionable_proposals() reads whatever's actually in the
store, which is already the right behavior.

Also not included: the vault's cached USDC balance. LiveVault always
fetches fresh from chain (refresh_balance()) rather than trusting a
possibly-stale cached number -- restoring a cached balance across a
restart would reintroduce exactly the kind of drift-from-reality risk the
whole LiveVault design was meant to avoid.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Optional

from .indicators import Candle
from .portfolio import TBPosition, RHPosition
from .live_orchestrator import LiveOrchestrator
from .live_config import NetworkConfig
from .live_proposals import ProposalStore


def _candle_to_dict(c: Candle) -> dict:
    return {"timestamp": c.timestamp, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}


def _dict_to_candle(d: dict) -> Candle:
    return Candle(timestamp=d["timestamp"], open=d["open"], high=d["high"], low=d["low"],
                   close=d["close"], volume=d.get("volume", 0.0))


def serialize_live_state(orch: LiveOrchestrator) -> dict:
    return {
        "version": 1,
        "candle_index": orch.candle_index,
        "current_day": orch._current_day.isoformat() if orch._current_day else None,
        "day_open_equity": orch.day_open_equity,

        "tb_positions": [dataclasses.asdict(p) for p in orch.tb.positions],
        "tb_circuit_breaker_active": orch.tb.circuit_breaker_active,
        "tb_realized_losses_today": orch.tb.realized_losses_today,
        "tb_cooldown_until_candle_index": orch.tb.cooldown_until_candle_index,

        "rh_position": dataclasses.asdict(orch.rh.position) if orch.rh.position else None,
        "rh_circuit_breaker_active": orch.rh.circuit_breaker_active,
        "rh_realized_losses_today": orch.rh.realized_losses_today,
        "rh_cooldown_until_candle_index": orch.rh.cooldown_until_candle_index,
        "rh_violator_history": [list(t) for t in orch.rh.violator_tracker.history],

        "candle_history": [_candle_to_dict(c) for c in orch.candle_history],
    }


def restore_live_state(state: dict, wallet_address: str, private_key: str,
                        network: NetworkConfig, proposal_sheet: ProposalStore,
                        auto_execute: bool = False) -> LiveOrchestrator:
    orch = LiveOrchestrator(wallet_address, private_key, network, proposal_sheet, auto_execute=auto_execute)

    for cd in state["candle_history"]:
        orch.replay_candle_indicators_only(_dict_to_candle(cd))
    orch.candle_index = state["candle_index"]   # source of truth if history was truncated by the buffer size

    orch._current_day = date.fromisoformat(state["current_day"]) if state["current_day"] else None
    orch.day_open_equity = state["day_open_equity"]

    orch.tb.positions = [TBPosition(**p) for p in state["tb_positions"]]
    orch.tb.circuit_breaker_active = state["tb_circuit_breaker_active"]
    orch.tb.realized_losses_today = state["tb_realized_losses_today"]
    orch.tb.cooldown_until_candle_index = state["tb_cooldown_until_candle_index"]

    orch.rh.position = RHPosition(**state["rh_position"]) if state["rh_position"] else None
    orch.rh.circuit_breaker_active = state["rh_circuit_breaker_active"]
    orch.rh.realized_losses_today = state["rh_realized_losses_today"]
    orch.rh.cooldown_until_candle_index = state["rh_cooldown_until_candle_index"]
    for entry in state["rh_violator_history"]:
        orch.rh.violator_tracker.history.append(tuple(entry))

    return orch
