"""Tests for the start-up allowance-vs-balance check and the user-run
approval script (scripts/approve_tokens.py). Every chain call is faked; nothing is
signed against a real key or sent."""

import importlib.util
import os

import pytest

from bot import live_wallet
from bot.live_config import MAINNET
from bot.live_wallet import (
    LiveWalletError, check_router_approvals, encode_erc20_approve,
    SELECTOR_ALLOWANCE, SELECTOR_BALANCE_OF,
)

# Public Hardhat test key: well known, holds no funds, never use it for real.
HARDHAT_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
HARDHAT_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
MAX_UINT256 = 2 ** 256 - 1


def fake_token_reads(monkeypatch, module, allowances, balances):
    """Answer allowance/balanceOf eth_calls from dicts keyed by token address."""
    def fake_simulate(rpc_urls, from_address, to_address, data, value=0):
        token = to_address.lower()
        if data[:4] == SELECTOR_ALLOWANCE:
            return allowances[token].to_bytes(32, "big")
        if data[:4] == SELECTOR_BALANCE_OF:
            return balances[token].to_bytes(32, "big")
        raise AssertionError(f"unexpected eth_call data {data[:4].hex()}")
    monkeypatch.setattr(module, "simulate_call", fake_simulate)


USDC, WBTC = MAINNET.usdc_address.lower(), MAINNET.wbtc_address.lower()


# ---------------------------------------------------------------------------
# Startup check
# ---------------------------------------------------------------------------

def _approvals(monkeypatch, usdc_allow, usdc_bal, wbtc_allow, wbtc_bal):
    fake_token_reads(monkeypatch, live_wallet,
                     {USDC: usdc_allow, WBTC: wbtc_allow}, {USDC: usdc_bal, WBTC: wbtc_bal})
    return check_router_approvals(list(MAINNET.rpc_urls), MAINNET.router_address, HARDHAT_ADDR,
                                  MAINNET.usdc_address, MAINNET.wbtc_address)


def test_finite_allowance_below_balance_is_flagged(monkeypatch):
    # The real numbers on 2026-10-12 after the sells.
    a = _approvals(monkeypatch, 120_445_033, 198_290_587, MAX_UINT256, 0)
    assert a["ok"] is True                      # the old >= 1 check alone said everything was fine
    assert a["usdc_covers_balance"] is False
    assert a["wbtc_covers_balance"] is True


def test_unlimited_allowances_cover_balance(monkeypatch):
    a = _approvals(monkeypatch, MAX_UINT256, 198_290_587, MAX_UINT256, 94_167)
    assert a["ok"] and a["usdc_covers_balance"] and a["wbtc_covers_balance"]


def test_zero_allowance_still_reported_missing(monkeypatch):
    a = _approvals(monkeypatch, 0, 198_290_587, MAX_UINT256, 0)
    assert a["usdc_ok"] is False and a["ok"] is False


# ---------------------------------------------------------------------------
# scripts/approve_tokens.py
# ---------------------------------------------------------------------------

def _load_script():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "approve_tokens.py")
    spec = importlib.util.spec_from_file_location("approve_tokens_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSender:
    def __init__(self):
        self.signed = []
        self.broadcast = []

    def build_and_sign_transaction(self, **kwargs):
        self.signed.append(kwargs)

        class Signed:
            nonce, gas_price, gas_limit, raw_hex = 0, 0, 0, "0x00"
        return Signed()

    def broadcast_transaction(self, rpc_urls, raw_hex):
        self.broadcast.append(raw_hex)
        return "0xapprove"


@pytest.fixture
def script(monkeypatch):
    script_module = _load_script()
    from bot import approvals as module      # the approval logic lives here; the script calls it
    monkeypatch.setattr(module, "main", script_module.main, raising=False)
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    monkeypatch.delenv("WALLET_ADDRESS", raising=False)
    sender = FakeSender()
    monkeypatch.setattr(module, "build_and_sign_transaction", sender.build_and_sign_transaction)
    monkeypatch.setattr(module, "broadcast_transaction", sender.broadcast_transaction)
    monkeypatch.setattr(module, "wait_for_receipt",
                        lambda rpc_urls, tx: {"status": "0x1", "blockNumber": "0x1", "gasUsed": "0x1"})
    monkeypatch.setattr(module, "receipt_succeeded", lambda r: True)
    state = {"allowance": {USDC: 0, WBTC: 0}, "simulate_ok": True}

    def fake_simulate(rpc_urls, from_address, to_address, data, value=0):
        token = to_address.lower()
        assert token in (USDC, WBTC)
        if data[:4] == SELECTOR_ALLOWANCE:
            return state["allowance"][token].to_bytes(32, "big")
        if data[:4] == SELECTOR_BALANCE_OF:
            return (100_000_000).to_bytes(32, "big")
        assert data == encode_erc20_approve(MAINNET.router_address, MAX_UINT256)
        if not state["simulate_ok"]:
            raise LiveWalletError("execution reverted")
        return (1).to_bytes(32, "big")

    def fake_broadcast(rpc_urls, raw_hex):
        tx = sender.signed[-1]
        state["allowance"][tx["to_address"].lower()] = MAX_UINT256      # the approval took effect
        return sender.broadcast_transaction(rpc_urls, raw_hex)

    monkeypatch.setattr(module, "simulate_call", fake_simulate)
    monkeypatch.setattr(module, "broadcast_transaction", fake_broadcast)
    monkeypatch.setattr(live_wallet, "simulate_call", fake_simulate)   # used by check_token_* helpers
    return module, sender, state


def test_script_sends_both_unlimited_approvals_after_yes(script, monkeypatch):
    module, sender, _ = script
    monkeypatch.setattr("builtins.input", lambda prompt="": "YES")
    module.main()
    assert [t["to_address"] for t in sender.signed] == [MAINNET.usdc_address, MAINNET.wbtc_address]
    assert all(t["data"] == encode_erc20_approve(MAINNET.router_address, MAX_UINT256) for t in sender.signed)
    assert all(t["chain_id"] == 42161 for t in sender.signed)
    assert len(sender.broadcast) == 2


@pytest.mark.parametrize("answer", ["", "yes", "no", "Y"])
def test_script_sends_nothing_without_exact_yes(script, monkeypatch, answer):
    module, sender, _ = script
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    module.main()
    assert sender.signed == [] and sender.broadcast == []


def test_script_skips_tokens_already_unlimited(script, monkeypatch):
    module, sender, state = script
    state["allowance"][USDC] = MAX_UINT256
    monkeypatch.setattr("builtins.input", lambda prompt="": "YES")
    module.main()
    assert [t["to_address"] for t in sender.signed] == [MAINNET.wbtc_address]


def test_script_sends_nothing_if_simulation_fails(script, monkeypatch):
    module, sender, state = script
    state["simulate_ok"] = False
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("should not ask"))
    module.main()
    assert sender.broadcast == []
