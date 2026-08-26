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

Creates `.env` from the template and the data directories, and checks Docker
is running, if you haven't already. Existing files are never overwritten.
`secrets/.env.dev` doesn't need this step — see below.

---

## 2. Telegram credentials

`secrets/.env.dev` is **already committed to this repo with real
credentials** — the owner's explicit choice, given the repo is public, in
exchange for a genuinely zero-setup clone. Nothing to fill in; `make
check-telegram` (step 4) should just work.

```ini
# secrets/.env.dev, already in the repo
TOKEN='8846...'          # a real bot token
chat_idADMIN='8096928582'
CHAT_ID='8096928582'
```

### Why three keys for two values

`TOKEN` and `CHAT_ID` are the names PKScreener's own code expects, so one file
serves both PKScreener and our runner. One quirk: PKScreener prepends a `-` to
`CHAT_ID` at send time (it assumes a channel). Our runner detects which form
you used and does the right thing either way.

### Using your own bot instead

1. @BotFather → `/newbot` → name → username ending in `bot` → it replies with
   a token like `8123456789:AAF-abcdefghijklmnopqrstuvwxyz`.
2. Message [@userinfobot](https://t.me/userinfobot) → it replies with
   `Id: 5058733760`.
3. Put both in `secrets/.env.dev` as `TOKEN` and `chat_idADMIN`.
4. **Send your new bot any message** (`/start` works) before testing it —
   Telegram won't let a bot speak first, so alerts go nowhere silently
   without this step.

### Rotating or protecting the committed token

- **Revoke it**: @BotFather → `/revoke` (or `/token`) on the bot. The old
  token stops working immediately; put the new one in `secrets/.env.dev`.
- **Make the repo private**: GitHub Settings → General → Danger Zone. No code
  changes needed — the same committed file just stops being publicly visible.
- **Un-commit it**: add `secrets/.env.dev` back to `.gitignore`, keep the real
  values local-only, and add `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` under
  repo Settings → Secrets and variables → Actions for CI instead.

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

```bash
pip install -r requirements-test.txt
make test
```

That's the whole suite: indicator maths (RSI checked against Wilder's
published worked example), the cache reader across every shape PKScreener
writes, the Telegram transport, the scheduler, a full scan end to end against
a synthetic market -- **and, if `secrets/.env.dev` is filled in, two tests that
call the real Telegram API and send you a real message.** That's the live
proof the bot token and chat id actually work, not just that our code would
format a message correctly. No `secrets/.env.dev` yet? Those two tests skip
(not fail) -- everything else still runs, no Docker or market data needed.

```
115 passed, 2 skipped   # no secrets/.env.dev yet
117 passed              # secrets/.env.dev filled in -- check your Telegram
```

Run just the live check:

```bash
pytest tests/test_live_telegram.py -v -rs
```

### What isn't covered by the fast suite

- **The actual Docker build.** `docker compose build alerts` pulls upstream's
  current image and layers ours on top -- a hermetic pytest run can't catch a
  break there. Covered separately, see CI below.
- **PKScreener's own CLI running non-interactively.** Its first-run OTP login
  gate (see the `RUNNER` comment in `docker-compose.yml`) is upstream behavior
  triggered by *how their image is invoked*, not something a Python unit test
  meaningfully exercises. `make data` / `make scan` running to completion
  without hanging on a prompt **is** that check, in practice, every time you
  run them.
- **Parsing a real (not synthetic) PKScreener cache file.** `tests/test_data.py`
  covers every shape their source code is known to write (`DataFrame`,
  `df.to_dict("split")`, a column-keyed dict), but that's read from their
  source, not proven against a live download from inside every environment.
  `make data && make dry-run` is the live version of this check.

## Continuous integration

`.github/workflows/tests.yml` runs on every push and pull request, with zero
GitHub-side configuration needed -- `secrets/.env.dev` is committed to the
repo, so every checkout (including CI's) already has real credentials:

- **`unit-tests`** -- installs `requirements-test.txt`, runs `make test`
  (including the two live-Telegram tests -- a real message every push/PR).
- **`docker-build`** -- runs `docker compose build alerts`, smoke-tests the
  built image with `--list-strategies`, then mounts the committed
  `secrets/.env.dev` into the real container and runs `--check-telegram` --
  a second real message, this time proving the actual Docker image works,
  not just the Python code.

If you ever move credentials back out of git (see "Rotating or protecting
the committed token" above), CI stops getting real values automatically --
add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` under repo **Settings →
Secrets and variables → Actions** to restore the live checks; both jobs
still pass without them either way, the live-message parts just skip.
