"""
Start the lifedao BTC trading bot.

    python run_bot.py

It trades WBTC/USDC on Uniswap V3 (Arbitrum One) with real money, fully
automatically: every signal is executed immediately, with no approval step.
Read README.md and DISCLAIMER.md before running it.

On start-up it:
  1. reads your settings (.env or environment variables; only the private
     key is required) and works out your wallet address from the key;
  2. checks the pool on-chain, your balances (USDC, WBTC, ETH for gas) and
     the router approvals, and prints a summary;
  3. resumes from its saved memory (data/state.json) or, on a first start,
     warms up its indicators from about 70 days of price history (no trades
     come from the warm-up);
  4. then polls the price every 20 minutes and runs a trading cycle every
     4 hours (00, 04, 08, 12, 16, 20 UTC).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from datetime import datetime, timezone

from bot.live_config import MAINNET, LIVE_SLIPPAGE_TOLERANCE_PCT, MAX_PRICE_STALENESS_PCT
from bot.live_orchestrator import LiveOrchestrator
from bot.live_portfolio import LiveVault
from bot.live_state_store import serialize_live_state, restore_live_state
from bot.local_store import LocalStore
from bot.onchain import verify_pool, get_live_price, OnchainError
from bot.bootstrap import bootstrap_history, BootstrapError
from bot.scheduler import run_forever
from bot.settings import SettingsError, load_settings
from bot.swap_candles import candle_source_with_retries

log = logging.getLogger("lifedao-bot")

MIN_ETH_FOR_GAS = 0.0003       # a few hundred swaps' worth on Arbitrum
MIN_USDC_TO_TRADE = 60.0       # the bot always keeps $50 in USDC


def finished_candles(candles, now_ts: int) -> list:
    """Only candles whose 4-hour window has ended. The history warm-up can
    include the window still in progress (its "close" is just the latest
    price); the live loop builds and processes that window itself when it
    ends, so keeping the partial one would count it twice."""
    return [c for c in candles if c.timestamp + 4 * 3600 <= now_ts]


class CappedVault(LiveVault):
    """LiveVault with an optional upper limit on any single buy (MAX_TRADE_USD)."""

    def __init__(self, wallet_address, network, cap_usd):
        super().__init__(wallet_address, network)
        self.cap_usd = cap_usd

    def max_affordable_size(self, requested_usd: float) -> float:
        size = super().max_affordable_size(requested_usd)
        return min(size, self.cap_usd) if self.cap_usd else size


def setup_logging(data_dir) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    data_dir.mkdir(parents=True, exist_ok=True)
    fileh = logging.handlers.RotatingFileHandler(data_dir / "bot.log", maxBytes=5_000_000, backupCount=3,
                                                 encoding="utf-8")
    fileh.setFormatter(fmt)
    root.addHandler(fileh)


def build_summary(orch: LiveOrchestrator, last_price: float) -> dict:
    tb_val, rh_val = orch._position_value(last_price)
    equity = orch.vault.balance + tb_val + rh_val
    return {
        "Last updated (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "Mode": "FULLY AUTOMATIC -- entries and exits execute immediately, no approval step",
        "Wallet": orch.wallet_address,
        "Candles processed": orch.candle_index + 1,
        "Warmup complete?": "Yes" if orch.warmup_done else f"No ({orch.candle_index + 1}/300)",
        "Current BTC price ($)": round(last_price, 2),
        "Current regime": {0: "RANGING", 1: "TRENDING_UP", 2: "TRENDING_DOWN", 3: "QUIET"}.get(orch.current_regime, "n/a"),
        "USDC ($)": round(orch.vault.balance, 2),
        "TB open positions": len(orch.tb.positions),
        "RH open position?": "Yes" if orch.rh.position else "No",
        "Combined equity ($)": round(equity, 2),
        "Slippage tolerance (%)": LIVE_SLIPPAGE_TOLERANCE_PCT,
        "Price staleness limit (%)": MAX_PRICE_STALENESS_PCT,
    }


def on_cycle_complete(orch: LiveOrchestrator, rec, store: LocalStore) -> None:
    store.append_cycle_log(rec)
    store.update_summary(build_summary(orch, rec.close))
    store.save_state(serialize_live_state(orch))
    log.info("Cycle written: candle %d, regime=%s, equity=$%.2f", rec.candle_index, rec.regime_label,
             rec.combined_equity)
    for action in rec.actions:
        log.info("  -> %s", action)


class LocalCandleStore:
    """The in-progress candle (data/partial_candle.json), so a restart can
    recover it. A window the bot has already processed is never handed back,
    so a restart right after a cycle can't run that cycle twice."""

    def __init__(self, store: LocalStore, orch: LiveOrchestrator):
        self.store = store
        self.orch = orch

    def load(self):
        saved = self.store.load_partial_candle()
        if saved and self.orch.candle_history and saved.get("window_ts") is not None \
                and saved["window_ts"] <= self.orch.candle_history[-1].timestamp:
            return None
        return saved

    def save(self, builder_dict: dict) -> None:
        self.store.save_partial_candle(builder_dict)


def preflight(address: str) -> None:
    """Read-only checks and a start-up summary. Warns; never stops the bot,
    except for the pool check."""
    from bot.live_wallet import check_router_approvals, get_eth_balance
    log.info("Checking the pool on-chain...")
    try:
        verify_pool(MAINNET.pool_address)
    except OnchainError as e:
        log.error("Pool verification failed -- refusing to start: %s", e)
        sys.exit(1)
    try:
        price = get_live_price(MAINNET.pool_address)["price_usdc_per_wbtc"]
        a = check_router_approvals(list(MAINNET.rpc_urls), MAINNET.router_address, address,
                                   MAINNET.usdc_address, MAINNET.wbtc_address)
        eth = get_eth_balance(list(MAINNET.rpc_urls), address) / 1e18
        usdc = a["usdc_balance"] / 10 ** MAINNET.usdc_decimals
        wbtc = a["wbtc_balance"] / 10 ** MAINNET.wbtc_decimals
        log.info("=" * 70)
        log.info("Wallet:    %s", address)
        log.info("Network:   Arbitrum One, WBTC/USDC 0.05%% pool %s", MAINNET.pool_address)
        log.info("BTC price: $%.2f", price)
        log.info("Balances:  %.2f USDC, %.8f WBTC (~$%.2f), %.6f ETH for gas", usdc, wbtc, wbtc * price, eth)
        log.info("Approvals: USDC %s, WBTC %s", "OK" if a["usdc_ok"] else "MISSING", "OK" if a["wbtc_ok"] else "MISSING")
        log.info("=" * 70)
        if not (a["usdc_ok"] and a["wbtc_ok"]) or not a["usdc_covers_balance"]:
            log.warning("ROUTER APPROVAL MISSING OR LOW: buys and/or sells will fail until you run "
                        "`python scripts/approve_tokens.py` (one-time, see README).")
        if eth < MIN_ETH_FOR_GAS:
            log.warning("LOW ETH: %.6f ETH. Every trade pays a little gas in ETH on Arbitrum; add some ETH "
                        "(0.001 ETH is plenty) or trades will fail.", eth)
        if usdc < MIN_USDC_TO_TRADE:
            log.warning("LOW USDC: the bot always keeps $50 in USDC, so with $%.2f it can't open a trade.", usdc)
        if wbtc > 0:
            log.warning("This wallet already holds WBTC. The bot only manages positions it opened itself and "
                        "never sells WBTC it didn't buy. A dedicated wallet holding only USDC is best.")
    except Exception as e:  # noqa: BLE001 -- a pre-flight check must never crash start-up
        log.error("Could not complete the start-up checks (continuing): %s", e)


def main() -> None:
    try:
        settings = load_settings()
    except SettingsError as e:
        print(f"Setup problem: {e}")
        sys.exit(1)
    setup_logging(settings.data_dir)
    log.info("lifedao BTC trading bot starting. Data folder: %s", settings.data_dir)
    preflight(settings.wallet_address)

    store = LocalStore(settings.data_dir)
    state = store.load_state()
    if state:
        log.info("Found saved memory (candle %d) -- resuming.", state["candle_index"])
        orch = restore_live_state(state, settings.wallet_address, settings.private_key, MAINNET, store,
                                  auto_execute=True)
    else:
        log.info("No saved memory -- first start. Warming up the indicators from price history "
                 "(no trades come from this)...")
        try:
            candles = bootstrap_history(settings.coingecko_api_key)
        except BootstrapError as e:
            log.error("History warm-up failed: %s. Try again in a minute, or set COINGECKO_API_KEY "
                      "(a free key from coingecko.com).", e)
            sys.exit(1)
        candles = finished_candles(candles, int(time.time()))
        orch = LiveOrchestrator(settings.wallet_address, settings.private_key, MAINNET, store, auto_execute=True)
        for c in candles:
            orch.replay_candle_indicators_only(c)
        log.info("Warm-up complete: %d historical candles (ready to trade: %s).", len(candles), orch.warmup_done)
        store.save_state(serialize_live_state(orch))

    if settings.max_trade_usd:
        orch.vault = CappedVault(settings.wallet_address, MAINNET, settings.max_trade_usd)
        log.info("MAX_TRADE_USD: no single buy will be larger than $%.2f.", settings.max_trade_usd)

    log.warning("=" * 70)
    log.warning("FULLY AUTOMATIC MODE: entries and exits execute for REAL, immediately, with NO")
    log.warning("approval step. Stop the bot (Ctrl+C, or stop the service) to stop trading.")
    log.warning("=" * 70)

    if settings.candle_source == "swaps":
        log.info("Candles: built from every trade in the pool (the 20-minute samples are the fallback).")
        source = candle_source_with_retries
    else:
        log.info("Candles: built from 20-minute price samples (CANDLE_SOURCE=samples).")
        source = None
    run_forever(orch, lambda o, rec: on_cycle_complete(o, rec, store),
                store=LocalCandleStore(store, orch), candle_source=source)


if __name__ == "__main__":
    main()
