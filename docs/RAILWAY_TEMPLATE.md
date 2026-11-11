# Making the "Deploy on Railway" button (for the maintainer, once)

**Done on 2026-11-09:** the template is https://railway.com/deploy/G59rx7 and the README links to it. These notes are for rebuilding it.

A Railway template turns the Railway setup into: click the button, sign in, paste a private key. Templates are created in a Railway account, so the maintainer makes it once. Nobody else's keys ever reach the maintainer: each person's deploy runs in their own Railway account.

1. Sign in at railway.app. Open your workspace's **Templates** page (Workspace settings → Templates) and click **New Template**.
2. **Add a service → GitHub repo**, and choose this repository (`the-lifedao-btc-trading-bot-shareable`, branch `main`).
3. In the service's settings in the template editor:
   - **Volume:** add one, mount path `/data`.
   - **Variables:**

     | Name | Value | Notes |
     |---|---|---|
     | `WALLET_PRIVATE_KEY` | *(leave empty)* | Mark it **required**. Description: "The private key of a NEW wallet used only for this bot, holding USDC and a little ETH on Arbitrum One." |
     | `DATA_DIR` | `/data` | |
     | `AUTO_APPROVE` | `yes` | Sends the one-time router approvals on the first start |
     | `TREND_CORE` | `on` | |
     | `MAX_TRADE_USD` | *(empty)* | Optional. Description: "Upper limit on any single buy, in USD. Empty = no limit." |

     Don't add the `SAVINGS_*` variables to the template: the Savings Vault is off by default and only works for wallets the vault's developers have registered. Anyone who has one can add `SAVINGS_VAULT_MODE` to their own service.

   - Start command, restart policy and the no-overlap setting come from `railway.json`; there's nothing to set.
4. Click **Create Template**. If you want it listed publicly on Railway, also click **Publish**. If you don't, the link still works for anyone who has it.
5. Copy the template's link (`https://railway.com/deploy/...`) and add the button to the README's "Run it 24/7 on Railway" section:

   ```markdown
   [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/YOUR-TEMPLATE-CODE)
   ```

Deploys made from the template use the repository you chose. People who want to change the code should fork it and deploy their fork instead.
