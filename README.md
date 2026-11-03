# NDBOT — Telegram trading bot for NEAR memecoins

**Trade NEAR memecoins at meme speed, right inside Telegram:** instant token cards, one-tap buys and sells, a deployer sniper for nearly.trade and JustHoot, copytrade, limit orders with TP/SL, and shareable PnL cards. Wallet keys are encrypted per user, and every transaction is built and signed by the bot's own code — no third-party NEAR SDK.

**Try it live:** [@ndegen_bot](https://t.me/ndegen_bot) · 🇷🇺 [README на русском](README.ru.md) (server setup guide)

Built at **NEAR HACKATHON** — tracks: *Unstoppable DeFi*, *Memecoins With Mechanics*, *Social Money*, *Open Innovation*.
Second project of the same author: **[NEAR Analyzer Bot](https://github.com/LoveTon-crypto/near-analyzer-bot)** (scanner, wallet PnL, memepad alerts, deploys feed). The two work together: every analyzer alert has a **⚡ Buy on NDbot** button that opens this bot on that token — see [Works with NEAR Analyzer](#works-with-near-analyzer).

---

## Why

Memecoins on NEAR launch on memepads (**nearly.trade**, **JustHoot**) and trade on **Ref Finance / Ref DCL** pools. On TON and Solana, traders have Telegram trading bots (DTrade, Trojan, BonkBot…) with sniping, copytrade and limit orders. NEAR had none of that: you had to open a web wallet, find the right pool, sign every swap by hand and track PnL in a spreadsheet. NDBOT brings the full trading-bot toolkit to NEAR.

## Features

| Feature | What it does |
|---|---|
| 👛 **Wallets** | Create a wallet (BIP39 12-word seed, NEAR path m/44'/397'/0' — compatible with Meteor / MyNearWallet) or import by seed phrase / private key; the bot finds named `name.near` accounts bound to the key. Up to 10 wallets per user, switch the active one, rename, show seed (spoiler, auto-deleted), transfer NEAR and tokens, delete |
| 📊 **Instant token cards** | Paste a contract or a nearly.trade / JustHoot / NearBlocks link → price in $ and NEAR, MC, liquidity, 24h volume and txns, ATH MC, age, socials, 15-minute candle chart, your position with invested / current value / PnL, and the pool the swap will use. All data is fetched in parallel |
| ⚡ **One-tap trading** | Buy presets (0.5 / 1 / 2 / 5 NEAR, editable) or a custom amount; sell 25 / 50 / 100 % or a custom %. Per-user slippage |
| 🔀 **Smart routing** | Picks the deepest wNEAR pool across classic Ref pools and Ref DCL (concentrated liquidity), including multi-hop DCL routes (wNEAR → base token → memecoin) for tokens paired against something other than wNEAR. Quotes come straight from the contracts |
| 🎯 **Deployer sniper** | Follow dev wallets. When one deploys a new token on nearly.trade or JustHoot, the bot buys automatically (waits for the pool if it appears a few blocks later) and places take-profit / stop-loss orders. Per dev: amount, slippage, TP % and share to sell, SL %, every deploy or next one only, wallet |
| 🔁 **Copytrade** | Mirror any wallet in real time: fixed amount or % of the leader's trade with a cap, min leader trade filter, sells copied as the same share of the position. Multi-hop swaps are merged into one net trade; stale or missed trades are never copied |
| 📋 **Limit orders** | Buy the dip / breakout, take-profit, stop-loss — target in NEAR price, $ price, % or market cap. Checked every 10 s; when a position is fully closed, the remaining sell orders for it are cancelled |
| 🖼 **PnL cards** | After every sell (manual, order, copytrade) the bot sends a share-ready PnL card with the user's referral link; random characters with a green/red chart, or the user's own uploaded pictures |
| 🤝 **3-level referrals** | Each user gets a referral link; inviters earn a share of the bot fee from 3 levels (30 / 5 / 3 %) and withdraw it to their wallet in one tap |
| 📈 **Positions** | All tokens on the active wallet with value in NEAR and PnL |
| 🌐 **EN / RU** | Language follows Telegram, switchable in settings |
| 🛠 **Admin** | `/admin` — fees, referral liabilities, payout wallet; `/rpc` — RPC node health; `/backup` + daily automatic database backup to the admins' Telegram |

### Business model
A 1 % fee (`FEE_BPS`) on each trade is sent to the fee account in a separate transfer after a successful swap (retried in the background if it fails). Referral rewards are paid from it. This keeps the bot sustainable without tokens or subscriptions.

## Architecture

```
Telegram ─► bot.py ── UI, handlers, orders, sniper, copytrade, referrals, PnL (python-telegram-bot)
              │
              ├─ wallets.py   BIP39 + SLIP-10 ed25519 derivation, AES-256-GCM vault (own code, no NEAR SDK)
              ├─ near.py      transaction building / signing (borsh), RPC client with node scoring + hedged reads
              ├─ ref.py       Ref Finance + Ref DCL: pool discovery, quotes, multi-hop routes, swaps, storage
              ├─ market.py    DexScreener, GeckoTerminal, nearly.trade API → card data and candle charts
              ├─ snipe.py     new-token stream (Intear WS) + deploy signer lookup (neardata blocks)
              ├─ copytrade.py leader trades stream (Intear WS), per-transaction aggregation
              ├─ pnl_card.py  PnL card renderer (Pillow) with pnl_images/ characters
              ├─ i18n.py      RU → EN translation layer for every outgoing message and button
              └─ db.py        SQLite: users, wallets (encrypted), trades, orders, fees, referrals, snipe / copy targets
```

On-chain flow of a buy: `wrap.near` `near_deposit` + `ft_transfer_call` to `v2.ref-finance.near` (classic pool) or `dclv2.ref-labs.near` (DCL `Swap` with one or two `pool_ids`) in a single transaction, with `min_output_amount` from the on-chain quote and the user's slippage. Storage deposits for the token are added automatically; leftover wNEAR is unwrapped back to NEAR.

## Security

| What | How |
|---|---|
| Wallet keys | Stored only encrypted: AES-256-GCM under `MASTER_KEY`, each record bound to its user and account (AAD), so records can't be swapped between users. A leaked database alone reveals nothing |
| Seed phrases | Deleted from the chat right after import; shown only after confirmation, under a spoiler, protected from forwarding, auto-deleted after 2 min |
| Signing | Own ed25519 / borsh code in `near.py`, verified with BIP39, SLIP-10 and near-seed-phrase test vectors (`tests/test_wallets.py`). One nonce lock per wallet, so parallel orders never collide |
| Access | Each user sees and signs only their own wallets; groups and channels are ignored |
| Limits | Max NEAR per buy, gas reserve, max wallets / orders / snipe / copy targets per user |
| Server | Dedicated VPS, systemd sandbox (`ProtectSystem=strict`, no shell user), firewall with only SSH open, fail2ban, key-only SSH; `.env` and the database are `chmod 600` |

**Rule for users:** keep only a trading bank in the bot; main funds stay in your own wallet. Seed phrases created by the bot work in any NEAR wallet.

## Run it

Requires Python 3.11+ and a bot token from [@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/LoveTon-crypto/near-trade-bot && cd near-trade-bot
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env
# put BOT_TOKEN in .env and generate the vault key:
echo "MASTER_KEY=$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')" >> .env
venv/bin/python bot.py
```

Open the bot in Telegram → `/start` → **💼 Wallet → ➕ Create** → top up the address with a little NEAR → paste a token, e.g. `batman-4.nearlytrade.near`, and tap a buy button. Start with small amounts.

Production install on a hardened server (`server-setup.sh`, `deploy.sh`, `tradebot.service`, one-command updates) is described step by step in [README.ru.md](README.ru.md).

### Tests

```bash
venv/bin/python tests/test_wallets.py   # BIP39 / SLIP-10 / NEAR key derivation and vault, offline
venv/bin/python tests/test_i18n.py      # every user-facing Russian string has an English translation
```

### Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | Telegram bot token |
| `MASTER_KEY` | — | 32-byte base64 vault key. **Back it up offline** — without it no wallet in the database can be opened |
| `ADMIN_IDS` | — | Telegram ids allowed to use `/admin`, `/rpc`, `/backup` |
| `MAX_BUY_NEAR` / `GAS_RESERVE_NEAR` | 100 / 0.1 | Per-buy cap and NEAR kept for gas |
| `FEE_BPS` / `FEE_ACCOUNT` / `REF_SHARES` | 100 / … / 30,5,3 | Bot fee, fee wallet, referral shares per level |
| `RPC_URLS` / `FASTNEAR_API_KEY` | free public nodes | RPC list (fastest healthy node is preferred) and optional paid key |
| `ORDER_POLL_SEC` | 10 | Limit-order price check interval |
| `SNIPE_POOL_WAIT_SEC` | 45 | How long the sniper waits for the pool after a deploy |
| `MAX_WALLETS` / `MAX_ORDERS` / `MAX_SNIPE_TARGETS` / `MAX_COPY_TARGETS` | 10 / 20 / 10 / 10 | Per-user limits |
| `BACKUP_TIME_UTC` | 00:17 | Daily database backup to admins |

## Works with NEAR Analyzer

[NEAR Analyzer Bot](https://github.com/LoveTon-crypto/near-analyzer-bot) ([@nearai_agent](https://t.me/nearai_agent)) is the research side: token scanner, wallet PnL, memepad MC / dip alerts and the [new deploys channel](https://t.me/neardeploys). NDBOT is the execution side. Each analyzer alert and scan has a **⚡ Buy on NDbot** button — a deep link `t.me/ndegen_bot?start=c<token>` that opens this bot straight on the token card, so a trader goes from signal to filled order in two taps. The deploys channel shows each dev's history; add a dev you like to the 🎯 sniper and the bot buys their next launch automatically.

## Data sources

NEAR RPC (FastNear, Intear, Lava, dRPC and other public nodes), Ref Finance and Ref DCL contracts, [Intear](https://intea.rs) events WebSocket (copytrade, new tokens), [neardata.xyz](https://neardata.xyz) blocks (deploy signer), DexScreener, GeckoTerminal, nearly.trade API.

## Roadmap

- Sniping by ticker / keyword and on first-pool creation for any token
- Multi-wallet bundle buys
- Trailing stop and DCA orders
- Mini App with charts and portfolio view
