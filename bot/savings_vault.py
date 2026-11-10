"""
WARNING -- WHITELISTED WALLETS ONLY. This optional feature is OFF by default
and must stay off unless the Savings Vault's developers (TakafulDAO) have
registered YOUR wallet as a member of the vault. The vault only accepts
deposits for registered members: with any other wallet every deposit is
refused on-chain, and the bot reports "this wallet is not a registered
member of the vault" and deposits nothing. Ask the vault's developers first.

The REAL Savings Vault: idle USDC is deposited into TakafulDAO's SFVault and
taken back out when a trade needs it. Off by default (SAVINGS_VAULT_MODE=off).

Not to be confused with the vault_* modules (a model that moves no money) or
LiveVault in live_portfolio.py (the wallet's USDC balance, an old name).

What the contract does (read from its verified source, SFVault.sol, and the
circuit breaker SFAndIFCircuitBreaker.sol; implementation
0x085ED6685Fd12507F7605E226DfEE996753397Ed behind the proxy below):
  - deposit(uint256 assets, address receiver): an ERC-4626 deposit of USDC.
    Only registered members may receive shares (maxDeposit is 0 for anyone
    else). Needs a USDC allowance for the vault. No fees today
    (management and performance fee 0); a $1,000,000 total cap.
  - withdraw(uint256 assets, address receiver, address owner): burns shares
    for `assets` of USDC-equivalent value, paid from the vault's idle USDC,
    then its idle USDT, then the Uniswap position. The payout can be partly
    USDT (USD₮0): the strategy sends both tokens without converting. So after
    a withdrawal the bot swaps any USDT to USDC (0.01% USDC/USDT pool).
  - redeem(uint256 shares, ...): the same by shares, used to empty it.
  - A circuit breaker checks every withdrawal: per-user and vault-wide
    24-hour caps ($8,000 and $10,000 on 2026-10-10), and an optional
    "large withdrawal" threshold (0 = off) that queues a request for manual
    approval. Going over a cap PAUSES THE WHOLE VAULT for every member and
    pays nothing. So before every withdrawal this module reads the caps and
    refuses unless the amount fits with a safety margin, and it never splits
    a withdrawal to get under the approval threshold.
  - Shares can't be transferred; there is no withdrawal delay unless the
    team turns the approval threshold on.

Safety rules built in:
  - Only the vault address below; SAVINGS_VAULT_ADDRESS, if set, must match
    it or the feature refuses to start. Every transaction this module sends
    is checked against a fixed list of contracts and approval spenders.
  - Exact approvals only: approve(amount) right before the deposit (or swap)
    and back to 0 if it didn't go through.
  - Every transaction's receipt is checked, and the vault's own Deposit /
    Withdraw events and the token transfers in it are what the books use.
    A transaction not mined in time is remembered and resolved next cycle;
    nothing else is sent while one is unresolved.
  - If the vault's code is upgraded (the proxy points elsewhere), deposits
    stop and an alert is raised; withdrawals stay allowed (the way out).
  - Never stops the trading bot: every failure is caught and reported.

Modes (SAVINGS_VAULT_MODE): off | dry_run | on | withdraw_all.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from eth_abi import encode as abi_encode

from .live_config import NetworkConfig
from .live_wallet import (
    _selector, _event_topic0, _rpc_call, simulate_call, build_and_sign_transaction, broadcast_transaction,
    get_eth_balance, get_gas_price, receipt_succeeded, encode_erc20_approve, encode_exact_input_single,
    LiveWalletError, SimulationRevertedError,
)

log = logging.getLogger("lifedao-bot.savings_vault")

# ---------------------------------------------------------------------------
# Addresses (Arbitrum One), all verified on-chain 2026-11-10
# ---------------------------------------------------------------------------
SAVINGS_VAULT_ADDRESS = "0x6d1bD94Cd572e51Aa018aE6Bf909BA61F8F8d4f9"     # SFVault proxy ("TLDSaveVault", TLDSV)
EXPECTED_IMPLEMENTATION = "0x085ED6685Fd12507F7605E226DfEE996753397Ed"   # SFVault code behind the proxy
ADDRESS_MANAGER = "0x0353E6d4Bb44e81B4350BAeEFE6499e7CcA64178"           # where the vault looks up its circuit breaker
USDT_ADDRESS = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"              # USD₮0, 6 decimals
STABLE_POOL = "0xbE3aD6a5669Dc0B8b12FeBC03608860C31E2eef6"               # Uniswap V3 USDC/USDT, fee 0.01%
STABLE_POOL_FEE = 100
ETH_USDC_POOL = "0xC6962004f452bE9203591991D15f6b388e09E8D0"             # Uniswap V3 WETH/USDC 0.05%, for gas in $
EIP1967_IMPLEMENTATION_SLOT = 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc
REAL_VAULT_LAUNCH_TS = int(datetime(2026, 2, 9, tzinfo=timezone.utc).timestamp())   # share price 1.0

MODES = ("off", "dry_run", "on", "withdraw_all")

# Guards
RATE_LIMIT_MARGIN_USD = 500.0     # leave this much of the vault-wide 24h cap for others (they may withdraw first)
WITHDRAW_EXTRA_PCT = 0.3          # withdraw 0.3% more than the shortfall: covers USDT valuation and swap fee
MAX_USDT_DEPEG_PCT = 0.5          # don't swap USDT->USDC if 1 USDT is worth under 0.995 USDC
USDT_SWAP_SLIPPAGE_PCT = 0.3
MIN_USDT_TO_SWAP_RAW = 10_000     # 0.01 USDT
RECEIPT_TIMEOUT_SECONDS = 180.0
STUCK_ALERT_SECONDS = 3600
ALERT_REPEAT_SECONDS = 24 * 3600
PRICE_HISTORY_SECONDS = 8 * 86400

# Typical gas (measured on real vault transactions, Nov 2026): deposit ~1.06M,
# withdraw from the Uniswap position ~1.6-2.6M.
GAS_APPROVE = 60_000
GAS_DEPOSIT = 1_250_000
GAS_WITHDRAW = 2_700_000
GAS_SWAP = 200_000
GAS_ROUND_TRIP = GAS_APPROVE * 2 + GAS_DEPOSIT + GAS_WITHDRAW + GAS_SWAP + GAS_APPROVE * 2

SEL_TOTAL_ASSETS = _selector("totalAssets()")
SEL_ASSET = _selector("asset()")
SEL_PAUSED = _selector("paused()")
SEL_BALANCE_OF = _selector("balanceOf(address)")
SEL_ALLOWANCE = _selector("allowance(address,address)")
SEL_CONVERT_TO_ASSETS = _selector("convertToAssets(uint256)")
SEL_CONVERT_TO_SHARES = _selector("convertToShares(uint256)")
SEL_MAX_DEPOSIT = _selector("maxDeposit(address)")
SEL_MAX_WITHDRAW = _selector("maxWithdraw(address)")
SEL_MAX_REDEEM = _selector("maxRedeem(address)")
SEL_PREVIEW_DEPOSIT = _selector("previewDeposit(uint256)")
SEL_PREVIEW_REDEEM = _selector("previewRedeem(uint256)")
SEL_DEPOSIT = _selector("deposit(uint256,address)")
SEL_WITHDRAW = _selector("withdraw(uint256,address,address)")
SEL_REDEEM = _selector("redeem(uint256,address,address)")
SEL_SLOT0 = _selector("slot0()")
SEL_GET_PROTOCOL_ADDRESS = _selector("getProtocolAddressByName(string)")
SEL_CB_CONFIG = _selector("config(address)")
SEL_CB_PROTECTION = _selector("protectionEnabled(address)")
SEL_CB_GLOBAL_WINDOW = _selector("getGlobalWindowState(address)")
SEL_CB_USER_WINDOW = _selector("getUserWindowState(address,address)")
SEL_CB_PAUSE_FLAGS = _selector("getPauseFlags(address)")
SEL_CB_PENDING_REQUEST = _selector("pendingWithdrawalRequestId(address,address)")
SEL_APPROVE = _selector("approve(address,uint256)")
SEL_EXACT_INPUT_SINGLE = _selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")

TOPIC_DEPOSIT = _event_topic0("Deposit(address,address,uint256,uint256)")
TOPIC_WITHDRAW = _event_topic0("Withdraw(address,address,address,uint256,uint256)")
TOPIC_TRANSFER = _event_topic0("Transfer(address,address,uint256)")

USD = 10 ** 6       # USDC, USDT and vault shares all have 6 decimals
MAX_UINT = 2 ** 256 - 1


class SavingsConfigError(ValueError):
    pass


class SavingsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Settings (environment variables only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SavingsSettings:
    mode: str = "off"
    max_deposit_usd: float = 250.0      # at most this much in one deposit
    min_deposit_usd: float = 50.0       # never deposit less than this
    cash_buffer_usd: float = 5.0        # USDC always left in the wallet (rounding, swap dust)
    min_eth: float = 0.0005             # no vault transaction with less ETH than this for gas
    expected_idle_days: float = 30.0    # a deposit must earn its round-trip gas back in this many days


def _env_float(environ, name: str, default: float, minimum: float = 0.0) -> float:
    raw = environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SavingsConfigError(f"{name} must be a number, got {raw!r}")
    if not math.isfinite(value) or value < minimum:
        raise SavingsConfigError(f"{name} must be at least {minimum}, got {raw!r}")
    return value


def load_settings(environ) -> SavingsSettings:
    """Reads the SAVINGS_* variables. Raises SavingsConfigError on anything
    wrong, including a vault address other than the one this bot allows."""
    mode = str(environ.get("SAVINGS_VAULT_MODE", "off")).strip().lower()
    if mode not in MODES:
        raise SavingsConfigError(f"SAVINGS_VAULT_MODE must be one of {', '.join(MODES)}; got {mode!r}")
    addr = str(environ.get("SAVINGS_VAULT_ADDRESS", "")).strip()
    if addr and addr.lower() != SAVINGS_VAULT_ADDRESS.lower():
        raise SavingsConfigError(f"SAVINGS_VAULT_ADDRESS {addr} is not the allowed Savings Vault "
                                 f"{SAVINGS_VAULT_ADDRESS}; refusing to use it")
    s = SavingsSettings(
        mode=mode,
        max_deposit_usd=_env_float(environ, "SAVINGS_MAX_DEPOSIT_USD", 250.0, 1.0),
        min_deposit_usd=_env_float(environ, "SAVINGS_MIN_DEPOSIT_USD", 50.0, 1.0),
        cash_buffer_usd=_env_float(environ, "SAVINGS_CASH_BUFFER_USD", 5.0, 0.0),
        min_eth=_env_float(environ, "SAVINGS_MIN_ETH", 0.0005, 0.0),
        expected_idle_days=_env_float(environ, "SAVINGS_EXPECTED_IDLE_DAYS", 30.0, 1.0),
    )
    if s.min_deposit_usd > s.max_deposit_usd:
        raise SavingsConfigError("SAVINGS_MIN_DEPOSIT_USD can't be above SAVINGS_MAX_DEPOSIT_USD")
    return s


# ---------------------------------------------------------------------------
# The chain (real RPC). Tests replace it with a fake.
# ---------------------------------------------------------------------------

class RpcChain:
    """Reads (eth_call) from the wallet's address, and sends signed
    transactions. The private key stays inside this object and the signing
    helper; it is never logged or returned."""

    def __init__(self, network: NetworkConfig, wallet_address: str, private_key: Optional[str]):
        self.network = network
        self.rpc_urls = list(network.rpc_urls)
        self.wallet = wallet_address
        self._key = private_key

    def call(self, to: str, data: bytes) -> bytes:
        return simulate_call(self.rpc_urls, self.wallet, to, data)

    def storage_at(self, address: str, slot: int) -> int:
        return int(_rpc_call(self.rpc_urls, "eth_getStorageAt", [address, hex(slot), "latest"]), 16)

    def eth_balance(self) -> int:
        return get_eth_balance(self.rpc_urls, self.wallet)

    def gas_price(self) -> int:
        return get_gas_price(self.rpc_urls)

    def send(self, to: str, data: bytes) -> str:
        if not self._key:
            raise SavingsError("no private key: this run can't send transactions")
        signed = build_and_sign_transaction(self.rpc_urls, self.network.chain_id, self._key, to, data)
        return broadcast_transaction(self.rpc_urls, signed.raw_hex)

    def receipt(self, tx_hash: str) -> Optional[dict]:
        return _rpc_call(self.rpc_urls, "eth_getTransactionReceipt", [tx_hash])

    def wait(self, tx_hash: str, timeout: float = RECEIPT_TIMEOUT_SECONDS, poll: float = 3.0) -> Optional[dict]:
        deadline = time.time() + timeout
        while True:
            try:
                r = self.receipt(tx_hash)
            except LiveWalletError:
                r = None
            if r is not None:
                return r
            if time.time() >= deadline:
                return None
            time.sleep(poll)


# ---------------------------------------------------------------------------
# Encoding / decoding helpers (pure)
# ---------------------------------------------------------------------------

def _addr_word(a: str) -> bytes:
    return abi_encode(["address"], [a])


def _uint_word(n: int) -> bytes:
    return abi_encode(["uint256"], [n])


def _words(raw: bytes) -> list[int]:
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def encode_deposit(assets_raw: int, receiver: str) -> bytes:
    return SEL_DEPOSIT + abi_encode(["uint256", "address"], [assets_raw, receiver])


def encode_withdraw(assets_raw: int, receiver: str, owner: str) -> bytes:
    return SEL_WITHDRAW + abi_encode(["uint256", "address", "address"], [assets_raw, receiver, owner])


def encode_redeem(shares: int, receiver: str, owner: str) -> bytes:
    return SEL_REDEEM + abi_encode(["uint256", "address", "address"], [shares, receiver, owner])


def _topic_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _log_words(lg: dict) -> list[int]:
    data = lg["data"][2:] if lg["data"].startswith("0x") else lg["data"]
    return _words(bytes.fromhex(data))


def find_vault_event(receipt: dict, topic0: str, owner: str) -> Optional[tuple[int, int]]:
    """(assets, shares) from the vault's Deposit or Withdraw event for
    `owner` in a receipt, or None."""
    for lg in receipt.get("logs", []):
        if lg["address"].lower() != SAVINGS_VAULT_ADDRESS.lower() or not lg["topics"]:
            continue
        if lg["topics"][0].lower() != topic0.lower():
            continue
        if _topic_addr(lg["topics"][-1]) != owner.lower():      # owner is the last indexed topic in both events
            continue
        w = _log_words(lg)
        return w[0], w[1]
    return None


def token_transfers(receipt: dict, token: str, to: Optional[str] = None, frm: Optional[str] = None) -> int:
    """Sum of `token` Transfer amounts in a receipt, filtered by recipient/sender."""
    total = 0
    for lg in receipt.get("logs", []):
        if lg["address"].lower() != token.lower() or len(lg["topics"]) < 3:
            continue
        if lg["topics"][0].lower() != TOPIC_TRANSFER.lower():
            continue
        if to and _topic_addr(lg["topics"][2]) != to.lower():
            continue
        if frm and _topic_addr(lg["topics"][1]) != frm.lower():
            continue
        total += _log_words(lg)[0]
    return total


def gas_cost_wei(receipt: dict) -> int:
    return int(receipt.get("gasUsed", "0x0"), 16) * int(receipt.get("effectiveGasPrice", "0x0"), 16)


def annualised(growth: float, seconds: float) -> Optional[float]:
    if seconds <= 0 or growth <= 0:
        return None
    return growth ** (365 * 86400 / seconds) - 1


# ---------------------------------------------------------------------------
# Status of the vault for this wallet
# ---------------------------------------------------------------------------

@dataclass
class VaultStatus:
    shares: int = 0
    value_raw: int = 0                  # convertToAssets(shares), USDC-equivalent
    share_price: float = 0.0
    paused: bool = False
    member: bool = False
    max_deposit_raw: int = 0
    max_withdraw_raw: int = 0
    implementation: str = ""
    implementation_ok: bool = False
    cb_address: str = ""
    cb_readable: bool = False
    cb_pause_flags: int = 0
    cb_protection: bool = False
    approval_threshold_raw: int = 0     # 0 = off
    global_remaining_raw: int = 0
    user_remaining_raw: int = 0
    pending_request_id: int = 0
    wallet_usdc_raw: int = 0
    wallet_usdt_raw: int = 0
    usdt_price: float = 1.0             # USDC per USDT, from the 0.01% pool
    eth_wei: int = 0
    eth_usd: float = 0.0
    gas_price_wei: int = 0
    read_at: int = 0

    @property
    def value_usd(self) -> float:
        return self.value_raw / USD


# ---------------------------------------------------------------------------
# The vault client
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _fresh_state() -> dict:
    return {"version": 1, "shares": 0, "deposited_usd": 0.0, "withdrawn_usd": 0.0, "gas_usd": 0.0,
            "conversion_cost_usd": 0.0, "external_usd": 0.0, "pending": None, "price_history": [],
            "alerts": {}, "last_alert": "", "last_alert_ts": 0}


class SavingsVault:
    def __init__(self, chain, wallet_address: str, settings: SavingsSettings, network: NetworkConfig,
                 state: Optional[dict] = None, clock=_now):
        self.chain = chain
        self.wallet = wallet_address
        self.settings = settings
        self.network = network
        self.clock = clock
        self.state = {**_fresh_state(), **(state or {})}
        self.status: Optional[VaultStatus] = None
        self.deposits_blocked_reason = ""        # set by checks that stop deposits (not withdrawals)
        self.log_rows: list[dict] = []           # rows for the "Savings Vault Log" tab, flushed by the caller
        self.cycle_index: Optional[int] = None

    # ---- properties the wallet / orchestrator read ----------------------
    @property
    def mode(self) -> str:
        return self.settings.mode

    @property
    def can_send(self) -> bool:
        return self.mode in ("on", "withdraw_all")

    @property
    def value_usd(self) -> float:
        return self.status.value_usd if self.status else 0.0

    @property
    def wallet_usdt_usd(self) -> float:
        if not self.status:
            return 0.0
        price = self.status.usdt_price if self.status.usdt_price > 0 else 1.0   # unreadable: count it at $1
        return self.status.wallet_usdt_raw / USD * min(1.0, price)

    @property
    def spendable_usd(self) -> float:
        """How much of the vault balance a trade may count on this cycle."""
        if not self.status or self.mode != "on":
            return 0.0
        return self.withdrawable_raw() / USD

    def yield_usd(self) -> float:
        """Lifetime earnings: what's in the vault now plus what came out,
        minus what went in (outside deposits/withdrawals included)."""
        return self.value_usd + self.state["withdrawn_usd"] - self.state["deposited_usd"] - self.state["external_usd"]

    def costs_usd(self) -> float:
        return self.state["gas_usd"] + self.state["conversion_cost_usd"]

    def apy(self) -> Optional[float]:
        """Recent yield of the vault itself, from its share price: over the
        last 7 days when the bot has watched that long, else since launch."""
        hist = self.state.get("price_history") or []
        now_price = self.status.share_price if self.status else None
        if now_price and hist:
            old = [h for h in hist if h[0] <= self.clock() - 7 * 86400]
            first = old[-1] if old else hist[0]
            if self.clock() - first[0] >= 3 * 86400 and first[1] > 0:
                return annualised(now_price / first[1], self.clock() - first[0])
        if now_price:
            return annualised(now_price, self.clock() - REAL_VAULT_LAUNCH_TS)
        return None

    def to_state(self) -> dict:
        return dict(self.state)

    # ---- reading ---------------------------------------------------------
    def _call_words(self, to: str, data: bytes) -> list[int]:
        return _words(self.chain.call(to, data))

    def _token_balance(self, token: str) -> int:
        return self._call_words(token, SEL_BALANCE_OF + _addr_word(self.wallet))[0]

    def _allowance(self, token: str, spender: str) -> int:
        return self._call_words(token, SEL_ALLOWANCE + _addr_word(self.wallet) + _addr_word(spender))[0]

    def _pool_price(self, pool: str) -> float:
        sqrt_p = self._call_words(pool, SEL_SLOT0)[0]
        return (sqrt_p / 2 ** 96) ** 2

    def refresh_light(self) -> VaultStatus:
        """The wallet's shares and their value, and its USDT. Three reads."""
        st = self.status or VaultStatus()
        st.shares = self._call_words(SAVINGS_VAULT_ADDRESS, SEL_BALANCE_OF + _addr_word(self.wallet))[0]
        st.value_raw = (self._call_words(SAVINGS_VAULT_ADDRESS, SEL_CONVERT_TO_ASSETS + _uint_word(st.shares))[0]
                        if st.shares else 0)
        st.wallet_usdt_raw = self._token_balance(USDT_ADDRESS)
        self.status = st
        self._reconcile_shares(st.shares)
        return st

    def refresh(self) -> VaultStatus:
        """Everything the guards need, read fresh."""
        st = VaultStatus(read_at=self.clock())
        v = SAVINGS_VAULT_ADDRESS
        st.shares = self._call_words(v, SEL_BALANCE_OF + _addr_word(self.wallet))[0]
        st.value_raw = self._call_words(v, SEL_CONVERT_TO_ASSETS + _uint_word(st.shares))[0] if st.shares else 0
        st.share_price = self._call_words(v, SEL_CONVERT_TO_ASSETS + _uint_word(USD))[0] / USD
        st.paused = bool(self._call_words(v, SEL_PAUSED)[0])
        st.max_deposit_raw = self._call_words(v, SEL_MAX_DEPOSIT + _addr_word(self.wallet))[0]
        st.member = st.max_deposit_raw > 0          # maxDeposit is 0 for non-members (SFVault.sol)
        st.max_withdraw_raw = self._call_words(v, SEL_MAX_WITHDRAW + _addr_word(self.wallet))[0]
        impl = self.chain.storage_at(v, EIP1967_IMPLEMENTATION_SLOT)
        st.implementation = "0x" + format(impl, "040x")
        st.implementation_ok = st.implementation.lower() == EXPECTED_IMPLEMENTATION.lower()
        st.wallet_usdc_raw = self._token_balance(self.network.usdc_address)
        st.wallet_usdt_raw = self._token_balance(USDT_ADDRESS)
        st.eth_wei = self.chain.eth_balance()
        st.gas_price_wei = self.chain.gas_price()
        try:
            st.usdt_price = 1.0 / self._pool_price(STABLE_POOL)          # pool price is USDT per USDC
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't read the USDC/USDT price (%s); treating USDT as unswappable this cycle", e)
            st.usdt_price = 0.0
        try:
            st.eth_usd = self._pool_price(ETH_USDC_POOL) * 1e12           # WETH 18 dp, USDC 6 dp
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't read the ETH price (%s)", e)
            st.eth_usd = 0.0
        self._read_circuit_breaker(st)
        self.status = st
        self._reconcile_shares(st.shares)
        return st

    def _read_circuit_breaker(self, st: VaultStatus) -> None:
        """The vault asks its AddressManager for the circuit breaker at every
        withdrawal; read it the same way. Any read failure leaves
        cb_readable False, which blocks withdrawals (fail closed)."""
        try:
            name = "PROTOCOL__CIRCUIT_BREAKER"
            arg = abi_encode(["string"], [name])
            w = self._call_words(ADDRESS_MANAGER, SEL_GET_PROTOCOL_ADDRESS + arg)
            st.cb_address = "0x" + format(w[1], "040x")
            if int(st.cb_address, 16) == 0:
                # No breaker: the vault proceeds without checks.
                st.cb_readable, st.global_remaining_raw, st.user_remaining_raw = True, MAX_UINT, MAX_UINT
                return
            cb, va = st.cb_address, _addr_word(SAVINGS_VAULT_ADDRESS)
            cfg = self._call_words(cb, SEL_CB_CONFIG + va)            # global cap, user cap, approval threshold, enabled
            st.cb_protection = bool(self._call_words(cb, SEL_CB_PROTECTION + va)[0]) and bool(cfg[3])
            st.approval_threshold_raw = cfg[2] if st.cb_protection else 0
            gw = self._call_words(cb, SEL_CB_GLOBAL_WINDOW + va)     # start, withdrawn, cap, remaining, resetsAt
            uw = self._call_words(cb, SEL_CB_USER_WINDOW + va + _addr_word(self.wallet))
            st.global_remaining_raw = gw[3] if st.cb_protection else MAX_UINT
            st.user_remaining_raw = uw[3] if st.cb_protection else MAX_UINT
            st.cb_pause_flags = self._call_words(cb, SEL_CB_PAUSE_FLAGS + va)[0]
            st.pending_request_id = self._call_words(cb, SEL_CB_PENDING_REQUEST + va + _addr_word(self.wallet))[0]
            st.cb_readable = True
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't read the vault's circuit breaker (%s); withdrawals blocked this cycle", e)
            st.cb_readable = False

    def withdrawable_raw(self, st: Optional[VaultStatus] = None) -> int:
        """The most this wallet may withdraw right now without tripping any
        circuit-breaker rule (0 if anything is off)."""
        st = st or self.status
        if st is None or st.shares == 0 or self.withdraw_blocked_reason(st):
            return 0
        margin = int(RATE_LIMIT_MARGIN_USD * USD)
        limit = min(st.value_raw, st.max_withdraw_raw, st.user_remaining_raw,
                    max(0, st.global_remaining_raw - margin) if st.global_remaining_raw != MAX_UINT else MAX_UINT)
        if st.approval_threshold_raw:
            limit = min(limit, st.approval_threshold_raw - 1)   # above it a request waits for manual approval
        return max(0, limit)

    def withdraw_blocked_reason(self, st: Optional[VaultStatus] = None) -> str:
        st = st or self.status
        if st is None:
            return "vault not read yet"
        if self.state.get("pending"):
            return "an earlier vault transaction is still unconfirmed"
        if st.paused:
            return "the vault is paused"
        if not st.cb_readable:
            return "couldn't read the vault's withdrawal limits"
        if st.cb_pause_flags:
            return f"the vault's circuit breaker has pause flags set ({st.cb_pause_flags})"
        if st.pending_request_id:
            return f"a withdrawal request (#{st.pending_request_id}) is waiting for the vault team's approval"
        if st.eth_wei < self.settings.min_eth * 1e18:
            return f"wallet ETH {st.eth_wei / 1e18:.6f} is below {self.settings.min_eth} for gas"
        return ""

    def deposit_blocked_reason(self, st: Optional[VaultStatus] = None) -> str:
        st = st or self.status
        if st is None:
            return "vault not read yet"
        if self.state.get("pending"):
            return "an earlier vault transaction is still unconfirmed"
        if not st.implementation_ok:
            return f"the vault's code was changed (implementation {st.implementation}); deposits stopped"
        if st.paused:
            return "the vault is paused"
        if not st.member:
            return "this wallet is not a registered member of the vault (maxDeposit is 0)"
        if st.eth_wei < self.settings.min_eth * 1e18:
            return f"wallet ETH {st.eth_wei / 1e18:.6f} is below {self.settings.min_eth} for gas"
        return ""

    # ---- books -----------------------------------------------------------
    def _reconcile_shares(self, onchain_shares: int) -> None:
        """Shares changed without the bot (e.g. the owner's script): book the
        difference at today's share price as an outside deposit/withdrawal,
        so the yield figure stays right."""
        known = self.state.get("shares", 0)
        if self.state.get("pending") or onchain_shares == known:
            return
        price = self.status.share_price if self.status and self.status.share_price else None
        if price is None:
            try:
                price = self._call_words(SAVINGS_VAULT_ADDRESS, SEL_CONVERT_TO_ASSETS + _uint_word(USD))[0] / USD
            except Exception:  # noqa: BLE001
                return
        delta_usd = (onchain_shares - known) / USD * price
        self.state["external_usd"] += delta_usd
        self.state["shares"] = onchain_shares
        self._row("EXTERNAL_CHANGE", delta_usd, "OK",
                  f"vault shares changed outside the bot ({known} -> {onchain_shares}); booked ${delta_usd:,.2f} "
                  f"as an outside {'deposit' if delta_usd > 0 else 'withdrawal'}")

    def _book_gas(self, receipt: Optional[dict]) -> None:
        if receipt and self.status and self.status.eth_usd:
            self.state["gas_usd"] += gas_cost_wei(receipt) / 1e18 * self.status.eth_usd

    # ---- reporting -------------------------------------------------------
    def _row(self, action: str, amount_usd: float, status: str, notes: str, tx: str = "") -> None:
        row = {"Timestamp (UTC)": datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
               "Cycle #": "" if self.cycle_index is None else self.cycle_index, "Mode": self.mode,
               "Action": action, "Amount ($)": round(amount_usd, 2), "Status": status, "Tx Hash": tx,
               "Vault Balance ($)": round(self.value_usd, 2), "Notes": notes}
        self.log_rows.append(row)
        (log.error if status in ("ALERT", "FAILED") else log.info)("Savings vault %s %s: %s", action, status, notes)

    def alert(self, key: str, message: str, actions: Optional[list] = None) -> None:
        """An alert goes to the log (ERROR), the Savings Vault Log tab and the
        cycle notes, at most once a day per kind so it doesn't flood."""
        last = self.state["alerts"].get(key, 0)
        if self.clock() - last < ALERT_REPEAT_SECONDS:
            return
        self.state["alerts"][key] = self.clock()
        self.state["last_alert"], self.state["last_alert_ts"] = message, self.clock()
        self._row("ALERT", 0.0, "ALERT", message)
        if actions is not None:
            actions.append(f"SAVINGS ALERT: {message}")

    # ---- sending (the only place transactions leave) ---------------------
    def _check_outgoing(self, to: str, data: bytes) -> None:
        """Hard allow-list: the vault, USDC/USDT approvals to the vault or the
        router only, and the router's USDT->USDC swap."""
        sel, to_l = data[:4], to.lower()
        if to_l == SAVINGS_VAULT_ADDRESS.lower():
            if sel not in (SEL_DEPOSIT, SEL_WITHDRAW, SEL_REDEEM):
                raise SavingsError("refusing an unexpected vault call")
            return
        if to_l in (self.network.usdc_address.lower(), USDT_ADDRESS.lower()) and sel == SEL_APPROVE:
            spender = "0x" + data[4:36].hex()[-40:]
            allowed = SAVINGS_VAULT_ADDRESS if to_l == self.network.usdc_address.lower() else self.network.router_address
            if spender.lower() != allowed.lower():
                raise SavingsError(f"refusing an approval to {spender}")
            return
        if to_l == self.network.router_address.lower() and sel == SEL_EXACT_INPUT_SINGLE:
            return
        raise SavingsError(f"refusing a transaction to {to}: not on the Savings Vault allow-list")

    def _send_and_wait(self, kind: str, to: str, data: bytes, amount_usd: float, actions: list) -> Optional[dict]:
        """Send one transaction and wait for it. Returns the receipt (check its
        status), or None if it wasn't mined in time: then it's remembered as
        pending and resolved at the next cycle."""
        self._check_outgoing(to, data)
        tx_hash = self.chain.send(to, data)
        receipt = self.chain.wait(tx_hash)
        if receipt is None:
            self.state["pending"] = {"hash": tx_hash, "kind": kind, "amount_usd": amount_usd, "sent_at": self.clock()}
            self._row(kind.upper(), amount_usd, "PENDING", "not confirmed after "
                      f"{RECEIPT_TIMEOUT_SECONDS:.0f}s; will be checked next cycle", tx_hash)
            self.alert("pending", f"{kind} transaction {tx_hash} not confirmed yet; no more vault transactions "
                       f"until it is", actions)
            return None
        self._book_gas(receipt)
        return receipt

    def _approve_exact(self, token: str, spender: str, amount_raw: int, actions: list) -> bool:
        """Set the allowance to exactly `amount_raw` (0 first if it holds a
        different non-zero value, as some tokens require)."""
        current = self._allowance(token, spender)
        if current == amount_raw:
            return True
        if current != 0 and amount_raw != 0:
            r = self._send_and_wait("approve", token, encode_erc20_approve(spender, 0), 0.0, actions)
            if r is None or not receipt_succeeded(r):
                return False
        r = self._send_and_wait("approve", token, encode_erc20_approve(spender, amount_raw), amount_raw / USD, actions)
        return r is not None and receipt_succeeded(r)

    def _clear_allowance(self, token: str, spender: str, actions: list) -> None:
        try:
            if self._allowance(token, spender) != 0:
                self._approve_exact(token, spender, 0, actions)
        except Exception as e:  # noqa: BLE001
            self.alert("allowance", f"couldn't reset the {token} approval for {spender} to 0: {e}", actions)

    # ---- the cycle -------------------------------------------------------
    def begin_cycle(self, cycle_index: Optional[int], actions: list) -> None:
        """Start of each 4-hour cycle: resolve an unconfirmed transaction,
        read everything fresh, record the share price, raise alerts."""
        self.cycle_index = cycle_index
        self._resolve_pending(actions)
        st = self.refresh()
        hist = [h for h in self.state["price_history"] if h[0] >= self.clock() - PRICE_HISTORY_SECONDS]
        hist.append([self.clock(), st.share_price])
        self.state["price_history"] = hist
        if self.mode in ("on", "dry_run") and not st.implementation_ok:
            self.alert("implementation", f"the Savings Vault's code changed (now {st.implementation}, expected "
                       f"{EXPECTED_IMPLEMENTATION}): no more deposits until you review it", actions)
        if self.mode in ("on", "dry_run") and not st.member:
            self.alert("member", "this wallet isn't a registered member of the Savings Vault: deposits will fail; "
                       "ask the vault team to whitelist it", actions)
        if st.shares and st.paused:
            self.alert("paused", "the Savings Vault is paused: money in it can't be withdrawn for trades until "
                       "the vault team unpauses it", actions)
        if st.shares and st.pending_request_id:
            self.alert("request", f"withdrawal request #{st.pending_request_id} waits for the vault team's "
                       "approval", actions)
        if self.can_send and st.eth_wei < self.settings.min_eth * 1e18:
            self.alert("eth", f"wallet ETH {st.eth_wei / 1e18:.6f} is below {self.settings.min_eth}: top up ETH "
                       "for gas; vault transactions are paused", actions)

    def _resolve_pending(self, actions: list) -> None:
        p = self.state.get("pending")
        if not p:
            return
        try:
            receipt = self.chain.receipt(p["hash"])
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't check pending vault transaction %s: %s", p["hash"], e)
            return
        if receipt is None:
            if self.clock() - p["sent_at"] > STUCK_ALERT_SECONDS:
                self.alert("stuck", f"{p['kind']} transaction {p['hash']} still unconfirmed after "
                           f"{(self.clock() - p['sent_at']) / 3600:.1f}h; check it on arbiscan.io", actions)
            return
        self.state["pending"] = None
        self._book_gas(receipt)
        ok = receipt_succeeded(receipt)
        if ok and p["kind"] == "deposit":
            ev = find_vault_event(receipt, TOPIC_DEPOSIT, self.wallet)
            if ev:
                self.state["deposited_usd"] += ev[0] / USD
                self.state["shares"] += ev[1]
        elif ok and p["kind"] in ("withdraw", "redeem"):
            ev = find_vault_event(receipt, TOPIC_WITHDRAW, self.wallet)
            if ev:
                self.state["withdrawn_usd"] += ev[0] / USD
                self.state["shares"] -= ev[1]
        self._row(p["kind"].upper(), p["amount_usd"], "OK" if ok else "FAILED",
                  "confirmed late" if ok else "reverted (found late)", p["hash"])
        if not ok:
            self.alert("reverted", f"{p['kind']} transaction {p['hash']} reverted", actions)

    # ---- USDT -> USDC ------------------------------------------------------
    def convert_usdt(self, actions: list) -> float:
        """Swap any USDT in the wallet (from a withdrawal) to USDC. Returns
        the USDC received."""
        usdt = self._token_balance(USDT_ADDRESS)
        if usdt < MIN_USDT_TO_SWAP_RAW:
            return 0.0
        if self.mode == "dry_run":
            self._row("SWAP_USDT", usdt / USD, "DRY_RUN", f"would swap {usdt / USD:,.6f} USDT to USDC")
            return 0.0
        price = 1.0 / self._pool_price(STABLE_POOL)                    # USDC per USDT
        if price < 1 - MAX_USDT_DEPEG_PCT / 100:
            self.alert("depeg", f"1 USDT = {price:.4f} USDC: not swapping {usdt / USD:,.2f} USDT until it's back "
                       "near $1", actions)
            return 0.0
        router = self.network.router_address
        min_out = int(usdt * min(price, 1.0) * (1 - USDT_SWAP_SLIPPAGE_PCT / 100))
        if not self._approve_exact(USDT_ADDRESS, router, usdt, actions):
            self.alert("swap", "couldn't approve USDT for the USDC swap; USDT stays in the wallet", actions)
            return 0.0
        data = encode_exact_input_single(USDT_ADDRESS, self.network.usdc_address, STABLE_POOL_FEE, self.wallet,
                                         usdt, min_out)
        r = self._send_and_wait("swap", router, data, usdt / USD, actions)
        if r is None:
            return 0.0
        if not receipt_succeeded(r):
            self._clear_allowance(USDT_ADDRESS, router, actions)
            self._row("SWAP_USDT", usdt / USD, "FAILED", "swap reverted; USDT stays in the wallet",
                      r.get("transactionHash", ""))
            self.alert("swap", "the USDT->USDC swap reverted; USDT stays in the wallet (retried next cycle)", actions)
            return 0.0
        got = token_transfers(r, self.network.usdc_address, to=self.wallet)
        self.state["conversion_cost_usd"] += max(0.0, (usdt - got) / USD)
        self._row("SWAP_USDT", usdt / USD, "OK", f"swapped {usdt / USD:,.6f} USDT for {got / USD:,.6f} USDC",
                  r.get("transactionHash", ""))
        actions.append(f"Savings: swapped {usdt / USD:,.2f} USDT from the vault to {got / USD:,.2f} USDC")
        return got / USD

    # ---- withdraw ----------------------------------------------------------
    def ensure_wallet_usdc(self, needed_usd: float, actions: list) -> float:
        """Before a buy of `needed_usd`: withdraw from the vault exactly what
        the wallet is short of (plus the cash buffer, +0.3% for the USDT
        swap). Returns the wallet's USDC afterwards. Raises SavingsError if
        the money can't be made available; the caller then doesn't trade."""
        wallet_usdc = self._token_balance(self.network.usdc_address)
        short_raw = int(math.ceil((needed_usd + self.settings.cash_buffer_usd) * USD)) - wallet_usdc
        if short_raw <= 0:
            return wallet_usdc / USD
        if self.mode == "dry_run":
            if self.status and self.status.shares:
                self._row("WITHDRAW", short_raw / USD, "DRY_RUN",
                          f"a ${needed_usd:,.2f} buy would withdraw ${short_raw / USD:,.2f} first")
            return wallet_usdc / USD
        if not self.can_send:
            return wallet_usdc / USD
        # USDT left over from an earlier withdrawal counts first
        if self._token_balance(USDT_ADDRESS) >= MIN_USDT_TO_SWAP_RAW:
            self._safe_convert(actions)
            wallet_usdc = self._token_balance(self.network.usdc_address)
            short_raw = int(math.ceil((needed_usd + self.settings.cash_buffer_usd) * USD)) - wallet_usdc
            if short_raw <= 0:
                return wallet_usdc / USD
        amount_raw = int(math.ceil(short_raw * (1 + WITHDRAW_EXTRA_PCT / 100)))
        self.withdraw(amount_raw, actions, reason=f"for a ${needed_usd:,.2f} buy")
        self._safe_convert(actions)
        wallet_usdc = self._token_balance(self.network.usdc_address)
        return wallet_usdc / USD

    def _safe_convert(self, actions: list) -> None:
        try:
            self.convert_usdt(actions)
        except Exception as e:  # noqa: BLE001 -- USDT stays in the wallet and is retried next cycle
            self.alert("swap", f"couldn't swap the wallet's USDT to USDC ({e}); retried next cycle", actions)

    def withdraw(self, amount_raw: int, actions: list, reason: str = "") -> int:
        """withdraw(assets) for this wallet after every guard. Returns the
        USDC-equivalent the vault paid (USDC + USDT). Raises SavingsError."""
        st = self.refresh()
        blocked = self.withdraw_blocked_reason(st)
        if blocked:
            raise SavingsError(f"can't withdraw from the Savings Vault: {blocked}")
        allowed = self.withdrawable_raw(st)
        if amount_raw > allowed:
            if allowed < USD:      # under $1 isn't worth a withdrawal's gas
                raise SavingsError("can't withdraw from the Savings Vault: nothing withdrawable within its limits "
                                   f"(vault ${st.value_usd:,.2f}, per-user 24h room ${st.user_remaining_raw / USD:,.2f},"
                                   f" vault-wide room ${st.global_remaining_raw / USD:,.2f})")
            self.alert("partial", f"needed ${amount_raw / USD:,.2f} from the vault but only ${allowed / USD:,.2f} can "
                       "be withdrawn within its limits; the trade may be smaller or skipped", actions)
            amount_raw = allowed
        data = encode_withdraw(amount_raw, self.wallet, self.wallet)
        try:
            preview_shares = _words(self.chain.call(SAVINGS_VAULT_ADDRESS, data))[0]
        except SimulationRevertedError as e:
            raise SavingsError(f"the vault would refuse this withdrawal: {e}")
        if preview_shares == 0:
            # The circuit breaker would hold or block it instead of paying out.
            raise SavingsError("the vault's circuit breaker would not pay this withdrawal out; not sending it")
        r = self._send_and_wait("withdraw", SAVINGS_VAULT_ADDRESS, data, amount_raw / USD, actions)
        if r is None:
            raise SavingsError("the withdrawal wasn't confirmed in time (checked again next cycle)")
        tx = r.get("transactionHash", "")
        if not receipt_succeeded(r):
            self._row("WITHDRAW", amount_raw / USD, "FAILED", f"reverted {reason}", tx)
            self.alert("withdraw", f"a ${amount_raw / USD:,.2f} withdrawal reverted (tx {tx})", actions)
            raise SavingsError(f"the withdrawal reverted (tx {tx})")
        ev = find_vault_event(r, TOPIC_WITHDRAW, self.wallet)
        got_usdc = token_transfers(r, self.network.usdc_address, to=self.wallet)
        got_usdt = token_transfers(r, USDT_ADDRESS, to=self.wallet)
        if ev is None:
            self._row("WITHDRAW", amount_raw / USD, "ALERT", "succeeded but paid nothing", tx)
            self.alert("held", "a withdrawal went through but the vault paid nothing: its circuit breaker held it "
                       "(the vault may now be paused). Check with the vault team.", actions)
            raise SavingsError("the vault paid nothing for this withdrawal (circuit breaker)")
        assets, shares = ev
        self.state["withdrawn_usd"] += assets / USD
        self.state["shares"] -= shares
        paid_value = got_usdc + got_usdt * min(1.0, st.usdt_price or 1.0)
        if paid_value < assets * 0.995:
            self.alert("underpaid", f"withdrew ${assets / USD:,.2f} but received {got_usdc / USD:,.2f} USDC + "
                       f"{got_usdt / USD:,.2f} USDT (tx {tx})", actions)
        self.refresh_light()
        self._row("WITHDRAW", assets / USD, "OK", f"{reason}: received {got_usdc / USD:,.2f} USDC + "
                  f"{got_usdt / USD:,.2f} USDT", tx)
        actions.append(f"Savings: withdrew ${assets / USD:,.2f} {reason} ({got_usdc / USD:,.2f} USDC + "
                       f"{got_usdt / USD:,.2f} USDT, tx {tx})")
        return got_usdc + got_usdt

    def withdraw_all(self, actions: list) -> None:
        """Emergency: take everything out (as far as the vault's 24h limits
        allow now; the rest at the next cycles) and convert USDT."""
        st = self.refresh()
        if st.shares == 0:
            self.convert_usdt(actions)
            return
        blocked = self.withdraw_blocked_reason(st)
        if blocked:
            self.alert("withdraw_all", f"emergency withdraw-all is waiting: {blocked}", actions)
            return
        shares = min(st.shares, self._call_words(SAVINGS_VAULT_ADDRESS, SEL_MAX_REDEEM + _addr_word(self.wallet))[0])
        est = self._call_words(SAVINGS_VAULT_ADDRESS, SEL_PREVIEW_REDEEM + _uint_word(shares))[0]
        room = self.withdrawable_raw(st)
        if est > room:
            if room <= 0:
                self.alert("withdraw_all", "emergency withdraw-all is waiting: the vault's 24h withdrawal limits are "
                           "used up; retrying next cycle", actions)
                return
            shares = self._call_words(SAVINGS_VAULT_ADDRESS, SEL_CONVERT_TO_SHARES + _uint_word(room))[0]
            self.alert("withdraw_all_partial", f"emergency withdraw-all: taking ${room / USD:,.2f} now (vault "
                       "24h limits); the rest at the next cycles", actions)
        if shares == 0:
            return
        data = encode_redeem(shares, self.wallet, self.wallet)
        if self.mode == "dry_run":
            self._row("REDEEM_ALL", est / USD, "DRY_RUN", f"would redeem {shares} shares")
            return
        try:
            preview_assets = _words(self.chain.call(SAVINGS_VAULT_ADDRESS, data))[0]
        except SimulationRevertedError as e:
            self.alert("withdraw_all", f"the vault would refuse the emergency withdrawal: {e}", actions)
            return
        if preview_assets == 0:
            self.alert("withdraw_all", "the vault's circuit breaker would hold the emergency withdrawal; not sent",
                       actions)
            return
        r = self._send_and_wait("redeem", SAVINGS_VAULT_ADDRESS, data, preview_assets / USD, actions)
        if r is None:
            return
        tx = r.get("transactionHash", "")
        ev = find_vault_event(r, TOPIC_WITHDRAW, self.wallet) if receipt_succeeded(r) else None
        if ev is None:
            self.alert("withdraw_all", f"the emergency withdrawal paid nothing or reverted (tx {tx})", actions)
            return
        self.state["withdrawn_usd"] += ev[0] / USD
        self.state["shares"] -= ev[1]
        self._row("REDEEM_ALL", ev[0] / USD, "OK", "emergency withdraw-all", tx)
        actions.append(f"Savings: emergency withdrawal of ${ev[0] / USD:,.2f} (tx {tx})")
        self.refresh_light()
        self.convert_usdt(actions)

    # ---- deposit -----------------------------------------------------------
    def round_trip_gas_usd(self, st: Optional[VaultStatus] = None) -> Optional[float]:
        st = st or self.status
        if not st or not st.eth_usd or not st.gas_price_wei:
            return None
        return GAS_ROUND_TRIP * st.gas_price_wei / 1e18 * st.eth_usd

    def plan_deposit(self, wallet_usdc_raw: int, reserved_usd: float) -> tuple[int, str]:
        """(amount to deposit in raw USDC, reason it's 0). Pure on the status."""
        st = self.status
        free_raw = wallet_usdc_raw - int(math.ceil((self.settings.cash_buffer_usd + reserved_usd) * USD))
        if free_raw < self.settings.min_deposit_usd * USD:
            return 0, (f"${max(0, free_raw) / USD:,.2f} idle above the buffer and reserved buys, below the "
                       f"${self.settings.min_deposit_usd:,.2f} minimum")
        blocked = self.deposit_blocked_reason(st)
        if blocked:
            return 0, blocked
        amount = min(free_raw, int(self.settings.max_deposit_usd * USD), st.max_deposit_raw)
        if amount < self.settings.min_deposit_usd * USD:
            return 0, f"the vault accepts only ${st.max_deposit_raw / USD:,.2f} more"
        apy = self.apy()
        gas = self.round_trip_gas_usd(st)
        if apy is None or apy <= 0:
            return 0, "the vault's recent yield isn't positive"
        if gas is None:
            return 0, "couldn't price gas"
        earn = amount / USD * apy * self.settings.expected_idle_days / 365
        if earn < gas:
            return 0, (f"not worth the gas: ${amount / USD:,.2f} at {apy:.2%}/yr earns ${earn:,.3f} in "
                       f"{self.settings.expected_idle_days:.0f} days, a deposit+withdrawal costs about ${gas:,.3f}")
        return amount, ""

    def sweep(self, reserved_usd: float, actions: list) -> None:
        """End of a cycle with no trade left to make: put idle USDC to work.
        `reserved_usd`: buys still waiting for approval (kept in the wallet)."""
        if self.mode == "withdraw_all":
            self.withdraw_all(actions)
            return
        if self.mode not in ("on", "dry_run"):
            return
        if self.mode == "on":
            self.convert_usdt(actions)
            self._clear_allowance(self.network.usdc_address, SAVINGS_VAULT_ADDRESS, actions)
        st = self.refresh()
        amount, why = self.plan_deposit(st.wallet_usdc_raw, reserved_usd)
        if amount == 0:
            actions.append(f"Savings: no deposit ({why})")
            return
        if self.mode == "dry_run":
            self._row("DEPOSIT", amount / USD, "DRY_RUN",
                      f"would deposit ${amount / USD:,.2f} (approve exactly that, then deposit); APY "
                      f"{(self.apy() or 0):.2%}, round-trip gas ~${self.round_trip_gas_usd(st) or 0:,.3f}")
            actions.append(f"Savings [DRY RUN]: would deposit ${amount / USD:,.2f}")
            return
        self.deposit(amount, actions)

    def deposit(self, amount_raw: int, actions: list) -> int:
        """Exact approve, simulate, deposit, verify. Returns shares received (0 on failure)."""
        v = SAVINGS_VAULT_ADDRESS
        usdc = self.network.usdc_address
        if not self._approve_exact(usdc, v, amount_raw, actions):
            self._row("DEPOSIT", amount_raw / USD, "FAILED", "the exact USDC approval didn't go through")
            self.alert("deposit", "a deposit's USDC approval failed; nothing deposited", actions)
            self._clear_allowance(usdc, v, actions)
            return 0
        data = encode_deposit(amount_raw, self.wallet)
        try:
            preview = _words(self.chain.call(v, data))[0]
        except SimulationRevertedError as e:
            self._clear_allowance(usdc, v, actions)
            self.alert("deposit", f"the vault would refuse a ${amount_raw / USD:,.2f} deposit: {e}", actions)
            return 0
        r = self._send_and_wait("deposit", v, data, amount_raw / USD, actions)
        if r is None:
            return 0
        tx = r.get("transactionHash", "")
        if not receipt_succeeded(r):
            self._clear_allowance(usdc, v, actions)
            self._row("DEPOSIT", amount_raw / USD, "FAILED", "reverted", tx)
            self.alert("deposit", f"a ${amount_raw / USD:,.2f} deposit reverted (tx {tx}); the USDC is still in "
                       "the wallet", actions)
            return 0
        ev = find_vault_event(r, TOPIC_DEPOSIT, self.wallet)
        paid = token_transfers(r, usdc, frm=self.wallet, to=v)
        if ev is None or ev[1] == 0:
            self.alert("deposit", f"a deposit went through but no shares were recorded (tx {tx}); check it", actions)
            return 0
        assets, shares = ev
        if paid != amount_raw or shares < preview * 0.995:
            self.alert("deposit_check", f"deposit check: paid {paid / USD:,.6f} USDC for {shares} shares (expected "
                       f"{amount_raw / USD:,.6f} for about {preview}) (tx {tx})", actions)
        self.state["deposited_usd"] += assets / USD
        self.state["shares"] += shares
        if self._allowance(usdc, v) != 0:
            self._clear_allowance(usdc, v, actions)
        self.refresh_light()
        self._row("DEPOSIT", assets / USD, "OK", f"{shares} shares", tx)
        actions.append(f"Savings: deposited ${assets / USD:,.2f} into the Savings Vault (tx {tx})")
        return shares

    # ---- startup ------------------------------------------------------------
    def verify(self) -> list[str]:
        """Startup checks. Raises SavingsError if this isn't the expected
        USDC vault; returns warnings for anything that blocks deposits."""
        asset = "0x" + format(self._call_words(SAVINGS_VAULT_ADDRESS, SEL_ASSET)[0], "040x")
        if asset.lower() != self.network.usdc_address.lower():
            raise SavingsError(f"the vault's asset is {asset}, not USDC; refusing to use it")
        st = self.refresh()
        warnings = []
        if not st.implementation_ok:
            warnings.append(f"the vault's code is {st.implementation}, not the reviewed {EXPECTED_IMPLEMENTATION}: "
                            "deposits are blocked until this is reviewed")
        if not st.member:
            warnings.append("this wallet is not a registered member of the vault: deposits are blocked")
        if st.paused:
            warnings.append("the vault is paused")
        if not st.cb_readable:
            warnings.append("the vault's withdrawal limits couldn't be read: withdrawals are blocked until they can")
        return warnings

    # ---- summary ------------------------------------------------------------
    def summary(self) -> dict:
        st = self.status
        apy = self.apy()
        return {
            "Savings Vault mode": self.mode.upper(),
            "Savings Vault balance ($)": round(self.value_usd, 2),
            "Savings Vault withdrawable now ($)": round(self.withdrawable_raw() / USD, 2) if st else 0,
            "Savings Vault yield earned ($)": round(self.yield_usd(), 4),
            "Savings Vault costs: gas + USDT swaps ($)": round(self.costs_usd(), 4),
            "Savings Vault APY (%)": round(apy * 100, 2) if apy is not None else "n/a",
            "Savings Vault deposited / withdrawn ($)": f"{self.state['deposited_usd']:,.2f} / "
                                                       f"{self.state['withdrawn_usd']:,.2f}",
            "Savings Vault last alert": (f"{datetime.fromtimestamp(self.state['last_alert_ts'], tz=timezone.utc):%Y-%m-%d %H:%M} "
                                         f"{self.state['last_alert']}" if self.state.get("last_alert") else "none"),
        }
