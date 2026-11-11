# The Lifedao BTC Trading Bot

An automatic trading bot that trades **WBTC/USDC on Uniswap V3 (Arbitrum One)** with real money, from your own wallet. You give it the private key of a dedicated wallet holding USDC. It runs 24/7, and every 4 hours it decides whether to buy or sell. **It executes immediately, with no approval step.**

> **Read [DISCLAIMER.md](DISCLAIMER.md) first.** This is experimental software that trades real money. Use a **new wallet** holding only what you can afford to lose. It is not financial advice.

## What it does

The bot runs two strategies at a time, which share the wallet's USDC: the Trend Core (or, if you switch it off, the Trend Bot) and the Range Harvester.

- **Trend Core** (the default). It holds an amount of BTC that follows the strength of the trend, from nothing in a downtrend up to most of the account in a strong uptrend. It adjusts that amount every 4 hours.
  - **Trend score (0–20):** built from 4 moving-average gaps (24/96 up to 192/768 candles) and 4 breakout measures (60 to 480 candles). Each is scaled to the same size, then they're averaged. A downtrend scores 0.
  - **Size:** account value × score/10 × 25% ÷ BTC's annual volatility, and at most the account minus the $50 reserve. So it holds less when BTC is wild and more when it's calm.
  - **Trades:** only when the gap to the target is more than 10% of a typical position and at least $10, which avoids constant small trades.
  - It has no stop-loss of its own: it sells as the trend score falls. It needs 128 days of history, which it downloads from Binance's public price data on the first start.
  - Based on Robert Carver's *Systematic Trading*. In the original project's tests on the pool's own trades (2023–2026), it gained much more than the Trend Bot in rising markets, lost more in sideways, falling and crash periods, and had deeper drops (about −24% at worst). Past results don't predict future ones.
- **Trend Bot** (only with `TREND_CORE=off`). It buys when a strong uptrend is confirmed and the price is above its 20-candle average:
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
- **Circuit breaker:** stops the Trend Bot or the Range Harvester for the day after losses of 7% of the day's opening value.
- **Guards:** a trade is skipped if:
  - the price moved more than 1% since the decision;
  - the pool's price is more than 1% away from its own 30-minute average (TWAP), which protects against trading on a brief price spike;
  - the swap would get more than 0.5% less than expected.

  A skipped trade is re-checked at the next cycle. Each candle also counts only the final price of each transaction, so a price spike inside one transaction can't distort the indicators.

## What you need

1. **A new wallet** (for example, a new account in MetaMask) and its **private key**.
2. On **Arbitrum One**, in that wallet:
   - **USDC** (native USDC, `0xaf88d065e77c8cC2239327C5EDb3A432268e5831`). At least $60, since $50 always stays in reserve and trades are a share of the rest.
   - A little **ETH for gas**. 0.001 ETH is plenty; each trade costs about $0.01.
3. To run it on your own computer: nothing else. The start file helps you install Python 3.11+ if it's missing. To run it 24/7 in the cloud: a **Railway** account (about $5 a month).

Start with a wallet holding **only USDC**. The bot manages only the positions it opens itself and never sells WBTC it didn't buy.

## Start it on your computer (easiest)

1. **Download this project:** on GitHub, click the green **Code** button, then **Download ZIP**. Unzip it into a folder.
2. **Start it:**
   - **Windows:** double-click **`Start Bot (Windows).bat`**. If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**.
   - **Mac:** double-click **`Start Bot (Mac).command`**. The first time, macOS may refuse to open it: right-click it, choose **Open**, then **Open** again.
   - **Linux:** run `bash start.sh`.
3. **Follow the steps in the window:**
   1. If Python isn't installed, it tells you how to get it. On Windows it can install it for you.
   2. The first time, it installs what the bot needs. That takes a few minutes.
   3. It asks for your wallet's **private key**. Paste it and press Enter. Nothing shows as you paste; that's normal. It shows the wallet address the key belongs to, so you can check it's the right wallet. The key is saved only in the `.env` file in this folder.
   4. It checks your wallet: USDC, ETH for gas, and the approvals. If something is missing, it says what to send and waits.
   5. **One-time approvals.** Uniswap's router can only swap your USDC and WBTC once you approve it. The window explains this and asks you to type `YES`. It's two small transactions costing a fraction of a cent each, and it moves no tokens.
   6. It starts the bot.

**After the first time, just double-click the same file:** it goes straight to the checks and starts the bot.

- **Keep the window open.** Closing it, or pressing Ctrl+C, stops the bot. Open positions stay in your wallet, and the bot picks them up again when you restart it.
- **While it runs, the computer won't go to sleep by itself.** But it must stay on and online, and closing a laptop's lid still sleeps it. If the computer is off when a cycle is due, the bot catches up when it's back, so it may sell later than planned. For 24/7 running, use Railway (below).
- **Every 4 hours the window shows a status line:** BTC price, the Trend Core's score and holding, USDC, and the total.
- **To use a different wallet,** run `python start.py --new-key` from this folder. If the old wallet's memory is still in the `data` folder, it offers to rename that folder first (it never deletes it).
- **Optional settings** such as `MAX_TRADE_USD` go in the `.env` file. Open it in Notepad or TextEdit; see **Settings** below.

### The manual way (for developers)
```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python
cp .env.example .env                                     # then put your key in .env
.venv/bin/python scripts/check_setup.py                  # read-only check
.venv/bin/python scripts/approve_tokens.py               # one-time approvals (type YES)
.venv/bin/python run_bot.py
```

## Where to see what it's doing

Everything is in the `data` folder:

| File | What's in it |
|---|---|
| `cycle_log.csv` | One row per 4-hour cycle: price, market regime, USDC, position values, total equity, what happened |
| `trades.csv` | Every executed swap, with its transaction hash (look it up on arbiscan.io) |
| `proposals.csv` | Every signal and its outcome (executed, failed, skipped by a guard) |
| `summary.json` | The latest overview, including the Trend Core's score, target and holding |
| `bot.log` | The full log |
| `wallet.txt` | Which wallet this folder belongs to. The bot refuses to use one wallet's memory with another wallet's key |
| `state.json` | **The bot's memory.** Don't edit or delete it while positions are open, or the bot will forget them |

The `.csv` files open in Excel or Google Sheets.

## Run it 24/7 on Railway

Railway keeps the bot running around the clock, about $5 a month. Your key stays in your own Railway account.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/G59rx7?referralCode=o_9ppa)

1. **Click the button above** and sign in to Railway (or create an account).
2. **Paste your wallet's private key** when Railway asks for `WALLET_PRIVATE_KEY`. Everything else is already set: the storage volume, `DATA_DIR=/data`, `AUTO_APPROVE=yes` (the bot sends the one-time approvals itself on its first start) and `TREND_CORE=on`.
3. **Click Deploy.** After a few minutes, open the service, then **Deployments**, then **View logs**. You should see the start-up summary with your wallet's address and balances, then "Router approvals are in place", then "Trend Core: … ready: True".

Make sure the wallet already holds USDC (at least $60) and a little ETH for gas on Arbitrum One before you deploy.

**Never deploy a second copy for the same wallet,** and don't run it on your computer at the same time: two bots on one wallet would each think they own the money.

### Setting it up by hand instead
If you've changed the code, deploy your own copy instead of the button:

1. **Get your own copy of this project on GitHub:** click **Fork** at the top of this page.
2. **Create the service:**
   1. Sign in at railway.app.
   2. Click **New Project → Deploy from GitHub repo** and pick your copy.
   3. The start command, restart policy and "never run two copies" settings come from `railway.json` in this project; you don't need to set them.
3. **Add a volume. This is essential.** Without one, the bot would forget its open positions on every restart. The deploy refuses to run without it.
   1. Right-click the service and choose **Attach Volume**.
   2. Set its mount path to `/data`.
4. **Variables:** open the service's **Variables** tab and add:
   - `WALLET_PRIVATE_KEY`: your key. Railway stores it encrypted; never put it in the repository.
   - `DATA_DIR`: `/data`.
   - `AUTO_APPROVE`: `yes`. The bot then sends the one-time approvals itself on its first start, so you don't need a computer for that step.
   - Optional: `MAX_TRADE_USD`, `TREND_CORE`.
5. **Check it's running.** Open **Deployments**. The newest deployment should show **Active**, and its **Deploy Logs** should show the start-up summary, then "Router approvals are in place", then lines like "Sampled price".

The bot's log appears in Railway's **Deploy Logs**. The data files are on the volume. If a deployment fails while "downloading" its build tools, that's usually a temporary GitHub problem: open the deployment's **⋯** menu and choose **Redeploy**.

## Settings

| Setting | Required | What it does |
|---|---|---|
| `WALLET_PRIVATE_KEY` | Yes | The private key of the bot's wallet. The address is worked out from it. Never printed or logged |
| `MAX_TRADE_USD` | No | Upper limit on any single buy, in USD. Empty means no limit |
| `DATA_DIR` | No | Where memory and logs are kept. Default: `./data`; on Railway, your volume path (`/data`) |
| `AUTO_APPROVE` | No | `yes`: if the one-time approvals are missing, the bot sends them itself at start-up (for Railway). Default `no`: the Start Bot file asks you instead |
| `TREND_CORE` | No | `on` (default): Trend Core + Range Harvester. `off`: Trend Bot + Range Harvester; any WBTC the Trend Core holds is sold at the next cycle |
| `CANDLE_SOURCE` | No | `swaps` (default): candles from every trade in the pool. `samples`: from 20-minute price samples |
| `COINGECKO_API_KEY` | No | Only for the one-time history warm-up, if CoinGecko refuses requests without a key |
| `WALLET_ADDRESS` | No | If set, must match the key's address (a safety check) |
| `SAVINGS_VAULT_MODE` | No | The optional Savings Vault, **for whitelisted wallets only** (see below). `off` (default), `dry_run`, `on`, `withdraw_all` |

Settings can live in `.env` or be set as environment variables. Environment variables take priority.

## Optional: Savings Vault (whitelisted wallets only)

**This is off by default, and it will not work for most people.** It only works if the developers of the Savings Vault (TakafulDAO) have registered *your* wallet as a member of their vault. With any other wallet the vault refuses every deposit, and the bot just reports "this wallet is not a registered member of the vault". If nobody has told you your wallet is registered, leave it off.

What it does when it's on: USDC the strategy isn't using is deposited into the Savings Vault (`0x6d1bD94Cd572e51Aa018aE6Bf909BA61F8F8d4f9` on Arbitrum One, the only vault the bot will ever use) after each 4-hour cycle, where it earns a small yield. Before a buy, the bot takes out exactly what the trade needs. The vault sometimes pays part of a withdrawal in USDT; the bot swaps that back to USDC. Your total, profit figures and the strategy's sizing include the money in the vault.

- **Modes** (`SAVINGS_VAULT_MODE`): `off` (default); `dry_run` writes what it *would* deposit and withdraw to `data/savings_log.csv` and sends nothing (try this first); `on`; `withdraw_all` is the emergency switch: the bot takes everything out of the vault (as fast as the vault's 24-hour withdrawal limits allow) and never deposits again.
- **Limits:** `SAVINGS_MAX_DEPOSIT_USD` (largest single deposit, default 250), `SAVINGS_MIN_DEPOSIT_USD` (smallest, default 50), `SAVINGS_CASH_BUFFER_USD` (USDC always left in the wallet, default 5), `SAVINGS_MIN_ETH` (no vault transaction below this much ETH for gas, default 0.0005), `SAVINGS_EXPECTED_IDLE_DAYS` (a deposit has to earn back its gas within this many days, default 30). Approvals are always for the exact amount, never unlimited.
- **The vault has its own safety limits.** Withdrawals are capped per 24 hours, per member and for the whole vault. Going over them pauses the vault for everyone, so the bot checks before every withdrawal and never goes near them. If money can't come out in time, the trade that needed it is skipped and the reason is logged.
- **By hand:** `python scripts/savings_vault.py status` shows whether your wallet is registered and what's in the vault; `deposit`, `withdraw` and `withdraw-all` do it yourself, after you type YES.
- Every deposit, withdrawal and alert is in `data/savings_log.csv`; the cycle log has two extra columns for the vault's balance and yield.

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
