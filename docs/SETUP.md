# Setup, step by step

Longer form of the README's quick start, with what each step actually does and
what "working" looks like.

---

## 0. Prerequisites

- **Docker Desktop** (Mac/Windows) or Docker Engine + the compose plugin (Linux).
  Check with `docker info` — it must print server details, not a connection error.
- About **3 GB** of disk for the image, plus ~500 MB for the candle cache.
- A Telegram account.

No Python, no TA-Lib, no pip installs on your machine.

---

## 1. Create the config files

```bash
make setup
```

Copies `.env.example` → `.env` and `secrets/.env.dev.example` → `secrets/.env.dev`,
creates the data directories, and checks Docker is running. Existing files are
never overwritten.

---

## 2. Telegram credentials

### The bot token

@BotFather → `/newbot` → name → username ending in `bot` → it replies with:

```
Use this token to access the HTTP API:
8123456789:AAF-abcdefghijklmnopqrstuvwxyz
```

### Your chat id

Message [@userinfobot](https://t.me/userinfobot). It replies with `Id: 5058733760`.

### Fill them in

```ini
# secrets/.env.dev
TOKEN='8123456789:AAF-abcdefghijklmnopqrstuvwxyz'
chat_idADMIN='5058733760'
CHAT_ID='5058733760'
```

`CHAT_ID` is only read by PKScreener's own bot/barometer modes. Setting it to the
same value is fine.

> **Telegram will not let a bot start a conversation.** Open your new bot's chat
> and send it `/start` before going further, or every message will vanish.

### Why three keys for two values

`TOKEN` and `CHAT_ID` are the names PKScreener's own code expects, so one file
serves both PKScreener and our runner. One quirk: PKScreener prepends a `-` to
`CHAT_ID` at send time (it assumes a channel). Our runner detects which form you
used and does the right thing either way.

---

## 3. Build

```bash
make build
```

Pulls `pkjmesra/pkscreener:latest` (~2.5 GB, once) and layers our `custom/` code
on top. Takes a few minutes on a first run.

---

## 4. Verify Telegram

```bash
make check-telegram
```

Expected:

```
OK: sent a test message as @your_bot to chat 5058733760
```

and a message in your Telegram. If it prints `FAILED: ... [401]` the token is
wrong; `[400] chat not found` means the chat id is wrong or you never messaged
the bot.

---

## 5. Download market data

```bash
make data
```

Runs PKScreener's downloader for every NSE stock. **The first run is slow** —
plan on 10–30 minutes depending on your connection. It lands in `data/` on your
host, so it survives container restarts and only needs re-running once a day.

Check it worked:

```bash
ls -lh data/results/Data/ data/actions-data-download/
```

You want a `stock_data_<DDMMYYYY>.pkl` of a few hundred MB.

---

## 6. Dry run

```bash
make dry-run
```

Runs your indicator over the whole market and prints the alert to the terminal
instead of sending it. This is the loop you will live in while tuning rules.

```
Scanned 1972 stocks in 19.8s -> 13 match(es)

----- TELEGRAM (dry run) -----
Momentum breakout
13 matches out of 1972 stocks scanned:
Symbol    Signal      Price    RSI    Vol x
--------  --------  -------  -----  -------
TATAPOWER BUY        477.00  68.07     3.17
...
```

Narrow it down while iterating:

```bash
make dry-run SYMBOLS=RELIANCE,TCS,INFY
```

---

## 7. Send one for real

```bash
make alert
```

Same scan, actually delivered. Check your phone.

---

## 8. Go live

```bash
make up      # starts the scheduler in the background
make logs    # follow it
```

It scans at the times in `PKS_RUN_AT` (default 15:45 and 09:05 IST), skipping
weekends. `restart: unless-stopped` means it comes back after a reboot as long as
Docker is set to start on login.

Stop with `make down`.

---

## 9. PKScreener's own bot (optional)

```bash
make bot
make bot-logs
```

Runs upstream's interactive Telegram bot with your token, so you can trigger
their 50 built-in scanners from your phone. Independent of the `alerts` service —
run either, or both.

---

## Daily rhythm

| When | Command | Why |
|---|---|---|
| Once, at setup | `make build` | Get the image |
| Every morning | `make data` | Refresh candles |
| While tuning | `make dry-run` | See hits without sending |
| Always on | `make up` | Scheduled alerts |
| Occasionally | `make build` | Pick up upstream's latest |

Automating the daily refresh is a one-line cron entry:

```cron
30 16 * * 1-5 cd /path/to/Trading && make data >> data/refresh.log 2>&1
```

---

## Changing the schedule

`.env`:

```ini
PKS_RUN_AT=09:05,12:00,15:45      # three IST times, trading days only
```

or

```ini
PKS_RUN_AT=
PKS_INTERVAL_MINUTES=60           # hourly instead
```

Then `docker compose up -d alerts` to apply.

---

## Running the tests

The custom code has a test suite that needs no Docker and no market data:

```bash
pip install pandas numpy pytest requests python-dotenv tabulate
make test
```

It covers the indicator maths (RSI is checked against Wilder's published
worked example), the cache reader, the Telegram transport, the scheduler, and a
full scan end to end against a synthetic market.
