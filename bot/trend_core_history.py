"""
Warm-up history for the Trend Core (strategy v4). Its slowest measure needs
768 finished 4-hour candles (128 days), more than the bot's own saved
history or the CoinGecko bootstrap provide. So on a start where the Trend
Core isn't ready, it is fed public BTC/USD 4-hour candles up to the last
window the bot has processed. Read-only public market data, no key.

Sources, in order:
  1. Binance's public market-data API (data-api.binance.vision), BTC/USDT,
     up to 1,000 candles. This is the data the Trend Core was tested and
     calibrated on.
  2. Kraken's public OHLC API, BTC/USD, its last 720 candles. Not quite 768:
     the Trend Core then becomes ready after 48 more live candles (8 days).
If both fail, the Trend Core keeps the candles it has and becomes ready once
it has seen 768 (it never trades before that).
"""

from __future__ import annotations

import logging

import requests

from .indicators import Candle
from .trend_core import TrendCore, WARMUP

log = logging.getLogger("lifedao-bot.trend_core_history")
H4 = 4 * 3600
HEADERS = {"User-Agent": "lifedao-bot/1.0"}


class HistoryError(RuntimeError):
    pass


def _continuous_until(candles: list[Candle], last_ts: int) -> list[Candle]:
    """The longest run of consecutive 4-hour candles ending exactly at last_ts."""
    candles = sorted((c for c in candles if c.timestamp <= last_ts), key=lambda c: c.timestamp)
    if not candles or candles[-1].timestamp != last_ts:
        raise HistoryError(f"history doesn't reach the last processed window ({last_ts})")
    run = [candles[-1]]
    for c in reversed(candles[:-1]):
        if run[-1].timestamp - c.timestamp != H4:
            break
        run.append(c)
    return list(reversed(run))


def fetch_binance(last_ts: int, limit: int = 1000, get=requests.get) -> list[Candle]:
    r = get("https://data-api.binance.vision/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "4h", "endTime": last_ts * 1000, "limit": limit},
            headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [Candle(timestamp=k[0] // 1000, open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]))
            for k in r.json()]


def fetch_kraken(last_ts: int, get=requests.get) -> list[Candle]:
    r = get("https://api.kraken.com/0/public/OHLC", params={"pair": "XBTUSD", "interval": 240},
            headers=HEADERS, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise HistoryError(f"Kraken: {body['error']}")
    rows = next(v for k, v in body["result"].items() if k != "last")
    return [Candle(timestamp=int(k[0]), open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]))
            for k in rows]


def warm_trend_core(last_ts: int, sources=None) -> tuple[TrendCore, str]:
    """A Trend Core fed with public history up to and including the window
    starting at last_ts. Returns it and the source used. Raises HistoryError
    if no source works."""
    sources = sources or [("Binance", fetch_binance), ("Kraken", fetch_kraken)]
    problems = []
    for name, fetch in sources:
        try:
            candles = _continuous_until(fetch(last_ts), last_ts)
        except Exception as e:  # noqa: BLE001 -- try the next source
            problems.append(f"{name}: {e}")
            continue
        tc = TrendCore()
        for c in candles:
            tc.update(c.high, c.low, c.close, c.timestamp)
        log.info("Trend Core warmed up on %d %s candles (ready: %s, needs %d)", len(candles), name, tc.ready, WARMUP)
        return tc, name
    raise HistoryError("; ".join(problems))
