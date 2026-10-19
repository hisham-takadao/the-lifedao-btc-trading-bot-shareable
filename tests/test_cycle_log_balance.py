"""Tests for P9: the cycle record (Live Cycle Log row, Live Summary) must
show the USDC balance after any trade executed in that cycle, not the one
read before it. Replays rows 508 (2026-10-12, two TB sells) and 511
(2026-10-13, first RH buy) of the live log. Offline, fake chain."""

import pytest

import run_bot as live_main
from bot import live_orchestrator
from bot.live_config import MAINNET
from bot.live_execution import LiveFillResult
from bot.live_wallet import LiveWalletError
from bot.live_state_store import restore_live_state
from bot.portfolio import PendingFill

from test_frozen_exits import (
    FakeProposalSheet, NEXT_CLOSE, QTY, WALLET, next_candle, old_saved_state, sheet_as_today,
)

RH_BUY_USD = 128.89
RH_FILL_PRICE = 83_134.81


class Wallet:
    """Fake chain that tracks the wallet's USDC like the real one would."""

    def __init__(self, usdc):
        self.usdc = usdc
        self.reads = 0
        self.fail_reads_after = None   # make reads after this many fail

    def refresh(self, vault):
        self.reads += 1
        if self.fail_reads_after is not None and self.reads > self.fail_reads_after:
            raise LiveWalletError("All RPC endpoints failed for eth_call")
        vault._cached_usdc_balance = self.usdc
        return self.usdc

    def execute_sell(self, network, private_key, wallet_address, wbtc_qty, price_at_proposal):
        usd = wbtc_qty * NEXT_CLOSE
        self.usdc += usd
        return LiveFillResult(fill_price=NEXT_CLOSE, wbtc_qty=wbtc_qty, usd_value=usd, slippage_pct=0.0,
                              fees_usd=0.0, tx_hash="0xsell", gas_used=0, gas_cost_eth=0.0, block_number=0)

    def execute_buy(self, network, private_key, wallet_address, size_usd, price_at_proposal):
        self.usdc -= size_usd
        return LiveFillResult(fill_price=RH_FILL_PRICE, wbtc_qty=size_usd / RH_FILL_PRICE, usd_value=size_usd,
                              slippage_pct=0.0, fees_usd=0.0, tx_hash="0xbuy", gas_used=0, gas_cost_eth=0.0,
                              block_number=0)


def install(monkeypatch, wallet):
    monkeypatch.setattr(live_orchestrator, "execute_sell", wallet.execute_sell)
    monkeypatch.setattr(live_orchestrator, "execute_buy", wallet.execute_buy)
    monkeypatch.setattr(live_orchestrator.LiveVault, "refresh_balance", lambda self: wallet.refresh(self))


def test_sell_cycle_shows_proceeds(monkeypatch):
    """Row 508: showed Vault USDC 120.45 after the sells had returned ~$77.85."""
    wallet = Wallet(120.445033)
    install(monkeypatch, wallet)
    orch = restore_live_state(old_saved_state(), WALLET, "unused-in-tests", MAINNET, sheet_as_today(),
                              auto_execute=True)

    rec = orch.process_candle(next_candle())

    proceeds = sum(QTY.values()) * NEXT_CLOSE
    assert orch.tb.positions == []
    assert rec.vault_usdc == pytest.approx(120.445033 + proceeds)
    assert rec.combined_equity == pytest.approx(120.445033 + proceeds)


def _flat_orch_with_rh_signal(monkeypatch, wallet):
    install(monkeypatch, wallet)
    state = old_saved_state()
    state["tb_positions"] = []
    orch = restore_live_state(state, WALLET, "unused-in-tests", MAINNET, FakeProposalSheet(), auto_execute=True)
    monkeypatch.setattr(orch.tb, "check_entry", lambda *a, **k: None)
    monkeypatch.setattr(orch.rh, "check_entry", lambda idx, *a, **k: PendingFill(
        engine="RH", side="BUY", signal_candle_index=idx, size_usd=RH_BUY_USD))
    return orch


def test_buy_cycle_does_not_double_count(monkeypatch):
    """Row 511: showed Vault USDC 198.29 and equity $326.96 after a $128.89 buy."""
    wallet = Wallet(198.290587)
    orch = _flat_orch_with_rh_signal(monkeypatch, wallet)
    candle = next_candle()

    rec = orch.process_candle(candle)

    position_value = (RH_BUY_USD / RH_FILL_PRICE) * candle.close
    assert orch.rh.position is not None
    assert rec.vault_usdc == pytest.approx(198.290587 - RH_BUY_USD)
    assert rec.rh_position_value == pytest.approx(position_value)
    assert rec.combined_equity == pytest.approx(198.290587 - RH_BUY_USD + position_value)
    assert rec.combined_equity < 200   # not ~$327

    summary = live_main.build_summary(orch, rec.close)
    assert summary["USDC ($)"] == round(198.290587 - RH_BUY_USD, 2)
    assert summary["Combined equity ($)"] == round(rec.combined_equity, 2)


def test_failed_post_trade_read_still_finishes_the_cycle(monkeypatch):
    wallet = Wallet(198.290587)
    wallet.fail_reads_after = 1   # the start-of-cycle read works, the post-trade one fails
    orch = _flat_orch_with_rh_signal(monkeypatch, wallet)

    rec = orch.process_candle(next_candle())

    assert orch.rh.position is not None             # the trade is kept in memory, so state gets saved
    assert rec.vault_usdc == pytest.approx(198.290587)   # pre-trade value, flagged below
    assert any("not re-read after trading" in a for a in rec.actions)


def test_no_trade_no_extra_read(monkeypatch):
    wallet = Wallet(198.290587)
    install(monkeypatch, wallet)
    state = old_saved_state()
    state["tb_positions"] = []
    orch = restore_live_state(state, WALLET, "unused-in-tests", MAINNET, FakeProposalSheet(), auto_execute=True)
    monkeypatch.setattr(orch.tb, "check_entry", lambda *a, **k: None)
    monkeypatch.setattr(orch.rh, "check_entry", lambda *a, **k: None)

    orch.process_candle(next_candle())

    assert wallet.reads == 1
