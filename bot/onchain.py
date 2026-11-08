"""
Live on-chain price reader for the WBTC/USDC pool.

Deliberately dependency-free beyond `requests`: no web3.py. The only calls
this bot ever needs are four read-only view functions on one known
contract (slot0, token0, token1, fee), all with fixed-width, non-dynamic
return types -- easy and safe to hand-decode, and it avoids pulling in a
much heavier dependency (web3.py drags in eth-abi, eth-utils, and friends)
for four function selectors.

Endpoint choice: arb1.arbitrum.io/rpc is Arbitrum Foundation's own public
RPC for Arbitrum One -- free, no API key, no signup. Our call volume here
is trivial (a handful of calls every 4 hours), well within what any public
endpoint tolerates, so there's no need for a paid RPC provider. A couple of
other well-known public endpoints are listed as fallbacks in case the
primary is ever down.

This module is unavoidably untestable end-to-end in the environment this
bot was built in (that sandbox's network egress is restricted to package
registries, not RPC endpoints) -- but the decoding logic itself is
verified in tests/test_onchain.py against the real slot0 tuple this pool
returned when checked via Blockscout during development, so the ABI
decoding is proven correct independent of network access. Once deployed on
Railway (which has normal outbound internet access), the actual HTTP calls
will simply work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

from . import config

RPC_ENDPOINTS = [
    "https://arb1.arbitrum.io/rpc",              # Arbitrum Foundation's own public RPC -- primary
    "https://arbitrum-one.publicnode.com",        # fallback
    "https://rpc.ankr.com/arbitrum",              # fallback
]

# Function selectors = first 4 bytes of keccak256("<signature>"), computed
# once during development (see build notes) and stable forever since they
# depend only on the function signature, not the deployed bytecode.
SELECTOR_SLOT0 = "0x3850c7bd"     # slot0()
SELECTOR_TOKEN0 = "0x0dfe1681"    # token0()
SELECTOR_TOKEN1 = "0xd21220a7"    # token1()
SELECTOR_FEE = "0xddca3f43"       # fee()
SELECTOR_OBSERVE = "0x883bdbfd"   # observe(uint32[])

WORD = 32   # bytes per ABI-encoded word


class OnchainError(RuntimeError):
    pass


def _decode_uint(chunk: bytes) -> int:
    return int.from_bytes(chunk, byteorder="big", signed=False)


def _decode_int(chunk: bytes) -> int:
    # Solidity sign-extends signed return values across the full 32-byte
    # word, so reading the whole word as signed recovers the correct value
    # directly -- no separate 24-bit masking needed for int24 (tick).
    return int.from_bytes(chunk, byteorder="big", signed=True)


def _decode_address(chunk: bytes) -> str:
    return "0x" + chunk[-20:].hex()


def _rpc_call(method: str, params: list, timeout: float = 10.0) -> str:
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    last_err: Optional[Exception] = None
    for url in RPC_ENDPOINTS:
        try:
            resp = requests.post(url, json=payload, timeout=timeout,
                                  headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise OnchainError(f"RPC error from {url}: {body['error']}")
            return body["result"]
        except Exception as e:   # noqa: BLE001 -- deliberately broad, we fall through to the next endpoint
            last_err = e
            continue
    raise OnchainError(f"All RPC endpoints failed for {method}. Last error: {last_err}")


def _eth_call(to_address: str, selector: str) -> bytes:
    result_hex = _rpc_call("eth_call", [{"to": to_address, "data": selector}, "latest"])
    hex_str = result_hex[2:] if result_hex.startswith("0x") else result_hex
    return bytes.fromhex(hex_str)


@dataclass
class Slot0:
    sqrt_price_x96: int
    tick: int
    observation_index: int
    observation_cardinality: int
    observation_cardinality_next: int
    fee_protocol: int
    unlocked: bool


def decode_slot0(data: bytes) -> Slot0:
    if len(data) != 7 * WORD:
        raise OnchainError(f"expected {7 * WORD} bytes from slot0(), got {len(data)}")
    words = [data[i * WORD:(i + 1) * WORD] for i in range(7)]
    return Slot0(
        sqrt_price_x96=_decode_uint(words[0]),
        tick=_decode_int(words[1]),
        observation_index=_decode_uint(words[2]),
        observation_cardinality=_decode_uint(words[3]),
        observation_cardinality_next=_decode_uint(words[4]),
        fee_protocol=_decode_uint(words[5]),
        unlocked=_decode_uint(words[6]) != 0,
    )


def price_from_sqrt_price_x96(sqrt_price_x96: int,
                               decimals0: int = config.WBTC_DECIMALS,
                               decimals1: int = config.USDC_DECIMALS) -> float:
    """USDC per WBTC (i.e. the BTC/USD price), from the raw Q64.96 value."""
    q96 = 2 ** 96
    ratio = (sqrt_price_x96 / q96) ** 2
    return ratio * (10 ** (decimals0 - decimals1))


def get_live_price(pool_address: str = config.POOL_ADDRESS) -> dict:
    """The one call the live cycle actually needs: current price + raw slot0
    fields, for logging."""
    raw = _eth_call(pool_address, SELECTOR_SLOT0)
    slot0 = decode_slot0(raw)
    price = price_from_sqrt_price_x96(slot0.sqrt_price_x96)
    return {
        "price_usdc_per_wbtc": price,
        "tick": slot0.tick,
        "sqrt_price_x96": slot0.sqrt_price_x96,
        "fetched_at": int(time.time()),
    }


def verify_pool(pool_address: str = config.POOL_ADDRESS) -> None:
    """Startup self-check, same philosophy as the tech doc's Section 2.1
    checksum verification: confirm the pool we're about to poll every 4
    hours actually is the WBTC/USDC 0.05% pool the spec describes, and
    raise loudly rather than silently trading against the wrong pool."""
    token0 = _decode_address(_eth_call(pool_address, SELECTOR_TOKEN0))
    token1 = _decode_address(_eth_call(pool_address, SELECTOR_TOKEN1))
    fee_raw = _decode_uint(_eth_call(pool_address, SELECTOR_FEE))

    problems = []
    if token0.lower() != config.TOKEN0_WBTC.lower():
        problems.append(f"token0 mismatch: expected {config.TOKEN0_WBTC}, got {token0}")
    if token1.lower() != config.TOKEN1_USDC.lower():
        problems.append(f"token1 mismatch: expected {config.TOKEN1_USDC}, got {token1}")
    if fee_raw != config.POOL_FEE_UNITS:
        problems.append(f"fee mismatch: expected {config.POOL_FEE_UNITS}, got {fee_raw}")

    if problems:
        raise OnchainError(
            "Pool verification FAILED -- refusing to start. "
            f"Pool address {pool_address} does not match the expected WBTC/USDC pool: "
            + "; ".join(problems)
        )


def decode_observe_twap_tick(data: bytes, seconds_ago: int) -> float:
    """Average tick over the last `seconds_ago` seconds from observe([s, 0]).
    Return layout: two dynamic arrays (int56[] tickCumulatives, uint160[]
    secondsPerLiquidityCumulativeX128s): [offset_a, offset_b, len_a, a0, a1,
    len_b, b0, b1]."""
    if len(data) < 8 * WORD:
        raise OnchainError(f"expected at least {8 * WORD} bytes from observe(), got {len(data)}")
    words = [data[i * WORD:(i + 1) * WORD] for i in range(len(data) // WORD)]
    off_a = _decode_uint(words[0]) // WORD
    if _decode_uint(words[off_a]) != 2:
        raise OnchainError("observe() returned an unexpected number of observations")
    cum_then, cum_now = _decode_int(words[off_a + 1]), _decode_int(words[off_a + 2])
    return (cum_now - cum_then) / seconds_ago


def tick_to_price(tick: float, decimals0: int = config.WBTC_DECIMALS, decimals1: int = config.USDC_DECIMALS) -> float:
    """USDC per WBTC at a (possibly fractional, averaged) tick."""
    return 1.0001 ** tick * 10 ** (decimals0 - decimals1)


def get_twap_price(pool_address: str = config.POOL_ADDRESS, seconds_ago: int = 1800) -> float:
    """The pool's own time-weighted average price over the last `seconds_ago`
    seconds (Uniswap V3's built-in price record). Read-only. A single
    transaction can push the spot price anywhere for a moment; it barely
    moves this average."""
    data = "0x" + SELECTOR_OBSERVE[2:] + f"{32:064x}" + f"{2:064x}" + f"{seconds_ago:064x}" + f"{0:064x}"
    result_hex = _rpc_call("eth_call", [{"to": pool_address, "data": data}, "latest"])
    raw = bytes.fromhex(result_hex[2:] if result_hex.startswith("0x") else result_hex)
    return tick_to_price(decode_observe_twap_tick(raw, seconds_ago))
