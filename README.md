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

---

## Quick start

```bash
git clone <this repo> && cd Trading
make setup                # creates .env and secrets/.env.dev
$EDITOR secrets/.env.dev  # paste your Telegram token + chat id (see below)
make build                # pulls pkjmesra/pkscreener, builds the alerts image
make check-telegram       # sends you a test message
make data                 # downloads candles for every NSE stock (slow, once a day)
make dry-run              # runs your indicator, prints the alert instead of sending
make up                   # runs it for real, on the schedule in .env
make logs                 # watch it work
```

`make` on its own lists every command.

---

## What I need from you

Two values. That is the whole list.

### 1. A Telegram bot token

1. Open Telegram, message **[@BotFather](https://t.me/BotFather)**
2. Send `/newbot`, pick a name and a username ending in `bot`
3. It replies with a token like `8123456789:AAF-abcdefghijklmnopqrstuvwxyz`

Put it in `secrets/.env.dev` as `TOKEN`.

### 2. Your Telegram chat id

1. Message **[@userinfobot](https://t.me/userinfobot)**
2. It replies with your numeric `Id`, e.g. `5058733760`

Put it in `secrets/.env.dev` as `chat_idADMIN`.

Then **send your new bot any message** (`/start` is fine). Telegram will not let a
bot message you first — without this the alerts silently go nowhere.

```ini
# secrets/.env.dev
TOKEN='8123456789:AAF-abcdefghijklmnopqrstuvwxyz'
chat_idADMIN='5058733760'
CHAT_ID='5058733760'
```

Verify both with `make check-telegram` — it messages you and prints the bot's
username. That file is git-ignored; it never leaves your machine.

### Optional, only if you want them

- **A group or channel instead of a DM** — add the bot as an admin, then use the
  channel id (`-100…`) as `chat_idADMIN`. Both the signed and unsigned forms work.
- **Your actual indicator rules.** The shipped example is a placeholder. Tell me
  the conditions in plain English ("RSI under 30 and price above the 200 EMA and
  volume twice its average") and I will write it, or write it yourself — see below.
- **Intraday alerts.** Right now everything is daily candles. Intraday needs a
  live data feed; PKScreener supports one but it needs broker credentials.

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

Your edits are live-mounted — no rebuild needed unless you add a pip package
(then put it in `docker/requirements-custom.txt` and `make build`).

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
secrets/.env.dev            Telegram credentials — git-ignored
config/pkscreener.ini       PKScreener's own filters (min price, volume ratio…)
config/universe.txt         your own stock list, if PKS_UNIVERSE=file

custom/
  strategies/my_indicator.py  >>> YOUR INDICATOR GOES HERE <<<
  strategies/base.py          Signal + RSI/EMA/ATR/MACD helpers
  runner.py                   scan → evaluate → alert
  data.py                     reads PKScreener's candle cache
  universe.py                 which stocks to scan
  notify.py                   Telegram transport
  report.py                   formats the alert table

tests/                      115 tests: run with `make test`
data/                       downloaded candles (git-ignored)
```

---

## How the data flows

`data-refresh` runs PKScreener's downloader, which writes one pickle holding
daily candles for every NSE symbol. `alerts` reads that same pickle — so your
indicator runs against the whole market in about 20 seconds and never touches a
data API itself. Re-run `make data` once a day (or let the compose dependency do
it when `alerts` starts).

If the cache is missing, the runner falls back to downloading from Yahoo, which
is much slower. The error message tells you which case you are in.

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
