"""The real Savings Vault (bot/savings_vault.py): deposits of idle USDC,
withdrawals before buys, the circuit-breaker guards, failure handling, the
books and the orchestrator wiring. Offline: a fake chain that behaves like
SFVault (as read from its verified source), the circuit breaker, the tokens
and the pools. Nothing is signed or sent."""

import math

import pytest
from eth_abi import decode as abi_decode, encode as abi_encode

from bot import savings_vault as sv
from bot.live_config import MAINNET
from bot.savings_vault import (
    SavingsVault, SavingsSettings, SavingsConfigError, SavingsError, load_settings, USD,
)

WALLET = "0x8B31246c3b4e1BC6c3Bf3fd9963E6d192fc51a27"
CB = "0x85Caf2ad5BFe036c6C96BD8c84D82Dfa3D0283bD"
USDC = MAINNET.usdc_address
USDT = sv.USDT_ADDRESS
VAULT = sv.SAVINGS_VAULT_ADDRESS
ROUTER = MAINNET.router_address
NOW = 1_794_300_000
MAX = 2 ** 256 - 1


def sel(sig):
    return sv._selector(sig)


def word(n):
    return n.to_bytes(32, "big")


def addr_topic(a):
    return "0x" + "0" * 24 + a.lower()[2:]


def sqrt_x96(price):
    return int(math.sqrt(price) * 2 ** 96)


class FakeChain:
    """A small, faithful model of what the bot touches on-chain."""

    def __init__(self, wallet_usdc=200.0, shares=0, price=1.016932):
        self.bal = {USDC.lower(): int(wallet_usdc * USD), USDT.lower(): 0}
        self.allow = {}                       # (token, spender) -> amount
        self.shares = shares
        self.price = price                    # USDC per share
        self.paused = False
        self.member = True
        self.impl = sv.EXPECTED_IMPLEMENTATION
        self.global_remaining = 7_000 * USD
        self.user_remaining = 8_000 * USD
        self.approval_threshold = 0
        self.pause_flags = 0
        self.pending_request = 0
        self.cb_hold = False                  # the breaker swallows the withdrawal (returns 0, pays nothing)
        self.usdt_fraction = 0.5              # share of a withdrawal paid in USDT
        self.usdt_per_usdc = 1.0005
        self.eth_wei = int(0.002 * 1e18)
        self.gas_price_wei = int(0.02e9)
        self.revert_next = set()              # kinds whose next transaction reverts
        self.delay = set()                    # kinds whose next transaction isn't mined in time
        self.sent = []                        # (to, selector, data)
        self.receipts = {}
        self.unreleased = {}

    # ---- reads -----------------------------------------------------------
    def value(self, shares):
        return int(shares * self.price)

    def call(self, to, data):
        s, args, t = data[:4], data[4:], to.lower()
        if t == VAULT.lower():
            if s == sel("balanceOf(address)"):
                return word(self.shares)
            if s == sel("convertToAssets(uint256)"):
                return word(self.value(abi_decode(["uint256"], args)[0]))
            if s == sel("convertToShares(uint256)"):
                return word(int(abi_decode(["uint256"], args)[0] / self.price))
            if s == sel("paused()"):
                return word(int(self.paused))
            if s == sel("asset()"):
                return abi_encode(["address"], [USDC])
            if s == sel("maxDeposit(address)"):
                return word(900_000 * USD if self.member else 0)
            if s == sel("maxWithdraw(address)"):
                return word(self.value(self.shares))
            if s == sel("maxRedeem(address)"):
                return word(self.shares)
            if s == sel("previewRedeem(uint256)"):
                return word(self.value(abi_decode(["uint256"], args)[0]))
            if s == sel("deposit(uint256,address)"):
                amount, _ = abi_decode(["uint256", "address"], args)
                if self.paused or not self.member or self.allow.get((USDC.lower(), VAULT.lower()), 0) < amount:
                    raise sv.SimulationRevertedError("execution reverted")
                return word(int(amount / self.price))
            if s == sel("withdraw(uint256,address,address)"):
                amount = abi_decode(["uint256", "address", "address"], args)[0]
                if self.paused or amount > self.value(self.shares):
                    raise sv.SimulationRevertedError("execution reverted")
                return word(0 if self.cb_hold else math.ceil(amount / self.price))
            if s == sel("redeem(uint256,address,address)"):
                shares = abi_decode(["uint256", "address", "address"], args)[0]
                return word(0 if self.cb_hold else self.value(shares))
        if t in (USDC.lower(), USDT.lower()):
            if s == sel("balanceOf(address)"):
                return word(self.bal[t])
            if s == sel("allowance(address,address)"):
                _, spender = abi_decode(["address", "address"], args)
                return word(self.allow.get((t, spender.lower()), 0))
        if t == sv.STABLE_POOL.lower() and s == sel("slot0()"):
            return word(sqrt_x96(self.usdt_per_usdc)) + word(0) * 6
        if t == sv.ETH_USDC_POOL.lower() and s == sel("slot0()"):
            return word(sqrt_x96(2000 / 1e12)) + word(0) * 6
        if t == sv.ADDRESS_MANAGER.lower():
            return word(0) + abi_encode(["address"], [CB]) + word(3)
        if t == CB.lower():
            if s == sel("config(address)"):
                return word(10_000 * USD) + word(8_000 * USD) + word(self.approval_threshold) + word(1)
            if s == sel("protectionEnabled(address)"):
                return word(1)
            if s == sel("getGlobalWindowState(address)"):
                return word(NOW) + word(0) + word(10_000 * USD) + word(self.global_remaining) + word(0)
            if s == sel("getUserWindowState(address,address)"):
                return word(NOW) + word(0) + word(8_000 * USD) + word(self.user_remaining) + word(0)
            if s == sel("getPauseFlags(address)"):
                return word(self.pause_flags)
            if s == sel("pendingWithdrawalRequestId(address,address)"):
                return word(self.pending_request)
        raise AssertionError(f"unexpected call to {to} selector {s.hex()}")

    def storage_at(self, address, slot):
        return int(self.impl, 16)

    def eth_balance(self):
        return self.eth_wei

    def gas_price(self):
        return self.gas_price_wei

    # ---- writes ------------------------------------------------------------
    def _transfer_log(self, token, frm, to, amount):
        return {"address": token, "topics": [sv.TOPIC_TRANSFER, addr_topic(frm), addr_topic(to)],
                "data": "0x" + word(amount).hex()}

    def send(self, to, data):
        s, args, t = data[:4], data[4:], to.lower()
        h = "0x%064x" % (len(self.sent) + 1)
        self.sent.append((t, s, data))
        kind = {sel("approve(address,uint256)"): "approve", sel("deposit(uint256,address)"): "deposit",
                sel("withdraw(uint256,address,address)"): "withdraw",
                sel("redeem(uint256,address,address)"): "redeem"}.get(s, "swap")
        logs, ok = [], kind not in self.revert_next
        self.revert_next.discard(kind)
        if ok and kind == "approve":
            spender, amount = abi_decode(["address", "uint256"], args)
            self.allow[(t, spender.lower())] = amount
        elif ok and kind == "deposit":
            amount, _ = abi_decode(["uint256", "address"], args)
            key = (USDC.lower(), VAULT.lower())
            assert self.allow.get(key, 0) >= amount, "deposit without allowance"
            self.allow[key] -= amount
            self.bal[USDC.lower()] -= amount
            minted = int(amount / self.price)
            self.shares += minted
            logs = [self._transfer_log(USDC, WALLET, VAULT, amount),
                    {"address": VAULT, "topics": [sv.TOPIC_DEPOSIT, addr_topic(WALLET), addr_topic(WALLET)],
                     "data": "0x" + (word(amount) + word(minted)).hex()}]
        elif ok and kind in ("withdraw", "redeem"):
            if kind == "withdraw":
                assets = abi_decode(["uint256", "address", "address"], args)[0]
                burned = math.ceil(assets / self.price)
            else:
                burned = abi_decode(["uint256", "address", "address"], args)[0]
                assets = self.value(burned)
            if not self.cb_hold:
                self.shares -= burned
                usdt = int(assets * self.usdt_fraction)
                usdc = assets - usdt
                self.bal[USDC.lower()] += usdc
                self.bal[USDT.lower()] += usdt
                logs = [self._transfer_log(USDC, VAULT, WALLET, usdc), self._transfer_log(USDT, VAULT, WALLET, usdt),
                        {"address": VAULT, "topics": [sv.TOPIC_WITHDRAW, addr_topic(WALLET), addr_topic(WALLET),
                                                       addr_topic(WALLET)],
                         "data": "0x" + (word(assets) + word(burned)).hex()}]
        elif ok and kind == "swap":
            assert t == ROUTER.lower()
            tin, tout, fee, recipient, amount_in, min_out, _ = abi_decode(
                ["(address,address,uint24,address,uint256,uint256,uint160)"], args)[0]
            assert (tin.lower(), tout.lower(), fee) == (USDT.lower(), USDC.lower(), 100)
            key = (USDT.lower(), ROUTER.lower())
            assert self.allow.get(key, 0) >= amount_in
            out = int(amount_in / self.usdt_per_usdc * 0.9999)
            if out < min_out:
                ok = False
            else:
                self.allow[key] -= amount_in
                self.bal[USDT.lower()] -= amount_in
                self.bal[USDC.lower()] += out
                logs = [self._transfer_log(USDC, sv.STABLE_POOL, WALLET, out)]
        receipt = {"status": "0x1" if ok else "0x0", "transactionHash": h, "logs": logs,
                   "gasUsed": hex(1_000_000), "effectiveGasPrice": hex(self.gas_price_wei), "blockNumber": "0x1"}
        if kind in self.delay:
            self.delay.discard(kind)
            self.unreleased[h] = receipt
        else:
            self.receipts[h] = receipt
        return h

    def release(self):
        self.receipts.update(self.unreleased)
        self.unreleased.clear()

    def receipt(self, h):
        return self.receipts.get(h)

    def wait(self, h, timeout=0, poll=0):
        return self.receipts.get(h)

    def kinds(self):
        names = {sel("approve(address,uint256)"): "approve", sel("deposit(uint256,address)"): "deposit",
                 sel("withdraw(uint256,address,address)"): "withdraw", sel("redeem(uint256,address,address)"): "redeem"}
        return [names.get(s, "swap") for _, s, _ in self.sent]


def vault(chain, mode="on", **kw):
    settings = SavingsSettings(mode=mode, **kw)
    v = SavingsVault(chain, WALLET, settings, MAINNET, clock=lambda: NOW)
    v.state["price_history"] = [[NOW - 8 * 86400, chain.price / 1.0005]]    # ~2.3%/yr over the last week
    return v


# ---------------------------------------------------------------------------
# Settings and the address allow-list
# ---------------------------------------------------------------------------

def test_settings_default_off_and_reject_other_vaults():
    assert load_settings({}).mode == "off"
    s = load_settings({"SAVINGS_VAULT_MODE": "dry_run", "SAVINGS_VAULT_ADDRESS": VAULT.lower(),
                       "SAVINGS_MAX_DEPOSIT_USD": "5", "SAVINGS_MIN_DEPOSIT_USD": "2"})
    assert (s.mode, s.max_deposit_usd, s.min_deposit_usd) == ("dry_run", 5.0, 2.0)
    with pytest.raises(SavingsConfigError):
        load_settings({"SAVINGS_VAULT_MODE": "on", "SAVINGS_VAULT_ADDRESS": "0x" + "11" * 20})
    with pytest.raises(SavingsConfigError):
        load_settings({"SAVINGS_VAULT_MODE": "yes"})
    with pytest.raises(SavingsConfigError):
        load_settings({"SAVINGS_MIN_DEPOSIT_USD": "100", "SAVINGS_MAX_DEPOSIT_USD": "50"})
    with pytest.raises(SavingsConfigError):
        load_settings({"SAVINGS_MAX_DEPOSIT_USD": "lots"})


def test_only_allowed_contracts_and_spenders():
    v = vault(FakeChain())
    v._check_outgoing(USDC, sv.encode_erc20_approve(VAULT, 1))
    v._check_outgoing(USDT, sv.encode_erc20_approve(ROUTER, 1))
    v._check_outgoing(VAULT, sv.encode_deposit(1, WALLET))
    for to, data in [("0x" + "22" * 20, sv.encode_deposit(1, WALLET)),             # another "vault"
                     (USDC, sv.encode_erc20_approve("0x" + "33" * 20, 1)),         # approval to a stranger
                     (USDC, sv.encode_erc20_approve(ROUTER, 1)),                   # USDC only to the vault
                     (VAULT, sel("transfer(address,uint256)") + word(0) + word(1))]:
        with pytest.raises(SavingsError):
            v._check_outgoing(to, data)


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------

def test_deposit_idle_usdc_with_an_exact_approval():
    ch = FakeChain(wallet_usdc=200.0)
    v = vault(ch)
    actions = []
    v.begin_cycle(10, actions)
    v.sweep(reserved_usd=0.0, actions=actions)
    assert ch.kinds() == ["approve", "deposit"]
    approved = abi_decode(["address", "uint256"], ch.sent[0][2][4:])
    assert approved[0].lower() == VAULT.lower() and approved[1] == 195 * USD      # exactly what's deposited
    assert ch.allow[(USDC.lower(), VAULT.lower())] == 0                             # nothing left approved
    assert ch.bal[USDC.lower()] == 5 * USD                                          # the cash buffer stays
    assert v.state["deposited_usd"] == pytest.approx(195.0)
    assert v.state["shares"] == ch.shares > 0
    assert v.value_usd == pytest.approx(195.0, abs=0.01)
    assert abs(v.yield_usd()) < 0.01
    assert any(r["Action"] == "DEPOSIT" and r["Status"] == "OK" for r in v.log_rows)


def test_deposit_capped_and_reserved_buys_stay_in_the_wallet():
    ch = FakeChain(wallet_usdc=200.0)
    v = vault(ch, max_deposit_usd=100.0)
    v.begin_cycle(1, [])
    v.sweep(reserved_usd=80.0, actions=[])            # a $80 BUY waits for approval: 200-5-80 = 115 -> cap 100
    assert v.state["deposited_usd"] == pytest.approx(100.0)
    ch2 = FakeChain(wallet_usdc=120.0)
    v2 = vault(ch2)
    v2.begin_cycle(1, [])
    acts = []
    v2.sweep(reserved_usd=80.0, actions=acts)         # 120-5-80 = 35 < $50 minimum
    assert ch2.sent == [] and "below the $50.00 minimum" in acts[-1]


def test_no_deposit_when_gas_would_eat_the_yield():
    ch = FakeChain(wallet_usdc=200.0)
    ch.gas_price_wei = int(5e9)                       # 5 gwei: a round trip ~ $44
    v = vault(ch)
    v.begin_cycle(1, [])
    acts = []
    v.sweep(0.0, acts)
    assert ch.sent == [] and "not worth the gas" in acts[-1]


def test_dry_run_sends_nothing_and_says_what_it_would_do():
    ch = FakeChain(wallet_usdc=200.0)
    v = vault(ch, mode="dry_run")
    acts = []
    v.begin_cycle(1, acts)
    v.sweep(0.0, acts)
    assert v.ensure_wallet_usdc(150.0, acts) == pytest.approx(200.0)
    assert ch.sent == []
    assert any(r["Status"] == "DRY_RUN" and "would deposit $195.00" in r["Notes"] for r in v.log_rows)


def test_not_a_member_or_paused_or_new_code_blocks_deposits():
    for setup, alert_word in [(lambda c: setattr(c, "member", False), "member"),
                              (lambda c: setattr(c, "paused", True), "paused"),
                              (lambda c: setattr(c, "impl", "0x" + "44" * 20), "code")]:
        ch = FakeChain(wallet_usdc=200.0)
        setup(ch)
        v = vault(ch)
        acts = []
        v.begin_cycle(1, acts)
        v.sweep(0.0, acts)
        assert ch.sent == [], alert_word
        assert "no deposit" in acts[-1]


def test_reverted_deposit_resets_the_approval_and_alerts():
    ch = FakeChain(wallet_usdc=200.0)
    ch.revert_next.add("deposit")
    v = vault(ch)
    acts = []
    v.begin_cycle(1, acts)
    v.sweep(0.0, acts)
    assert ch.kinds() == ["approve", "deposit", "approve"]
    assert ch.allow[(USDC.lower(), VAULT.lower())] == 0
    assert ch.bal[USDC.lower()] == 200 * USD and v.state["deposited_usd"] == 0
    assert any(a.startswith("SAVINGS ALERT") and "reverted" in a for a in acts)


def test_an_unrelated_old_allowance_is_reset_first():
    ch = FakeChain(wallet_usdc=200.0)
    ch.allow[(USDC.lower(), VAULT.lower())] = 7 * USD      # left over from somewhere
    v = vault(ch)
    v.begin_cycle(1, [])
    v.sweep(0.0, [])
    assert ch.allow[(USDC.lower(), VAULT.lower())] == 0
    assert v.state["deposited_usd"] == pytest.approx(195.0)


# ---------------------------------------------------------------------------
# Withdrawals before a buy
# ---------------------------------------------------------------------------

def funded(wallet_usdc=5.0, in_vault=195.0, **kw):
    ch = FakeChain(wallet_usdc=wallet_usdc, **kw)
    ch.shares = int(in_vault * USD / ch.price)
    v = vault(ch)
    v.state["shares"] = ch.shares
    v.state["deposited_usd"] = in_vault
    v.begin_cycle(1, [])
    return ch, v


def test_withdraws_exactly_the_shortfall_and_swaps_the_usdt():
    ch, v = funded()
    acts = []
    have = v.ensure_wallet_usdc(100.0, acts)
    # short: 100 + 5 buffer - 5 in wallet = 100, +0.3% for the USDT swap
    w = abi_decode(["uint256", "address", "address"], ch.sent[0][2][4:])
    assert ch.kinds()[0] == "withdraw" and w[0] == math.ceil(100 * USD * 1.003)
    assert w[1].lower() == WALLET.lower() and w[2].lower() == WALLET.lower()
    assert ch.kinds()[1:] == ["approve", "swap"]                       # USDT -> USDC, exact approval
    assert ch.bal[USDT.lower()] == 0 and ch.allow[(USDT.lower(), ROUTER.lower())] == 0
    assert have >= 100.0 + 5.0 - 0.01
    assert v.state["withdrawn_usd"] == pytest.approx(100.3)
    assert v.state["conversion_cost_usd"] > 0


def test_no_withdrawal_when_the_wallet_already_has_enough():
    ch, v = funded(wallet_usdc=150.0)
    assert v.ensure_wallet_usdc(100.0, []) == pytest.approx(150.0)
    assert ch.sent == []


@pytest.mark.parametrize("setup,needle", [
    (lambda c: setattr(c, "paused", True), "paused"),
    (lambda c: setattr(c, "pause_flags", 1), "pause flags"),
    (lambda c: setattr(c, "pending_request", 7), "waiting for the vault team"),
    (lambda c: setattr(c, "eth_wei", 10 ** 12), "ETH"),
    (lambda c: setattr(c, "global_remaining", 450 * USD), "limits"),     # under the $500 margin for others
    (lambda c: setattr(c, "user_remaining", 0), "limits"),
])
def test_guards_stop_a_withdrawal_before_anything_is_sent(setup, needle):
    ch, v = funded()
    setup(ch)
    with pytest.raises(SavingsError, match=needle):
        v.ensure_wallet_usdc(100.0, [])
    assert "withdraw" not in ch.kinds()


def test_never_splits_to_dodge_the_approval_threshold_but_stays_under_it():
    ch, v = funded()
    ch.approval_threshold = 60 * USD
    acts = []
    v.ensure_wallet_usdc(100.0, acts)
    w = abi_decode(["uint256", "address", "address"], ch.sent[0][2][4:])[0]
    assert w < 60 * USD and ch.kinds().count("withdraw") == 1
    assert any("only $" in a for a in acts)


def test_breaker_that_would_hold_the_withdrawal_means_nothing_is_sent():
    ch, v = funded()
    ch.cb_hold = True
    with pytest.raises(SavingsError, match="circuit breaker"):
        v.ensure_wallet_usdc(100.0, [])
    assert ch.sent == []


def test_withdrawal_that_pays_nothing_raises_an_alert():
    ch, v = funded()
    real_call = ch.call

    def call(to, data):          # the simulation looks fine, the real transaction is held
        if data[:4] == sel("withdraw(uint256,address,address)"):
            return word(1)
        return real_call(to, data)
    ch.call = call
    ch.cb_hold = True
    acts = []
    with pytest.raises(SavingsError, match="paid nothing"):
        v.ensure_wallet_usdc(100.0, acts)
    assert any("circuit breaker held it" in a for a in acts)
    assert v.state["withdrawn_usd"] == 0


def test_usdt_off_peg_is_kept_not_swapped():
    ch, v = funded()
    ch.usdt_per_usdc = 1.02                           # 1 USDT = 0.98 USDC
    acts = []
    v.ensure_wallet_usdc(100.0, acts)
    assert "swap" not in ch.kinds() and ch.bal[USDT.lower()] > 0
    assert any("not swapping" in a for a in acts)


# ---------------------------------------------------------------------------
# Unconfirmed transactions, outside changes, emergency withdraw-all
# ---------------------------------------------------------------------------

def test_unconfirmed_deposit_is_remembered_and_booked_when_it_confirms():
    ch = FakeChain(wallet_usdc=200.0)
    ch.delay.add("deposit")
    v = vault(ch)
    acts = []
    v.begin_cycle(1, acts)
    v.sweep(0.0, acts)
    assert v.state["pending"]["kind"] == "deposit" and v.state["deposited_usd"] == 0
    # while it's unconfirmed: no new vault transactions
    n = len(ch.sent)
    v.begin_cycle(2, acts)
    v.sweep(0.0, acts)
    assert len(ch.sent) == n
    with pytest.raises(SavingsError, match="unconfirmed"):
        v.withdraw(10 * USD, acts)
    ch.release()
    v.begin_cycle(3, acts)
    assert v.state["pending"] is None
    assert v.state["deposited_usd"] == pytest.approx(195.0) and v.state["shares"] == ch.shares
    assert abs(v.yield_usd()) < 0.01


def test_shares_changed_outside_the_bot_are_booked_not_counted_as_yield():
    ch, v = funded()
    ch.shares += int(50 * USD / ch.price)              # the owner deposited $50 with the script
    v.begin_cycle(2, [])
    assert v.state["external_usd"] == pytest.approx(50.0, abs=0.01)
    assert abs(v.yield_usd()) < 0.02
    assert any(r["Action"] == "EXTERNAL_CHANGE" for r in v.log_rows)


def test_yield_is_value_plus_withdrawn_minus_deposited():
    ch, v = funded()
    ch.price *= 1.01                                   # the vault earned 1%
    v.begin_cycle(2, [])
    assert v.yield_usd() == pytest.approx(1.95, abs=0.01)


def test_withdraw_all_redeems_everything_and_never_deposits():
    ch, v = funded(wallet_usdc=200.0)
    v.settings = SavingsSettings(mode="withdraw_all")
    acts = []
    v.sweep(0.0, acts)
    assert ch.shares == 0 and "deposit" not in ch.kinds()
    assert ch.bal[USDT.lower()] == 0
    assert ch.bal[USDC.lower()] > 390 * USD


def test_withdraw_all_respects_the_24h_limits():
    ch, v = funded()
    ch.global_remaining = 600 * USD                   # $100 of room after the $500 margin
    v.settings = SavingsSettings(mode="withdraw_all")
    acts = []
    v.sweep(0.0, acts)
    assert 0 < ch.shares and v.value_usd == pytest.approx(95.0, abs=0.1)
    assert any("rest at the next cycles" in a for a in acts)


# ---------------------------------------------------------------------------
# Wiring into the bot: balances, sizing, the cycle, saved state
# ---------------------------------------------------------------------------

def test_wallet_balance_counts_the_vault_but_sizing_only_what_can_come_out():
    from bot.live_portfolio import LiveVault
    ch, v = funded(wallet_usdc=5.0, in_vault=195.0)
    lv = LiveVault(WALLET, MAINNET)
    lv._cached_usdc_balance = 5.0
    lv.savings = v
    assert lv.balance == pytest.approx(200.0, abs=0.01)
    assert lv.wallet_cash == pytest.approx(5.0)
    assert lv.max_affordable_size(1000) == pytest.approx(150.0, abs=0.01)       # 200 - $50 reserve
    ch.paused = True
    v.begin_cycle(2, [])
    assert lv.balance == pytest.approx(200.0, abs=0.01)                          # still the owner's money
    assert lv.max_affordable_size(1000) == 0.0                                   # but a trade can't count on it


def _orch(ch, v, monkeypatch, buy=None):
    from bot import live_orchestrator
    from bot.live_execution import LiveFillResult
    from test_frozen_exits import FakeProposalSheet

    class Sheet(FakeProposalSheet):
        def __init__(self):
            super().__init__()
            self.savings_rows = []

        def append_savings_log(self, rows):
            self.savings_rows += rows

    orch = live_orchestrator.LiveOrchestrator(WALLET, "unused", MAINNET, Sheet(), auto_execute=True)
    orch.attach_savings(v)
    monkeypatch.setattr(live_orchestrator.LiveVault, "refresh_balance",
                        lambda self: setattr(self, "_cached_usdc_balance", ch.bal[USDC.lower()] / USD)
                        or v.refresh_light())

    def execute_buy(network, key, wallet, size, price):
        assert ch.bal[USDC.lower()] >= size * USD, "bought without the USDC in the wallet"
        ch.bal[USDC.lower()] -= int(size * USD)
        return LiveFillResult(fill_price=price, wbtc_qty=size / price, usd_value=size, slippage_pct=0, fees_usd=0,
                              tx_hash="0xbuy", gas_used=0, gas_cost_eth=0, block_number=0)
    monkeypatch.setattr(live_orchestrator, "execute_buy", buy or execute_buy)
    return orch


def _candle(ts=NOW - NOW % 14400, close=90_000.0):
    from bot.indicators import Candle
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close)


def test_cycle_deposits_idle_cash_and_reports_it(monkeypatch):
    ch = FakeChain(wallet_usdc=200.0)
    v = vault(ch)
    orch = _orch(ch, v, monkeypatch)
    rec = orch.process_candle(_candle())
    assert ch.kinds() == ["approve", "deposit"]
    assert rec.vault_usdc == pytest.approx(5.0)
    assert rec.savings_value == pytest.approx(195.0, abs=0.01)
    assert rec.combined_equity == pytest.approx(200.0, abs=0.01)
    assert any(r["Action"] == "DEPOSIT" for r in orch.proposals.savings_rows)


def test_trend_core_buy_is_funded_from_the_vault(monkeypatch):
    ch, v = funded(wallet_usdc=5.0, in_vault=195.0)
    orch = _orch(ch, v, monkeypatch)
    orch.vault.refresh_balance()
    actions = []
    orch._fund_buy(120.0, actions)
    assert ch.bal[USDC.lower()] >= 120 * USD
    assert "withdraw" in ch.kinds()


def test_a_buy_the_vault_cant_fund_fails_cleanly(monkeypatch):
    ch, v = funded(wallet_usdc=5.0, in_vault=195.0)
    ch.paused = True
    orch = _orch(ch, v, monkeypatch)
    orch.vault.refresh_balance()
    from bot.live_wallet import LiveWalletError
    with pytest.raises(LiveWalletError, match="couldn't make up the difference"):
        orch._fund_buy(120.0, [])
    assert ch.sent == []


def test_a_vault_crash_never_stops_the_cycle(monkeypatch):
    ch = FakeChain(wallet_usdc=200.0)
    v = vault(ch)
    orch = _orch(ch, v, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("RPC down")
    monkeypatch.setattr(v, "sweep", boom)
    rec = orch.process_candle(_candle())
    assert any("Savings vault error (trading unaffected): RPC down" in a for a in rec.actions)


def test_saved_state_round_trip_and_older_state():
    from bot.live_state_store import serialize_live_state, restore_live_state
    from bot import live_orchestrator
    from test_frozen_exits import FakeProposalSheet, old_saved_state
    ch, v = funded()
    orch = live_orchestrator.LiveOrchestrator(WALLET, "k", MAINNET, FakeProposalSheet(), auto_execute=True)
    orch.attach_savings(v)
    state = serialize_live_state(orch)
    assert state["savings"]["deposited_usd"] == pytest.approx(195.0)
    back = restore_live_state(state, WALLET, "k", MAINNET, FakeProposalSheet())
    assert back.savings_saved_state["shares"] == ch.shares
    older = restore_live_state(old_saved_state(), WALLET, "k", MAINNET, FakeProposalSheet())
    assert older.savings_saved_state is None
    assert serialize_live_state(older)["savings"] is None


def _settings(mode):
    from types import SimpleNamespace
    return SimpleNamespace(wallet_address=WALLET, private_key="k", savings=SavingsSettings(mode=mode))


def test_startup_wiring(monkeypatch):
    import run_bot
    from bot import live_orchestrator
    from test_frozen_exits import FakeProposalSheet
    orch = live_orchestrator.LiveOrchestrator(WALLET, "k", MAINNET, FakeProposalSheet())
    run_bot.setup_savings(orch, _settings("off"), chain=FakeChain())
    assert orch.savings is None                               # off and nothing in the vault
    ch, _ = funded()
    run_bot.setup_savings(orch, _settings("off"), chain=ch)
    assert orch.savings is not None and orch.savings.mode == "off"   # off, but its money is still counted
    assert orch.vault.savings_locked == pytest.approx(orch.vault.savings_value)
    orch2 = live_orchestrator.LiveOrchestrator(WALLET, "k", MAINNET, FakeProposalSheet())
    bad = FakeChain()
    bad.call_orig = bad.call
    bad.call = lambda to, data: (abi_encode(["address"], ["0x" + "55" * 20]) if data[:4] == sel("asset()")
                                 else bad.call_orig(to, data))
    run_bot.setup_savings(orch2, _settings("on"), chain=bad)
    assert orch2.savings is None                              # not a USDC vault: refused


def test_attached_after_the_trade_cap(monkeypatch):
    """MAX_TRADE_USD swaps orch.vault for a CappedVault: the vault must be attached to that final object."""
    import run_bot
    from bot import live_orchestrator
    from test_frozen_exits import FakeProposalSheet
    orch = live_orchestrator.LiveOrchestrator(WALLET, "k", MAINNET, FakeProposalSheet())
    orch.vault = run_bot.CappedVault(WALLET, MAINNET, 25.0)
    ch, _ = funded()
    run_bot.setup_savings(orch, _settings("on"), chain=ch)
    assert orch.vault.savings is orch.savings
    orch.vault._cached_usdc_balance = 5.0
    assert orch.vault.max_affordable_size(1000) == 25.0


# ---------------------------------------------------------------------------
# Shareable bot: off by default, and the local files
# ---------------------------------------------------------------------------

def test_off_by_default_in_the_shareable_settings(monkeypatch, tmp_path):
    from bot.settings import load_settings as load_bot_settings, SettingsError
    for k in list(__import__("os").environ):
        if k.startswith("SAVINGS_"):
            monkeypatch.delenv(k)
    key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"   # public Hardhat key, no funds
    monkeypatch.setenv("WALLET_PRIVATE_KEY", key)
    s = load_bot_settings(tmp_path / "missing.env")
    assert s.savings.mode == "off"
    assert key[2:] not in repr(s) and "savings_mode='off'" in repr(s)
    monkeypatch.setenv("SAVINGS_VAULT_MODE", "on")
    monkeypatch.setenv("SAVINGS_VAULT_ADDRESS", "0x" + "11" * 20)
    with pytest.raises(SettingsError, match="Savings Vault setting problem"):
        load_bot_settings(tmp_path / "missing.env")


def test_local_files_get_the_savings_log_and_cycle_columns(tmp_path):
    import csv
    from bot.local_store import LocalStore
    from bot.live_orchestrator import LiveCycleRecord
    from bot.live_proposals import LIVE_CYCLE_LOG_HEADERS
    # a cycle_log.csv written by the previous version (11 columns)
    old_header = LIVE_CYCLE_LOG_HEADERS[:11]
    crlf = chr(13) + chr(10)
    (tmp_path / "cycle_log.csv").write_bytes((",".join(old_header) + crlf + "2026-11-01 00:00:00,1,RANGING,"
                                              "90000,200,0,0,0,No,200,old row" + crlf).encode("utf-8"))
    store = LocalStore(tmp_path)
    store.append_cycle_log(LiveCycleRecord(candle_index=2, timestamp=NOW, close=90_000.0, regime_label="RANGING",
                                           vault_usdc=5.0, tb_position_value=0.0, rh_position_value=0.0,
                                           combined_equity=200.0, tb_open_positions=0, rh_open_position=False,
                                           warmup_done=True, actions=["x"], savings_value=195.0,
                                           savings_yield=0.1234))
    rows = list(csv.reader((tmp_path / "cycle_log.csv").open(encoding="utf-8", newline="")))
    assert rows[0] == LIVE_CYCLE_LOG_HEADERS
    assert rows[1][-1] == "old row" and len(rows[1]) == 11            # the old row is untouched
    assert rows[2][-2:] == ["195.0", "0.1234"]
    LocalStore(tmp_path)                                               # a restart doesn't touch it again
    assert list(csv.reader((tmp_path / "cycle_log.csv").open(encoding="utf-8", newline=""))) == rows
    store.append_savings_log([{"Action": "DEPOSIT", "Amount ($)": 195.0, "Status": "OK", "Tx Hash": "0xabc"}])
    log_rows = list(csv.reader((tmp_path / "savings_log.csv").open(encoding="utf-8", newline="")))
    assert log_rows[0][3:6] == ["Action", "Amount ($)", "Status"]
    assert log_rows[1][3:7] == ["DEPOSIT", "195.0", "OK", "0xabc"]
