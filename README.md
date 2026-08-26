# Trading — PKScreener + your own indicator → Telegram

Runs [PKScreener](https://github.com/pkjmesra/PKScreener) from its official Docker
image, and adds a small framework where **you write one Python function** and get
a Telegram alert whenever any listed Indian stock matches it.

Nothing is installed on your machine except Docker. TA-Lib, pandas, the whole
PKScreener stack — all of it lives inside the container.

---

## What you get

| Piece | What it does | Command |
|---|---|---|
| `screener` | PKScreener's own 50 built-in scanners, one-shot | `make scan` |
| `alerts` | **Your** indicator over the whole market, on a schedule → Telegram | `make up` |
| `bot` | PKScreener's interactive Telegram bot server | `make bot` |
| `data-refresh` | Downloads daily candles for every NSE stock | `make data` |
| `oi-scanner` | **F&O option open-interest blasts** across all ~215 F&O stocks → Telegram | `make oi-up` |

---

## Quick start

Telegram credentials are already committed in this repo (`secrets/.env.dev`) —
see **Credentials** below for why. Clone and go:

```bash
git clone <this repo> && cd Trading
make build                # pulls pkjmesra/pkscreener, builds the alerts image
make check-telegram       # sends you a test message -- should just work
make data                 # downloads candles for every NSE stock (slow, once a day)
make dry-run               # runs your indicator, prints the alert instead of sending
make up                   # runs it for real, on the schedule in .env
make logs                 # watch it work
```

`make` on its own lists every command.

---

## Credentials

`secrets/.env.dev` in this repo holds a **real** Telegram bot token and chat
id, committed on purpose so cloning this repo is genuinely zero-setup — no
file to create, no values to paste in. That's only reasonable because this
repo is public and the owner explicitly chose that trade-off: anyone who can
see this repo can see (and use) that bot token.

If that's ever not what you want:

- **Rotate the token** — message [@BotFather](https://t.me/BotFather) →
  `/revoke` (or `/token`) on the bot, which invalidates the old one instantly,
  then put the new one in `secrets/.env.dev`.
- **Make it private again** — GitHub Settings → General → Danger Zone. The
  committed credentials stop being publicly visible; no code changes needed.
- **Go back to a git-ignored secrets file** — add `secrets/.env.dev` back to
  `.gitignore`, keep the real values local-only, and (for CI) add
  `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` under repo Settings → Secrets and
  variables → Actions instead.

Setting up your OWN bot from scratch, if you ever need to: message
[@BotFather](https://t.me/BotFather) → `/newbot` for a token, message
[@userinfobot](https://t.me/userinfobot) for your chat id, then **send your
bot any message first** (`/start` works) — Telegram won't let a bot speak
first, so alerts go nowhere silently without this step. Put both values in
`secrets/.env.dev` (`TOKEN` and `chat_idADMIN`) and verify with
`make check-telegram`.

A group or channel works too instead of a DM — add the bot as admin, use the
channel id (`-100…`) as `chat_idADMIN`. Both the signed and unsigned forms work.

### Your indicator rules

The shipped example (`custom/strategies/my_indicator.py`) is a placeholder —
a volume-backed breakout screen. Tell me the conditions you actually want in
plain English ("RSI under 30 and price above the 200 EMA and volume twice its
average") and I'll write it, or edit it yourself — see below.

Intraday alerts aren't wired up: everything here runs on daily candles.
Intraday needs a live data feed, which needs broker credentials PKScreener
doesn't provide for free.

---

## Writing your own indicator

Edit **`custom/strategies/my_indicator.py`**. One function:

```python
def evaluate(symbol: str, df: pd.DataFrame) -> Signal | None:
    ...
```

It is called once per stock. `df` has columns `Open, High, Low, Close, Volume`,
oldest row first, ~250 trading days of history. Return a `Signal` to alert, or
`None` to stay quiet.

```python
from custom.strategies.base import Signal, ema, rsi, last

def evaluate(symbol, df):
    close = df["Close"]
    if last(rsi(close, 14)) < 30 and last(close) > last(ema(close, 200)):
        return Signal(symbol, "BUY", last(close), "Oversold in an uptrend")
    return None
```

Ready-made helpers in `custom/strategies/base.py`: `sma`, `ema`, `rsi`, `atr`,
`macd`, `bollinger`, `rolling_vwap`, `pct_change`, `crossed_above`,
`crossed_below`, `last`. TA-Lib is also installed if you prefer it
(`import talib`).

Test a change without spamming yourself:

```bash
make dry-run                       # whole market, printed to the terminal
make dry-run SYMBOLS=RELIANCE,TCS  # just these two
```

Keeping several strategies side by side: add more files to
`custom/strategies/`, then switch with `PKS_STRATEGY=<filename>` in `.env`.
`docker compose run --rm --no-deps alerts --list-strategies` shows what is there.

**One strategy runs per container.** `PKS_STRATEGY` names a single file —
adding more files does not make one scan evaluate all of them. To run several
at once, start one `alerts` container per strategy (each with its own
`PKS_STRATEGY`), or ask and multi-strategy support can be added to the runner.

**New strategy files are tested automatically.** `tests/test_strategy_contract.py`
discovers everything in `custom/strategies/` at collection time and checks each
one against the same contract: it loads, it survives awkward market data (flat
stocks, straight lines, one-bar history, NaNs, zero volume, penny prices), it
returns a `Signal` or `None`, its numbers are finite, it does not mutate the
caller's DataFrame, and it is deterministic. Drop in `my_new_thing.py` and CI
covers it on the next push with no edit to the test file. That matters because
`custom/runner.py` deliberately swallows per-symbol exceptions so one bad stock
cannot kill a whole scan — which means a broken indicator looks exactly like an
indicator that found nothing.

Your edits are live-mounted — no rebuild needed unless you add a pip package
(then put it in `docker/requirements-custom.txt` and `make build`).

---

## F&O open-interest blast scanner

A second, independent scanner: it watches **option open interest** rather than
price, across every NSE F&O underlying (~215 names, ~35,000 contracts a
session), and alerts when a contract's OI jumps past a threshold against the
previous session.

This replaces the TradingView Pine version, which was capped at ~32 stocks by
Pine's 64-`request.security()` limit.

```bash
make oi-build                  # build (same image as `alerts`)
make oi-dry-run                # scan the latest session, print the alert
make oi-alert                  # ...and actually send it
make oi-up                     # run unattended on a schedule
make oi-logs
```

Alerts look like:

```
🔥 GODREJCP 930 CALL SHORT BUILDUP OI NEW 🔴 BEARISH
   OI 0 → 3,494 lots (new position) · 6.0% of book
   Px -43.2% (intraday) · Vol 12,836 lots · ₹584.0 Cr
   Spot 910.01 (-11.2%) · strike 2.2% above
   25Aug26 · 13d · futures: Short Buildup · PCR 0.49
```

**Data source:** NSE's official end-of-day F&O bhavcopy archive — free, no
account, complete coverage, history back to July 2024. NSE's live option-chain
API is throttled to an empty response for non-residential IPs, so intraday OI
needs a broker feed (Kite/Upstox/Angel/Dhan); an adapter slot is already in
place for one.

**Before you trade on it:** the signal was backtested over 533 sessions and
**shows no persistent predictive edge** — it was strongly negative in-sample
and strongly positive out-of-sample at similar magnitude, and its long/short
spread (which is immune to market drift) averages roughly zero. It is a good
*screener* for finding unusual option positioning across a universe too big to
watch by hand; it is not a validated strategy. Full numbers, methodology and
the reasoning behind every filter are in
[docs/OI_SCANNER.md](docs/OI_SCANNER.md).

Configuration is the `OI_*` block in `.env`. Backtest it yourself with
`make oi-backtest START=2024-07-01 END=2026-08-25`.

---

## Running PKScreener's own scanners

```bash
make scan                  # uses SCAN_OPTIONS from .env
make scan OPTIONS=X:12:9   # Scanners → Nifty (All Stocks) → Volume gainers
```

The menu path is `Menu:Index:Scanner`:

| Index | | Scanner | |
|---|---|---|---|
| `1` | Nifty 50 | `1` | Probable breakouts |
| `5` | Nifty 500 | `5` | RSI screening |
| `12` | **Nifty (All Stocks)** | `6` | Reversal signals |
| `14` | F&O stocks only | `7` | Chart patterns |
| | | `9` | Volume gainers |
| | | `23` | Breaking out now |
| | | `31` | High momentum (RSI/MFI/CCI) |

Run `docker compose --profile manual run --rm screener` for the full interactive menu.

---

## Layout

```
docker-compose.yml          the four services
docker/Dockerfile           our image: FROM pkjmesra/pkscreener + custom code
.env                        knobs (schedule, filters, which strategy)
secrets/.env.dev            Telegram credentials — committed (see Credentials above)
config/pkscreener.ini       PKScreener's own filters (min price, volume ratio…)
config/universe.txt         your own stock list, if PKS_UNIVERSE=file

custom/
  oi/                         F&O open-interest scanner (see docs/OI_SCANNER.md)
  strategies/my_indicator.py  >>> YOUR INDICATOR GOES HERE <<<
  strategies/base.py          Signal + RSI/EMA/ATR/MACD helpers
  runner.py                   scan → evaluate → alert
  data.py                     reads PKScreener's candle cache
  universe.py                 which stocks to scan
  notify.py                   Telegram transport
  report.py                   formats the alert table

tests/                      276 tests: run with `make test`
.github/workflows/tests.yml CI: runs the tests + a Docker build check on every push/PR
data/                       downloaded candles (git-ignored)
```

---

## Testing

```bash
pip install -r requirements-test.txt
make test
```

Runs the full suite (no Docker, no market data needed for most of it). Two of
those tests are a deliberate exception: since `secrets/.env.dev` has real
credentials, `make test` also sends a real Telegram message through the real
Bot API every time, so you get live proof it's actually wired up correctly —
not just that the code would format a message right.

This also runs automatically in GitHub Actions on every push and pull
request (`.github/workflows/tests.yml`), plus a second job that rebuilds the
Docker image and sends its own live Telegram message through the actual
container — both work with zero configuration in GitHub, since the checkout
already has the same committed credentials. Full details, including exactly
what is and isn't covered, in [docs/SETUP.md](docs/SETUP.md#running-the-tests).

---

## How the data flows

`data-refresh` runs PKScreener's downloader, which writes one pickle holding
daily candles for every NSE symbol. `alerts` reads that same pickle — so your
indicator runs against the whole market in about 20 seconds and never touches a
data API itself. In `--schedule` mode (what `make up` runs), `alerts` also
refreshes that cache on its own once it's more than `PKS_DATA_MAX_AGE_HOURS`
old (20h by default) — no daily cron job needed.

If the cache is missing, the runner falls back to downloading from Yahoo, which
is much slower. The error message tells you which case you are in.

---

## Running this unattended (AWS or any always-on server)

`make up` only starts containers on the machine you run it on — for alerts to
fire while your laptop is off, `docker compose up -d alerts` needs to run
somewhere always-on: an EC2 instance, a cheap VPS, or a machine you already
leave running. Same commands as above, just run there instead:
`make build && make data && make up`. That one `docker-compose.yml` really is
the whole deployment — no separate cron or always-on data-refresh process,
since `alerts` now keeps its own cache fresh. Full walkthrough (instance
sizing, keeping Docker running across reboots) in
[docs/SETUP.md](docs/SETUP.md#running-this-on-a-server-aws-or-anywhere-else).

---

## Configuration

Everything in `.env`. The ones that matter:

| Variable | Default | Meaning |
|---|---|---|
| `PKS_STRATEGY` | `my_indicator` | Which file in `custom/strategies/` to run |
| `PKS_RUN_AT` | `15:45,09:05` | IST times to scan (empty = interval mode) |
| `PKS_INTERVAL_MINUTES` | `0` | Scan every N minutes instead |
| `PKS_UNIVERSE` | `auto` | `auto` (everything cached), `pkscreener`, or `file` |
| `PKS_MIN_PRICE` | `20` | Skip penny stocks |
| `PKS_MIN_AVG_VOLUME` | `100000` | Skip illiquid stocks |
| `PKS_MAX_ALERTS` | `40` | Cap per message; hits are ranked by `Signal.score` |
| `PKS_TRADING_DAYS_ONLY` | `1` | Skip weekends |
| `PKS_DRY_RUN` | `0` | Print instead of sending |
| `PKS_ATTACH_CSV` | `1` | Also attach the full table as a CSV |

---

## Troubleshooting

**"Telegram is not configured"** — `TOKEN` or `chat_idADMIN` is missing from
`secrets/.env.dev`. Run `make check-telegram`.

**Test message never arrives** — you have not messaged the bot yet. Open the chat
and send `/start`.

**"No usable candles"** — run `make data` first.

**No alerts ever fire** — your rules may be too strict. Try
`make dry-run SYMBOLS=RELIANCE,TCS,INFY` and loosen one condition at a time.

**A symbol goes missing from results** — your indicator raised on it. Set
`PKS_LOG_LEVEL=DEBUG` in `.env` and check `make logs`.

---

## Notes

PKScreener is MIT-licensed and maintained by [pkjmesra](https://github.com/pkjmesra/PKScreener);
this repo consumes its published image rather than vendoring its source, so
`make build` always picks up their latest release. Nothing here is financial
advice — screeners produce candidates, not decisions.
