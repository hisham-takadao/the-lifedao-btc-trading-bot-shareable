"""
One-time historical warm-up bootstrap. Tech doc Section 2.4 needs 300
candles (50 days) of history before the first trade is even eligible; this
fetches enough history to satisfy that plus a convergence buffer (see
config.BOOTSTRAP_TOTAL_CANDLES), the *one time* the bot has no history of
its own yet.

Why this doesn't just read the chain: reconstructing 70+ days of true 4h
OHLC directly from this pool's on-chain Swap events would mean pulling and
aggregating an enormous number of events -- Blockscout's own operating
guidance is explicit that on-chain data isn't meant to serve as a
historical price series. So this uses CoinGecko's free Demo API instead,
for this one bootstrap step only. Every live cycle after this reads the
real pool directly (onchain.py) -- this module is never called again once
the bot has built up its own history.

Why resampled hourly data rather than the native OHLC endpoint: CoinGecko's
`/coins/{id}/ohlc` only returns native 4-hour candles for a 3-30 day
lookback; beyond 30 days it switches to 4-day candles, too coarse for our
300-candle/50-day minimum. `/coins/{id}/market_chart` gives hourly price
points out to 90 days on the free plan, so this resamples those into
synthetic 4h OHLC (open=first, close=last, high=max, low=min within each
4h window). The loss versus true on-chain OHLC is some intra-candle wick
precision -- acceptable since this only seeds indicator history and is
never used for a live trading decision.
"""

from __future__ import annotations

from typing import Optional

import requests

from . import config
from .indicators import Candle

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
COIN_ID = "bitcoin"   # CoinGecko's own id for BTC; used only as a proxy for
                       # WBTC/USDC during bootstrap, never for live decisions


class BootstrapError(RuntimeError):
    pass


def fetch_hourly_prices_usd(days: int, api_key: Optional[str], timeout: float = 20.0) -> list[tuple[int, float]]:
    """Returns [(timestamp_seconds, price_usd), ...] sorted ascending."""
    params = {"vs_currency": "usd", "days": str(days)}
    if api_key:
        params["x_cg_demo_api_key"] = api_key
    url = f"{COINGECKO_BASE_URL}/coins/{COIN_ID}/market_chart"

    resp = requests.get(url, params=params, timeout=timeout)
    if resp.status_code == 429:
        raise BootstrapError("CoinGecko rate limit hit (429). Wait a minute and restart the bot.")
    resp.raise_for_status()
    body = resp.json()
    prices = body.get("prices")
    if not prices:
        raise BootstrapError(f"CoinGecko response had no 'prices' field: {body}")

    return sorted((int(ts_ms / 1000), float(price)) for ts_ms, price in prices)


def resample_to_4h_candles(hourly_prices: list[tuple[int, float]]) -> list[Candle]:
    """Groups (timestamp, price) points into 4-hour buckets aligned to UTC
    boundaries (00/04/08/12/16/20:00) and folds each bucket into one OHLC
    candle. Drops the final bucket if it isn't finished yet (its end
    boundary is still in the future), since a partial candle would corrupt
    every indicator that depends on candle completeness.
    """
    if not hourly_prices:
        return []

    buckets: dict[int, list[float]] = {}
    for ts, price in hourly_prices:
        bucket_start = ts - (ts % config.CANDLE_INTERVAL_SECONDS)
        buckets.setdefault(bucket_start, []).append(price)

    now = hourly_prices[-1][0]
    candles = []
    for bucket_start in sorted(buckets.keys()):
        bucket_end = bucket_start + config.CANDLE_INTERVAL_SECONDS
        if bucket_end > now + config.CANDLE_INTERVAL_SECONDS // 4:
            continue   # still-forming candle, not enough of the window has elapsed
        prices_in_bucket = buckets[bucket_start]
        candles.append(Candle(
            timestamp=bucket_start,
            open=prices_in_bucket[0],
            high=max(prices_in_bucket),
            low=min(prices_in_bucket),
            close=prices_in_bucket[-1],
        ))
    return candles


def bootstrap_history(api_key: Optional[str], days: int = 90) -> list[Candle]:
    """The one function main.py calls, exactly once, on a completely fresh
    deployment with no saved state yet."""
    hourly = fetch_hourly_prices_usd(days=days, api_key=api_key)
    candles = resample_to_4h_candles(hourly)
    if len(candles) < config.WARMUP_CANDLES:
        raise BootstrapError(
            f"Only got {len(candles)} candles from bootstrap, need at least "
            f"{config.WARMUP_CANDLES} for the spec's warm-up period. "
            "CoinGecko's free tier may have changed its history window -- "
            "check https://docs.coingecko.com/reference/coins-id-market-chart"
        )
    # Keep the most recent BOOTSTRAP_TOTAL_CANDLES; more history than that
    # doesn't hurt, but there's no need to feed the whole 90-day window
    # through if it's larger than the intended buffer.
    return candles[-config.BOOTSTRAP_TOTAL_CANDLES:]
