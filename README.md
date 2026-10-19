# The Lifedao BTC Trading Bot

An automatic trading bot that trades **WBTC/USDC on Uniswap V3 (Arbitrum One)** with real money, from your own wallet. You give it the private key of a dedicated wallet holding USDC. It runs 24/7, and every 4 hours it decides whether to buy or sell. **It executes immediately, with no approval step.**

> **Read [DISCLAIMER.md](DISCLAIMER.md) first.** This is experimental software that trades real money. Use a **new wallet** holding only what you can afford to lose. It is not financial advice.

## What it does

The bot runs two strategies, which share the wallet's USDC:

- **Trend Bot.** It buys when a strong uptrend is confirmed and the price is above its 20-candle average:
  - ADX above 42 and EMA20 above EMA50;
  - RSI between 45 and 75, MACD above its signal line, and volatility (ATR) at most 8%.

  Each trade uses 20–25% of the account. It holds at most 2 trades at once, with at most 40% of the account in them. A trade is sold at whichever comes first:
  - a 5% stop-loss;
  - a trailing stop (armed at +3%, set 1.5 × ATR below the peak);
  - the trend weakening;
  - a 2-day limit.
- **Range Harvester.** It buys a rebound from an oversold dip in a sideways market: the price climbs back above the lower Bollinger Band after an RSI below 35. Each trade uses 65% of the account, one trade at a time. A trade is sold at whichever comes first:
  - after the price reaches the middle of its range, on a pullback (1.5 × ATR);
  - a 7% stop-loss;
  - a 5-day limit (suspended if a strong uptrend takes over, when it switches to a trailing stop).

Some rules apply to everything:
- **Candles:** 4-hour candles (UTC), each built from every trade in the pool.
- **Reserve:** **$50 always stays in USDC**.
- **Circuit breaker:** stops a strategy for the day after losses of 7% of the day's opening value.
- **Guards:** a trade is skipped if the price moved more than 1% since the decision, or if the swap would get more than 0.5% less than expected.

## What you need

1. **A new wallet** (for example, a new account in MetaMask) and its **private key**.
2. On **Arbitrum One**, in that wallet:
   - **USDC** (native USDC, `0xaf88d065e77c8cC2239327C5EDb3A432268e5831`). At least $60, since $50 always stays in reserve and trades are a share of the rest.
   - A little **ETH for gas**. 0.001 ETH is plenty; each trade costs about $0.01.
3. **Python 3.11 or newer** (python.org) if you run it on your own computer, or a **Railway** account if you run it in the cloud.

Start with a wallet holding **only USDC**. The bot manages only the positions it opens itself and never sells WBTC it didn't buy.

## Set up (once)

### 1. Get the code and install
On Windows (PowerShell), from the folder where you downloaded or cloned this repository:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

On macOS or Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 2. Add your private key
Copy `.env.example` to a new file named `.env` in the same folder, open it, and replace `your-private-key-here` with your wallet's private key. Save it.

- `.env` never leaves your computer and is excluded from Git. **Never share it, commit it or paste it anywhere.**
- Optional: set `MAX_TRADE_USD` (for example `25`) to cap every buy while you get familiar with the bot.

### 3. Check your setup (read-only, free)
```powershell
.venv\Scripts\python scripts\check_setup.py
```
This shows the wallet address worked out from your key, your USDC, WBTC and ETH balances, and whether the approvals are in place. It then lists anything still needed.

### 4. Approve the tokens (one time)
```powershell
.venv\Scripts\python scripts\approve_tokens.py
```
Uniswap's router can only trade your USDC and WBTC once you approve it. The script sends up to two small transactions, costing a fraction of a cent each.
- It simulates each one first and asks you to type `YES` before sending.
- It moves no tokens.
- It's safe to run again.

### 5. Start the bot
```powershell
.venv\Scripts\python run_bot.py
```
On first start it prints a summary (wallet, balances, approvals), then warms up its indicators from about 70 days of price history; no trades come from the warm-up. After that:
- it polls the price every 20 minutes;
- it runs a trading cycle just after each 4-hour mark: 00, 04, 08, 12, 16 and 20 UTC.

**To stop it, press Ctrl+C.** Open positions stay in your wallet, and the bot picks them up again when you restart it.

On your own computer, the bot only trades while the computer is on, awake and online. For 24/7 running, use Railway (below) or another always-on machine.

## Where to see what it's doing

Everything is in the `data` folder:

| File | What's in it |
|---|---|
| `cycle_log.csv` | One row per 4-hour cycle: price, market regime, USDC, position values, total equity, what happened |
| `trades.csv` | Every executed swap, with its transaction hash (look it up on arbiscan.io) |
| `proposals.csv` | Every signal and its outcome (executed, failed, skipped by a guard) |
| `summary.json` | The latest overview |
| `bot.log` | The full log |
| `state.json` | **The bot's memory.** Don't edit or delete it while positions are open, or the bot will forget them |

The `.csv` files open in Excel or Google Sheets.

## Run it 24/7 on Railway

1. **Create the service:**
   1. Sign in at railway.app.
   2. Click **New Project → Deploy from GitHub repo** and pick your copy of this repository. Your GitHub account needs access to it.
2. **Settings → Deploy:**
   - Set **Custom Start Command** to `python run_bot.py`.
   - Set **Restart Policy** to **On Failure**.
   - Keep **Serverless** off: a sleeping bot can't watch prices.
3. **Add a volume. This is essential.** Railway's disk is wiped on every restart and redeploy, and without a volume the bot would forget its open positions.
   1. Right-click the service and choose **Attach Volume**.
   2. Set its mount path to `/data`.
4. **Variables:**
   - `WALLET_PRIVATE_KEY`: your key. Railway stores it encrypted; never put it in the repository.
   - `DATA_DIR`: `/data`.
   - Optional: `MAX_TRADE_USD`.
5. **Run the approval script once on your own computer** (step 4 above). Railway can't answer the `YES` prompt.
6. **Check it's running.** Open **Deployments**. The newest deployment should show **Active**, and its logs should show the start-up summary and then lines like "Sampled price".

The bot's log appears in Railway's **Deploy Logs**. The data files are on the volume.

## Settings

| Setting | Required | What it does |
|---|---|---|
| `WALLET_PRIVATE_KEY` | Yes | The private key of the bot's wallet. The address is worked out from it. Never printed or logged |
| `MAX_TRADE_USD` | No | Upper limit on any single buy, in USD. Empty means no limit |
| `DATA_DIR` | No | Where memory and logs are kept. Default: `./data`; on Railway, your volume path (`/data`) |
| `CANDLE_SOURCE` | No | `swaps` (default): candles from every trade in the pool. `samples`: from 20-minute price samples |
| `COINGECKO_API_KEY` | No | Only for the one-time history warm-up, if CoinGecko refuses requests without a key |
| `WALLET_ADDRESS` | No | If set, must match the key's address (a safety check) |

Settings can live in `.env` or be set as environment variables. Environment variables take priority.

## Good to know

- **Trades are real and immediate.** Every signal is signed and sent straight away. Stopping the bot is the only way to pause trading.
- **Exits are only checked every 4 hours,** so a fast move can take a loss beyond the stop-loss level before the bot acts.
- **Don't run two copies on the same wallet.** They'd each think they own the positions.
- **Don't move USDC or WBTC in or out while a position is open.** Adding USDC while the bot has no position is fine; it uses the new balance from the next cycle.
- **If a trade fails,** for example because ETH for gas ran out or an approval is missing, it's marked FAILED in `proposals.csv`. The strategy re-checks on the next cycle.

## Running the tests
```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest tests
```
The tests run offline: nothing is signed or sent.
