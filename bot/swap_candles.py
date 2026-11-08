"""
4-hour candles built from every swap in the pool (the default since
October 2026).

The 20-minute samples only see 12 prices per window: highs and lows between
samples are missed, the close is the price at hh:40:05 (20 minutes before
the window ends), and a missed or shifted poll changes the candle. That
sampling noise feeds ADX, which carries it for weeks (on 2026-10-15 the old
drifting polls left the live ADX at 21.7 instead of 28.2).

Here a finished window's candle is read from the pool's own Swap events,
so it is a fixed fact of the chain, whatever the poll timing or restarts:
  open  = the price after the last swap at or before the window start
  high  = the highest of the open and every transaction's final price in the window
  low   = the lowest of the open and every transaction's final price in the window
  close = the price after the last swap before the window ends
(exactly the original project's backtest "true candles"). Prices are
the pool's post-swap sqrtPriceX96, converted as everywhere else in the bot.

One price per transaction (2026-11-08): a transaction counts only with the
pool price after its last swap; prices between swaps of the same
transaction are ignored, because nobody else can trade at them. On
2025-10-20 single transactions (e.g. 0xef6923493d...) pushed the pool from
about $110,800 to $110 and back inside themselves; counting those prices
gave candles with a $110 low and inflated ATR for about 14 candles. Open
and close are unaffected (the last swap of a block is always the last swap
of its transaction).

Read-only: eth_getLogs and block headers only.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from . import config
from .indicators import Candle
from .onchain import RPC_ENDPOINTS, OnchainError, price_from_sqrt_price_x96

log = logging.getLogger("lifedao-bot.swap_candles")

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
WINDOW = config.CANDLE_INTERVAL_SECONDS
OPEN_LOOKBACK_SECONDS = 2 * 3600          # where to look for the last swap before the window
MAX_LOOKBACK_SECONDS = 24 * 3600
MAX_LOG_SPAN_BLOCKS = 100_000             # the official endpoint's eth_getLogs limit


def _rpc(method: str, params: list, timeout: float = 30.0):
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
        except Exception as e:   # noqa: BLE001 -- try the next endpoint
            last_err = e
    raise OnchainError(f"All RPC endpoints failed for {method}. Last error: {last_err}")


def _block_ts(block: int, cache: dict) -> int:
    if block not in cache:
        cache[block] = int(_rpc("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)
    return cache[block]


def first_block_at_or_after(ts: int, latest: int, cache: dict, lo_hint: Optional[int] = None) -> int:
    """Smallest block with timestamp >= ts (block timestamps never decrease).
    Interpolation search (Arbitrum's block times are regular), then bisection
    once the range is small, so it needs a handful of header reads."""
    hi = latest
    if _block_ts(hi, cache) < ts:
        raise OnchainError(f"no block at or after {ts} yet")
    lo = lo_hint if lo_hint is not None else max(0, latest - 4 * (_block_ts(latest, cache) - ts) - 20_000)
    step = max(1, latest - lo)
    while lo > 0 and _block_ts(lo, cache) >= ts:          # make sure lo is before ts
        lo = max(0, lo - step)
        step *= 2
    if _block_ts(lo, cache) >= ts:
        return lo
    # invariant: ts(lo) < ts <= ts(hi)
    while hi - lo > 64:
        t_lo, t_hi = _block_ts(lo, cache), _block_ts(hi, cache)
        guess = lo + int((ts - t_lo) / max(1, t_hi - t_lo) * (hi - lo))
        guess = min(hi - 1, max(lo + 1, guess))
        # probe a little either side of the guess to shrink both ends
        for g in (guess - 32, guess + 32):
            g = min(hi - 1, max(lo + 1, g))
            if _block_ts(g, cache) >= ts:
                hi = min(hi, g)
            else:
                lo = max(lo, g)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _block_ts(mid, cache) >= ts:
            hi = mid
        else:
            lo = mid
    return hi


def get_swap_logs(pool: str, from_block: int, to_block: int) -> list[dict]:
    """The pool's Swap events in [from_block, to_block], splitting ranges the
    endpoint refuses (over 100,000 blocks, too many results, or too slow)."""
    if from_block > to_block:
        return []
    if to_block - from_block + 1 > MAX_LOG_SPAN_BLOCKS:
        mid = from_block + MAX_LOG_SPAN_BLOCKS - 1
        return get_swap_logs(pool, from_block, mid) + get_swap_logs(pool, mid + 1, to_block)
    try:
        return _rpc("eth_getLogs", [{"address": pool, "fromBlock": hex(from_block),
                                     "toBlock": hex(to_block), "topics": [SWAP_TOPIC]}], timeout=60.0)
    except OnchainError:
        if to_block - from_block < 10:
            raise
        mid = (from_block + to_block) // 2
        return get_swap_logs(pool, from_block, mid) + get_swap_logs(pool, mid + 1, to_block)


def _sqrt_price(lg: dict) -> int:
    return int(lg["data"][2 + 64 * 2: 2 + 64 * 3], 16)


def last_swap_per_transaction(swaps: list[tuple]) -> list[tuple]:
    """Swaps given as (block, log_index, sqrtPriceX96, tx_hash): only the
    last swap (highest log index) of each transaction, in chain order."""
    last: dict = {}
    for s in sorted(swaps, key=lambda s: (s[0], s[1])):
        last[(s[0], s[3].lower())] = s
    return sorted(last.values(), key=lambda s: (s[0], s[1]))


def build_candle(window_ts: int, before: list[tuple], inside: list[tuple]) -> Candle:
    """Pure: the candle from swaps given as (block, log_index, sqrtPriceX96,
    tx_hash). `before`: swaps at or before the window start (only the last
    one is used; tx_hash optional there); `inside`: swaps after the window
    start and before its end, of which each transaction contributes only its
    last swap's price."""
    if not before:
        raise OnchainError(f"no swap found before window {window_ts}")
    order = lambda s: (s[0], s[1])
    open_ = price_from_sqrt_price_x96(max(before, key=order)[2])
    prices = [price_from_sqrt_price_x96(s[2]) for s in last_swap_per_transaction(inside)]
    allp = [open_] + prices
    return Candle(timestamp=window_ts, open=open_, high=max(allp), low=min(allp),
                  close=prices[-1] if prices else open_)


def candle_for_window(window_ts: int, pool: str = config.POOL_ADDRESS) -> tuple[Candle, int]:
    """The finished window's candle from the chain, and its number of swaps."""
    if window_ts % WINDOW:
        raise ValueError("window_ts must be a 4-hour UTC boundary")
    cache: dict = {}
    latest = int(_rpc("eth_blockNumber", []), 16)
    b_end = first_block_at_or_after(window_ts + WINDOW, latest, cache)     # blocks < this: ts < window end
    b_inside_start = first_block_at_or_after(window_ts, b_end, cache)
    b_open_end = first_block_at_or_after(window_ts + 1, b_end, cache, lo_hint=max(0, b_inside_start - 1))
    lookback = OPEN_LOOKBACK_SECONDS
    while True:
        b_lo = first_block_at_or_after(window_ts - lookback, b_inside_start, cache)
        logs = [lg for lg in get_swap_logs(pool, b_lo, b_end - 1) if lg["address"].lower() == pool.lower()]
        swaps = [(int(lg["blockNumber"], 16), int(lg["logIndex"], 16), _sqrt_price(lg), lg["transactionHash"])
                 for lg in logs]
        before = [s for s in swaps if s[0] < b_open_end]
        if before or lookback >= MAX_LOOKBACK_SECONDS:
            break
        lookback *= 4
    # A swap in the window-start second itself (ts == window_ts) counts as "at
    # or before the start" for the open and as inside the window for the
    # high/low, exactly as the backtest builds it.
    inside = [s for s in swaps if b_inside_start <= s[0] < b_end]
    return build_candle(window_ts, before, inside), len(inside)


def candle_source_with_retries(window_ts: int, attempts: int = 3, pause: float = 10.0) -> Candle:
    """What the scheduler calls when a window finishes. Retries a failed
    chain read a few times; the caller falls back to the sampled candle if
    every attempt fails."""
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            candle, n = candle_for_window(window_ts)
            log.info("Candle from %d swaps: open %.2f high %.2f low %.2f close %.2f",
                     n, candle.open, candle.high, candle.low, candle.close)
            return candle
        except Exception as e:   # noqa: BLE001
            last = e
            log.warning("Building the candle from swaps failed (attempt %d/%d): %s", i + 1, attempts, e)
            if i + 1 < attempts:
                time.sleep(pause * (i + 1))
    raise OnchainError(f"could not build the candle from swaps: {last}")
