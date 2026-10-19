"""
Live wallet: builds, signs, broadcasts, and simulates the real on-chain
transactions the live executor needs (ERC20 approve, Uniswap V3 swap).

This is the only module in the whole project that ever handles a private
key, and the rule around that is absolute: the key is read from an
environment variable at the moment `Account.from_key()` is called, used
only to produce a signature, and never logged, printed, returned, or
written anywhere. Every function below takes the key as an explicit
parameter rather than reading the environment itself, so it's obvious at
every call site exactly when the key is touched.

Two library choices worth noting:
  - `eth_account` for signing -- the standard, minimal library for this
    exact job (part of the web3.py ecosystem, usable completely standalone
    without pulling in the rest of web3.py).
  - `eth_abi` for encoding the swap's struct parameter -- unlike the simple
    fixed-selector reads in onchain.py (which only ever needed a bare 4-byte
    selector, no arguments), encoding a 7-field tuple correctly by hand is
    real surface area for a mistake. Given real funds are on the line here,
    reaching for a battle-tested encoding library was the right call over
    hand-rolling it, even though it's an extra dependency.

The exactInputSingle selector/struct layout below was confirmed against
Uniswap's actual deployed SwapRouter02 on Arbitrum One via
Blockscout:get_contract_abi during development -- notably, this router's
version of the struct has NO `deadline` field (older Uniswap router
versions did), which would have been an easy, costly mistake to make from
memory alone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests
from eth_account import Account
from eth_abi import encode as abi_encode
from Crypto.Hash import keccak

from . import config


WORD = 32


def _decode_uint(chunk: bytes) -> int:
    return int.from_bytes(chunk, byteorder="big", signed=False)


def _decode_int(chunk: bytes) -> int:
    return int.from_bytes(chunk, byteorder="big", signed=True)


class LiveWalletError(RuntimeError):
    pass


class SimulationRevertedError(LiveWalletError):
    """Raised by simulate_call when the chain reports the call WOULD
    revert -- exactly the signal the pre-flight check exists to catch,
    before any real transaction is ever sent."""
    pass


def _selector(signature: str) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(signature.encode())
    return k.digest()[:4]


SELECTOR_APPROVE = _selector("approve(address,uint256)")
SELECTOR_ALLOWANCE = _selector("allowance(address,address)")
SELECTOR_BALANCE_OF = _selector("balanceOf(address)")
SELECTOR_EXACT_INPUT_SINGLE = _selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")


# ---------------------------------------------------------------------------
# Call-data encoding (pure functions, no network -- see tests/test_live_wallet.py)
# ---------------------------------------------------------------------------

def encode_erc20_approve(spender: str, amount: int) -> bytes:
    return SELECTOR_APPROVE + abi_encode(["address", "uint256"], [spender, amount])


def encode_erc20_allowance_call(owner: str, spender: str) -> bytes:
    return SELECTOR_ALLOWANCE + abi_encode(["address", "address"], [owner, spender])


def encode_erc20_balance_of_call(owner: str) -> bytes:
    return SELECTOR_BALANCE_OF + abi_encode(["address"], [owner])


def encode_exact_input_single(token_in: str, token_out: str, fee: int, recipient: str,
                               amount_in: int, amount_out_minimum: int,
                               sqrt_price_limit_x96: int = 0) -> bytes:
    args = abi_encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(token_in, token_out, fee, recipient, amount_in, amount_out_minimum, sqrt_price_limit_x96)],
    )
    return SELECTOR_EXACT_INPUT_SINGLE + args


def compute_min_amount_out(expected_amount_out: int, slippage_tolerance_pct: float) -> int:
    """Floor on what we'll accept back from a swap. Defaults are set by the
    caller (see live_config.py) -- this is deliberately a plain function so
    the math is unit-testable on its own."""
    return int(expected_amount_out * (1 - slippage_tolerance_pct / 100))


# ---------------------------------------------------------------------------
# RPC plumbing
# ---------------------------------------------------------------------------

def _rpc_call(rpc_urls: list[str], method: str, params: list, timeout: float = 15.0):
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    last_err = None
    for url in rpc_urls:
        try:
            resp = requests.post(url, json=payload, timeout=timeout, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise LiveWalletError(f"RPC error from {url} calling {method}: {body['error']}")
            return body["result"]
        except LiveWalletError:
            raise   # a real RPC-level error (e.g. revert) should surface, not be swallowed by fallback
        except Exception as e:
            last_err = e
            continue
    raise LiveWalletError(f"All RPC endpoints failed for {method}. Last error: {last_err}")


def get_eth_balance(rpc_urls: list[str], address: str) -> int:
    result = _rpc_call(rpc_urls, "eth_getBalance", [address, "latest"])
    return int(result, 16)


def check_token_allowance(rpc_urls: list[str], token_address: str, owner: str, spender: str) -> int:
    """Read how much `spender` (the router) is currently approved to spend of
    `owner`'s `token_address`. Pure read (eth_call), no signing, no gas."""
    raw = simulate_call(rpc_urls, owner, token_address, encode_erc20_allowance_call(owner, spender))
    return int.from_bytes(raw, "big")


def check_router_approvals(rpc_urls: list[str], router: str, owner: str,
                            usdc_address: str, wbtc_address: str,
                            min_allowance: int = 1) -> dict:
    """Startup pre-flight: report whether the router is approved to spend BOTH
    tokens the bot trades. The bot needs USDC approval to BUY and WBTC
    approval to SELL. Historically only USDC was approved, so the very first
    sell reverted with Uniswap's 'STF' (SafeTransferFailed) error -- this
    check surfaces a missing approval at startup, before a trade fails,
    rather than discovering it the hard way mid-cycle.

    Also reports whether each allowance covers the wallet's current balance
    of that token. A finite allowance shrinks with every swap, so one that
    is non-zero but below the balance means trades will start reverting
    with 'STF' once it runs out (P3: the original USDC approval was 201).

    Returns a dict with each token's allowance and balance (raw units), and
    'ok' / '*_covers_balance' flags; it does NOT raise (a missing approval
    shouldn't crash the bot -- the bot can still run and BUY; it just can't
    SELL until WBTC is approved, which is exactly the situation to warn
    about, not to halt on)."""
    usdc_allowance = check_token_allowance(rpc_urls, usdc_address, owner, router)
    wbtc_allowance = check_token_allowance(rpc_urls, wbtc_address, owner, router)
    usdc_balance = check_token_balance(rpc_urls, usdc_address, owner)
    wbtc_balance = check_token_balance(rpc_urls, wbtc_address, owner)
    return {
        "usdc_allowance": usdc_allowance,
        "wbtc_allowance": wbtc_allowance,
        "usdc_balance": usdc_balance,
        "wbtc_balance": wbtc_balance,
        "usdc_ok": usdc_allowance >= min_allowance,
        "wbtc_ok": wbtc_allowance >= min_allowance,
        "ok": usdc_allowance >= min_allowance and wbtc_allowance >= min_allowance,
        "usdc_covers_balance": usdc_allowance >= usdc_balance,
        "wbtc_covers_balance": wbtc_allowance >= wbtc_balance,
    }


def check_token_balance(rpc_urls: list[str], token_address: str, owner: str) -> int:
    """`owner`'s balance of `token_address` in raw units. Pure read (eth_call)."""
    raw = simulate_call(rpc_urls, owner, token_address, encode_erc20_balance_of_call(owner))
    return int.from_bytes(raw, "big")


def get_nonce(rpc_urls: list[str], address: str) -> int:
    result = _rpc_call(rpc_urls, "eth_getTransactionCount", [address, "pending"])
    return int(result, 16)


def get_gas_price(rpc_urls: list[str]) -> int:
    result = _rpc_call(rpc_urls, "eth_gasPrice", [])
    return int(result, 16)


def estimate_gas(rpc_urls: list[str], from_address: str, to_address: str, data: bytes, value: int = 0) -> int:
    result = _rpc_call(rpc_urls, "eth_estimateGas", [{
        "from": from_address, "to": to_address, "value": hex(value), "data": "0x" + data.hex(),
    }])
    return int(result, 16)


def simulate_call(rpc_urls: list[str], from_address: str, to_address: str, data: bytes, value: int = 0) -> bytes:
    """A pure eth_call -- asks the chain 'would this succeed?' without
    executing anything for real. No gas spent, no state changed, no
    signature needed. If the call would revert, the RPC error surfaces as
    a SimulationRevertedError with whatever reason the contract gave."""
    try:
        result = _rpc_call(rpc_urls, "eth_call", [{
            "from": from_address, "to": to_address, "value": hex(value), "data": "0x" + data.hex(),
        }, "latest"])
    except LiveWalletError as e:
        raise SimulationRevertedError(f"Simulation indicates this call would revert: {e}") from e
    hex_str = result[2:] if result.startswith("0x") else result
    return bytes.fromhex(hex_str)


# ---------------------------------------------------------------------------
# Signing and sending (the only functions that ever see a private key)
# ---------------------------------------------------------------------------

@dataclass
class SignedTx:
    raw_hex: str
    tx_hash: str
    nonce: int
    gas_price: int
    gas_limit: int


def _ensure_0x(hex_str: str) -> str:
    return hex_str if hex_str.startswith("0x") else "0x" + hex_str


def sign_transaction_dict(private_key_hex: str, tx: dict) -> SignedTx:
    """The actual signing step, isolated from network calls -- takes a
    fully-formed transaction dict (nonce, gasPrice, gas, to, value, data,
    chainId already decided) and returns the signed, ready-to-broadcast
    result. Kept separate from build_and_sign_transaction so the signing
    logic itself -- the part that must be cryptographically correct -- can
    be tested with fixed inputs, independent of any RPC call.
    """
    account = Account.from_key(private_key_hex)
    signed = account.sign_transaction(tx)
    # eth_account renamed these attributes between versions: newer releases use
    # `raw_transaction` / `hash`, while 0.11.x uses `rawTransaction` / `hash`.
    # Support both so this works regardless of which version is installed
    # (the local machine had 0.11.3; Railway may have a newer one). getattr
    # with a fallback avoids an AttributeError crash at the signing step.
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = getattr(signed, "rawTransaction")
    tx_hash_attr = getattr(signed, "hash", None)
    # `.hex()` may or may not include the "0x" prefix depending on version --
    # _ensure_0x normalizes it so every downstream consumer is consistent.
    return SignedTx(raw_hex=_ensure_0x(raw.hex()), tx_hash=_ensure_0x(tx_hash_attr.hex()),
                     nonce=tx["nonce"], gas_price=tx["gasPrice"], gas_limit=tx["gas"])
    # `account` and `private_key_hex` fall out of scope here; nothing about
    # the key is retained beyond this function call.


def build_and_sign_transaction(rpc_urls: list[str], chain_id: int, private_key_hex: str,
                                to_address: str, data: bytes, value: int = 0,
                                gas_limit_override: Optional[int] = None,
                                gas_price_multiplier: float = 1.15) -> SignedTx:
    """Builds a legacy (type-0) transaction -- simpler and just as
    universally supported on Arbitrum as EIP-1559, and one less set of
    fee-market fields to get wrong for something this consequential."""
    from_address = Account.from_key(private_key_hex).address

    nonce = get_nonce(rpc_urls, from_address)
    gas_price = int(get_gas_price(rpc_urls) * gas_price_multiplier)
    gas_limit = gas_limit_override or int(estimate_gas(rpc_urls, from_address, to_address, data, value) * 1.3)

    tx = {
        "nonce": nonce, "gasPrice": gas_price, "gas": gas_limit,
        "to": to_address, "value": value, "data": "0x" + data.hex(), "chainId": chain_id,
    }
    return sign_transaction_dict(private_key_hex, tx)


def broadcast_transaction(rpc_urls: list[str], raw_hex: str) -> str:
    raw = raw_hex if raw_hex.startswith("0x") else "0x" + raw_hex
    return _rpc_call(rpc_urls, "eth_sendRawTransaction", [raw])


def wait_for_receipt(rpc_urls: list[str], tx_hash: str, timeout_seconds: float = 180.0,
                      poll_interval: float = 5.0) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        receipt = _rpc_call(rpc_urls, "eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            return receipt
        time.sleep(poll_interval)
    raise LiveWalletError(f"Timed out after {timeout_seconds}s waiting for receipt of {tx_hash}")


def receipt_succeeded(receipt: dict) -> bool:
    return int(receipt["status"], 16) == 1


# ---------------------------------------------------------------------------
# Swap event decoding -- the ground truth for what a swap actually did
# ---------------------------------------------------------------------------

# keccak256("Swap(address,address,int256,int256,uint160,uint128,int24)"),
# confirmed against the real pool's ABI via Blockscout:get_contract_abi.
# Event topics use the full 32-byte hash (unlike function selectors, which
# are truncated to 4 bytes).
def _event_topic0(signature: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(signature.encode())
    return "0x" + k.hexdigest()


SWAP_EVENT_TOPIC0 = _event_topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")


@dataclass
class SwapEvent:
    sender: str
    recipient: str
    amount0: int   # signed; negative = pool paid this token out
    amount1: int   # signed; negative = pool paid this token out
    sqrt_price_x96: int
    liquidity: int
    tick: int


def _decode_address_topic(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:]


def decode_swap_event(receipt: dict, pool_address: str) -> SwapEvent:
    """Finds and decodes this specific pool's Swap event within a receipt's
    logs. Raises if it's missing -- a swap transaction that succeeded but
    produced no Swap event from our pool would mean something is badly
    wrong and should never be silently treated as "no fill happened."""
    pool_lower = pool_address.lower()
    for log in receipt.get("logs", []):
        if log["address"].lower() != pool_lower:
            continue
        topics = log["topics"]
        if not topics or topics[0].lower() != SWAP_EVENT_TOPIC0.lower():
            continue

        sender = _decode_address_topic(topics[1])
        recipient = _decode_address_topic(topics[2])

        data_hex = log["data"][2:] if log["data"].startswith("0x") else log["data"]
        data = bytes.fromhex(data_hex)
        if len(data) != 5 * WORD:
            raise LiveWalletError(f"Unexpected Swap event data length: {len(data)}")
        words = [data[i * WORD:(i + 1) * WORD] for i in range(5)]
        return SwapEvent(
            sender=sender, recipient=recipient,
            amount0=_decode_int(words[0]), amount1=_decode_int(words[1]),
            sqrt_price_x96=_decode_uint(words[2]),
            liquidity=_decode_uint(words[3]),
            tick=_decode_int(words[4]),
        )
    raise LiveWalletError(f"No Swap event found from pool {pool_address} in this receipt's logs")
