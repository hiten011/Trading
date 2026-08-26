# F&O open-interest blast scanner

Scans every option contract on every NSE F&O underlying (~215 of them, ~35,000
contracts a session), flags the ones whose open interest jumped past a
threshold against the previous session, and sends a Telegram alert.

This is the Python replacement for the TradingView Pine indicator. Pine caps
`request.security()` at ~64 calls and each option leg costs one, which is why
that version could only watch ~32 stocks. This one has no such limit.

```bash
make oi-build                      # build (reuses the alerts image)
make oi-dry-run                    # scan the latest session, print the alert
make oi-alert                      # ...and actually send it
make oi-up                         # run on a schedule, unattended
make oi-logs
make oi-backtest START=2024-07-01 END=2026-08-25
```

---

## Read this before you trade on it

**The signal was backtested over 533 sessions (Jul 2024 - Aug 2026, the full
F&O universe) and shows no persistent predictive edge.** Details and numbers
in [Backtest results](#backtest-results) below. In short:

- Split into a 16-month in-sample and a 10-month out-of-sample window, the
  signal was **strongly negative in-sample and strongly positive
  out-of-sample** — at similar magnitude, and for *every* configuration
  tested including the raw Pine logic. A result that flips sign between two
  adjacent periods is a property of the period, not of the signal.
- The cleanest test is the long/short spread: the average 5-day return of
  names tagged bullish minus those tagged bearish. That is immune to market
  drift. It came out at **-0.07%** for the shipped configuration and
  **-0.29%** for the unfiltered Pine logic, positive in 5 of 9 and 3 of 9
  quarters respectively. The direction call is a coin flip.

So: this is a **screener**, not a strategy. It is genuinely good at finding
where unusual option positioning happened today, across a universe far too
large to watch by hand. Treat an alert as "go look at this name", not as a
trade. The buildup and bias labels describe *what happened*; the backtest says
they do not reliably predict what happens next.

Nothing here places orders. It only sends messages.

---

## Where the data comes from

NSE has no free official real-time API, and the option-chain endpoint on their
website is actively throttled — from this environment and from most
cloud/datacentre hosts it returns **HTTP 200 with an empty body** (`{}`), with
a warmed cookie jar, browser `User-Agent`, correct `Referer` and
`Accept-Language`. Other NSE JSON endpoints on the same host and jar answer
normally at the same moment, so it is that endpoint specifically, not a
network or session problem.

The scanner therefore runs on **NSE's official end-of-day F&O bhavcopy
archive** (UDiFF format):

```
https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip
```

| | |
|---|---|
| Cost | Free, no account, no key |
| Coverage | Every contract on every F&O underlying — 214 underlyings, ~35,500 option contracts per session |
| History | Back to July 2024 in this format; ~530 sessions available today |
| Resolution | One trading session |
| Reliability | A static archive file. No rate limiting, no blocking, no login |
| Published | After the close, roughly 18:00–19:00 IST |

Each row carries open interest, the day's change in open interest, traded
volume, the option's own open/close/previous close, the underlying's price and
the lot size — everything the scanner needs, with no second lookup for spot.

**One session is the finest resolution this gives you.** That happens to match
what the Pine original computed on a daily chart (`open_interest` vs
`open_interest[1]`), so nothing is lost against the thing being replaced. For
genuine *intraday* OI you need a broker feed — Kite Connect, Upstox, Angel
One or Dhan — all of which need an account and most of which need a paid API
tier. `custom/oi/sources/nselive.py` implements the website API behind the same
interface and will work from an IP NSE does not throttle (a home or office ISP
in India, typically); a broker adapter drops in beside it without the scanner
changing, since both produce the same `SessionData` shape.

### Two format facts that were verified, not assumed

Both of these change what a sensible threshold looks like, and getting either
wrong silently corrupts every filter:

1. **`OpnIntrst - ChngInOpnIntrst` reproduces the previous session's
   `OpnIntrst` exactly** — checked across the 35,325 contracts common to two
   consecutive files, 100% exact, zero mismatches. So one file is enough to
   compute a day-over-day OI change; no join needed.
2. **Open interest is quoted in units (shares); traded volume is quoted in
   lots.** Confirmed by reconciling `TtlTrfVal`: turnover ÷ (volume × lot size)
   recovers the underlying price, turnover ÷ volume does not. This asymmetry
   matters a lot — IDEA's lot is 71,475 and RELIANCE's is 500, so a raw
   "OI > 10,000 contracts" floor of the kind that seems natural would have
   *excluded* IDEA's most liquid strike while admitting far smaller real
   positions elsewhere. Every floor in this scanner is expressed in **lots**.

---

## What the scanner does

The core number is the Pine original's, unchanged:

```
oi_change_pct = (oi - oi[1]) / oi[1] * 100
```

Everything else exists to stop that number lying to you. A bare 1000% screen
over ~35,000 contracts fires **137 times a session** — mostly deep-OTM strikes
that went from 40 lots to 500 on a handful of trades. The shipped
configuration fires about **7 times a session**.

Gates run cheapest-and-most-selective first:

| Gate | Default | What it removes |
|---|---|---|
| Universe | all | Symbol allow-list, optional index exclusion |
| Expiry | nearest 2, DTE 2–45 | Expiry-week mechanics; far series whose thin base OI manufactures huge percentages |
| Rollover guard | on, 5 sessions | The next month filling up before a monthly expiry — position transfer, not conviction |
| Moneyness | within 10% of spot | Deep OTM lottery tickets |
| Liquidity | OI ≥ 500 lots, prev OI ≥ 50 lots, added ≥ 250 lots, volume ≥ 100 lots, ≥ ₹1 Cr traded | "10 contracts became 110" and anything that traded twice |
| Threshold | 300 / 1000 / 2000% | The tiers themselves |
| Significance | ≥ 0.5% of the symbol's OI book | Big percentages that are trivial against everything outstanding in that name |
| Z-score | off | Contracts whose OI routinely swings like this anyway |
| Confirmation | off | Option read that the futures book contradicts |

### Improvements over a plain threshold

Beyond the filters, several things were fixed or added because the naive
version is actively misleading:

- **New contracts are handled explicitly.** The Pine original divided by zero
  when a contract had no prior OI and its guard returned `0.0` — silently
  discarding exactly the contracts that went from nothing to a real position.
  Here the percentage is *undefined* rather than infinite, the contract is
  graded on the absolute size of what appeared, and the alert says `OI NEW`.
- **The price reference is basis-aware.** A listed-but-dormant strike carries
  a stale theoretical previous close. GODREJCP's 930 call sat at a
  carried-forward 103.25 with zero OI; when the underlying gapped 11% lower it
  printed 14.20. Read against that stale number it looks like an −86% collapse
  and gets classified as heavy call writing — a signal invented out of a
  bookkeeping artifact. The previous close is trusted only when there was open
  interest to justify it; otherwise the day's own open is used, which is the
  better question anyway ("as this position was built today, did the price
  rise or fall?"). The alert says which basis it used.
- **Bias folds in CE/PE.** The four-box buildup table is written for futures.
  On an option, the same "short buildup" means opposite things for a call and
  a put — writing calls is bearish, writing puts is bullish. `Buildup`
  describes activity in the contract; `Bias` translates it into a view on the
  underlying.
- **Futures OI is carried as context.** The four-box table *is* valid on
  futures, so each alert shows the symbol's futures buildup alongside. It can
  optionally be required to agree (`OI_REQUIRE_FUTURES_CONFIRM=1`).
- **Significance is measured against the symbol's own book**, not just against
  the contract's prior OI, so a large percentage on a small base ranks below a
  smaller percentage that moved a real share of everything outstanding.
- **Notional floors alongside lot floors**, so a ₹100 stock and a ₹40,000
  stock are held to a comparable bar.
- **Per-symbol cap**, so one stock mid-rollover cannot fill the whole message.

### De-duplication

A contract alerts once per crossing. It will not alert again for
`OI_COOLDOWN_HOURS` — unless it **escalates into a higher tier**, which is new
information and bypasses the cooldown. State lives in SQLite
(`data/oi_state.sqlite`) so a container restart does not replay everything
already sent. `--dry-run` uses an in-memory store, so rehearsing a scan never
silences the real alert that follows.

---

## Backtest results

533 sessions, 1 Jul 2024 – 25 Aug 2026, all 214 underlyings.

**Methodology.** NSE publishes the bhavcopy after the close, so a signal from
session D cannot be traded during session D. Entry is taken at the **close of
D+1** — deliberately conservative; entering at D+1's open would usually do
better. Measuring from D's close would be look-ahead bias and would flatter
the results badly, because the OI move and the price move that caused it
happen on the same day. Returns are **signed** by the alert's own bias (+1
bullish, −1 bearish), and benchmarked against every symbol on the same days
with the same bullish/bearish mix applied, so market drift cannot masquerade
as edge.

Signed 5-day return, in-sample (Jul 2024 – Oct 2025) vs out-of-sample
(Nov 2025 – Aug 2026):

| Configuration | Alerts/session | IS edge | IS t | OOS edge | OOS t |
|---|---|---|---|---|---|
| `pine_raw` (no filters) | 137.6 | −0.22% | −10.91 | +0.13% | +4.63 |
| OI floor only | 12.7 | −0.27% | −5.41 | +0.48% | +4.96 |
| shipped defaults | 7.0 | −0.43% | −4.04 | +0.63% | +4.85 |
| strict | 2.8 | −0.53% | −3.07 | +0.66% | +3.42 |
| extreme tier only | 1.1 | −0.31% | −1.83 | +1.42% | +4.96 |
| futures-confirmed | 5.4 | −0.81% | −6.93 | +0.61% | +4.69 |

Every row is negative in-sample and positive out-of-sample. That is not
overfitting (which gives the opposite pattern) — it is a regime flip that
affects every configuration equally, including the unfiltered baseline. Had
this been built in October 2025 on the first 16 months, the conclusion would
have been that it is a reliable *contrarian* signal.

Quarter by quarter, the shipped configuration's signed 5-day return:

| | 24Q3 | 24Q4 | 25Q1 | 25Q2 | 25Q3 | 25Q4 | 26Q1 | 26Q2 | 26Q3 |
|---|---|---|---|---|---|---|---|---|---|
| mean | −0.27 | −0.64 | −1.15 | +0.19 | −0.33 | +0.17 | +0.94 | +0.75 | +0.36 |

Five of nine quarters positive.

**The decisive test** is the long/short spread — the average raw 5-day return
of bullish-tagged names minus bearish-tagged names. Market drift cancels out
of it entirely, so it isolates whether the direction classifier works:

| Configuration | Mean quarterly spread | Positive quarters |
|---|---|---|
| shipped defaults | **−0.07%** | 5 / 9 |
| `pine_raw` | **−0.29%** | 3 / 9 |

Roughly zero for the filtered version and negative for the raw Pine logic. The
direction call carries no information.

**What the filters *do* buy**, and it is not nothing: alerts drop from 137.6
to 7.0 per session — from unusable to readable — while the direction statistic
gets no worse and modestly better. So the filters are worth keeping for
attention-management even though they do not create an edge.

Reproduce it with:

```bash
make oi-warm-cache START=2024-07-01 END=2026-08-25    # ~530 files, ~640 MB
make oi-backtest START=2024-07-01 END=2026-08-25 CSV=data/oi_backtest.csv
```

### If you want to keep investigating

The honest next steps, roughly in order of expected value:

1. **Test a shorter horizon.** Everything here is measured from D+1's close.
   Most of the reaction to an OI event may land in D+1's *open*, which this
   deliberately gives up. Bhavcopy cannot answer that — it needs intraday data.
2. **Condition on the underlying's move**, not just OI. The alerted names in
   several quarters had large raw moves in both directions; OI may be a
   volatility marker rather than a direction marker.
3. **Test it as a volatility signal instead** — does an OI blast predict the
   underlying's realised range over the next few days, regardless of sign?
   Nothing measured here rules that out, and it is the more plausible
   hypothesis given the results.
4. **Drop the direction claim** and use it purely as an attention filter,
   which is what the evidence currently supports.

---

## Configuration

Every knob is an `OI_*` environment variable, documented inline in `.env`.
The ones most worth touching:

| Variable | Default | Meaning |
|---|---|---|
| `OI_MIN_TIER` | `STRONG` | `WATCH` (300%), `STRONG` (1000%), `EXTREME` (2000%) |
| `OI_STRONG_PCT` | `1000` | The brief's original threshold |
| `OI_MIN_OI_LOTS` | `500` | Current OI floor, in lots |
| `OI_MIN_PREV_OI_LOTS` | `50` | Prior OI floor — kills "10 became 110" |
| `OI_MAX_MONEYNESS_PCT` | `10` | Strike must be within this % of spot |
| `OI_MIN_DTE` / `OI_MAX_DTE` | `2` / `45` | Days-to-expiry band |
| `OI_SUPPRESS_ROLLOVER` | `1` | Drop the far month during monthly rollover |
| `OI_COOLDOWN_HOURS` | `12` | Per-contract re-alert cooldown |
| `OI_MAX_ALERTS` / `OI_MAX_PER_SYMBOL` | `25` / `3` | Message size caps |
| `OI_RUN_AT` | `18:30` | IST. Earlier than ~18:00 scans the previous session |

### Why it scans once a day, not every minute

The natural expectation is that a scanner should poll constantly. Here it
should not, for a reason that is about the data rather than the code:

**The bhavcopy is published once per session.** It is a single archive file
NSE writes after the close. Polling it every minute re-downloads a byte-identical
file 375 times and produces exactly one scan's worth of information. There is
no intraday version of this file to poll.

So the once-a-day schedule is not a limitation of the scanner — it is the true
update rate of the only free, complete, unblocked source of NSE F&O open
interest. Scanning more often cannot manufacture data that was never published.

**What genuinely intraday OI would require.** Two things, and the first is the
blocker:

1. **A feed that publishes OI during the session.** NSE's own option-chain API
   does, but it is throttled to an empty response for non-residential IPs (see
   [Where the data comes from](#where-the-data-comes-from)). The practical
   route is a broker API — Kite Connect, Upstox, Angel One or Dhan — which
   needs a trading account and usually a paid API tier. `custom/oi/sources/`
   already defines the interface; a broker adapter drops in beside
   `bhavcopy.py` and the scanner does not change.
2. **Different thresholds.** This matters more than it sounds. A 1000%
   day-over-day OI jump is already rare — about 7 contracts a session out of
   ~35,000. Against a *5-minute* baseline the same 1000% is a much smaller
   real event, and most of what clears it will be contracts coming off a tiny
   base early in the session. The percentage bands, the OI floors and the
   cooldown would all need re-tuning against intraday data, and re-backtesting,
   before intraday alerts meant anything. Reusing today's numbers on a 5-minute
   bar would mostly generate noise.

Also worth knowing: open interest is not a tick-by-tick quantity even on a
live feed. It is a position count that exchanges disseminate on an interval,
so even with a broker feed the useful scan cadence is minutes, not seconds.

`OI_INTERVAL_MINUTES` exists for exactly this future and is off by default.
Turning it on against the bhavcopy source today just re-reads the same file —
the de-duplication would suppress every repeat, so you would get no extra
alerts, only extra requests to NSE.

If you have a broker account, say which one and this can be wired up; the
adapter is a contained piece of work, but the re-tuning and re-backtesting
above is the part that decides whether it is worth anything.

---

## Deployment

### Measured resource use

Taken from a real full-universe scan on this codebase, not estimated:

| | |
|---|---|
| Peak memory, full scan | **156 MB** (33,906 contracts, 214 underlyings) |
| Wall time per scan | **~1.3 s** from cache, ~5 s including the download |
| Bhavcopy per session | 1.1 MB |
| Full 2-year history | 607 MB (only needed for backtesting) |
| Docker image | ~6.5 GB on disk |

The image is large because it inherits `pkjmesra/pkscreener` for the equity
screener. The OI scanner itself needs only `pandas` and `requests` — a
purpose-built image would be roughly 200 MB and would make both a `t4g.nano`
and AWS Lambda comfortable. Worth doing if this ends up being the only thing
you deploy.

### Which AWS service

The scan runs **once a day** and takes **seconds**. That is a scheduled job,
not a service.

| Option | Verdict |
|---|---|
| **EC2 `t4g.small`** | Simplest by a distance. `docker compose up -d oi-scanner` and walk away. The container schedules itself, the cache is a local volume. **Recommended.** |
| **ECS Fargate scheduled task** | The managed equivalent. Runs the existing image unchanged with EventBridge firing it daily; needs EFS (or S3) for `data/` so the cache and de-dup state survive between runs. Worth it if you already run ECS. |
| **Lambda + EventBridge** | Cheapest in principle, but not with a 6.5 GB image — past Lambda's 10 GB limit once layers are counted, and `data/` would have to move to EFS. Viable only after building the slim image mentioned above. |

Upstream's base image is multi-arch (`linux/amd64` and `linux/arm64`), so
Graviton instances work and are the cheaper choice.

### Step by step on EC2

**1. Launch the instance**

- AMI: Amazon Linux 2023 (arm64)
- Type: `t4g.small` (2 GB). `t4g.micro` (1 GB) is fine too — the scan peaks at
  156 MB, and the rest is Docker's own overhead. `t4g.nano` (512 MB) will run
  the scan but leaves no room for the daemon plus a backtest.
- Storage: **20 GB gp3**. The default 8 GB will not hold a 6.5 GB image.
- Security group: outbound HTTPS only. **No inbound ports are needed** — the
  scanner makes outbound calls to NSE and Telegram and listens for nothing.

**2. Install Docker and enable it at boot**

```bash
sudo dnf update -y
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
newgrp docker            # or log out and back in
```

`systemctl enable` is the part that matters: with `restart: unless-stopped`
already set in the compose file, the scanner comes back on its own after a
reboot.

**3. Get the code and build**

```bash
git clone https://github.com/hiten011/Trading.git
cd Trading
docker compose build oi-scanner      # ~5-10 min on a t4g.small
```

**4. Confirm Telegram works before trusting the schedule**

```bash
docker compose run --rm oi-scanner --check-telegram
```

That sends a real message. If it does not arrive, open the bot in Telegram and
send `/start` — Telegram will not let a bot message you first.

**5. Rehearse a scan**

```bash
docker compose run --rm oi-scanner --once --dry-run
```

Prints what it would send, touches no Telegram and writes no de-dup state, so
it cannot silence the real alert that follows.

**6. Start it**

```bash
docker compose up -d oi-scanner
docker compose logs -f oi-scanner
```

You should see `Scheduled for 18:30 on trading days (Asia/Kolkata)` and then
`Next scan at ... (in N min)`.

**7. Optional — warm the backtest cache**

Only if you intend to run backtests on the box. It is 607 MB and the live
scanner does not need it.

```bash
docker compose run --rm oi-scanner --warm-cache --start 2024-07-01
```

### Timezone

The container is set to `Asia/Kolkata` regardless of the host's clock, so
`OI_RUN_AT=18:30` means 18:30 IST even on a UTC instance. NSE publishes the
F&O bhavcopy around 18:00-19:00 IST; a run before that scans the previous
session, which is harmless but a session behind.

### Keeping it healthy

```bash
docker compose logs --tail 100 oi-scanner    # what it has been doing
docker compose restart oi-scanner            # after an .env change
docker compose pull && docker compose build oi-scanner && docker compose up -d oi-scanner
```

State that survives restarts, all under the mounted `./data`:

- `data/oi_cache/` — downloaded bhavcopy files, so a restart re-downloads nothing
- `data/oi_state.sqlite` — de-dup state, so a restart replays no alerts
- `data/oi_reports/` — the CSV attached to each alert

Container logs are capped at 3 x 10 MB by the compose file, so they cannot
fill the disk.

### Behind a TLS-inspecting proxy

If your network terminates TLS with a private CA, mount the bundle and point
requests at it:

```yaml
volumes:
  - /path/to/ca-bundle.crt:/tmp/ca.crt:ro
environment:
  REQUESTS_CA_BUNDLE: /tmp/ca.crt
```

Do not disable certificate verification instead.

---

## Layout

```
custom/oi/
  models.py            value types; OI arithmetic, units, buildup/bias
  config.py            every knob, resolved from the environment
  scanner.py           the gates, scoring and ranking
  state.py             SQLite cooldown / de-duplication
  alerts.py            Telegram rendering + CSV export
  backtest.py          historical replay, forward returns, benchmark
  cli.py               python3 -m custom.oi.cli
  sources/
    bhavcopy.py        NSE end-of-day archive (default)
    nselive.py         NSE website API (intraday, throttled — see above)

tests/test_oi_*.py     131 tests covering all of the above
data/oi_cache/         downloaded bhavcopy zips (git-ignored)
data/oi_state.sqlite   de-dup state (git-ignored)
data/oi_reports/       alert CSVs (git-ignored)
```

The scanner shares this repo's existing Telegram transport
(`custom/notify.py`), secrets loading (`custom/config.py`) and Docker image —
only the entrypoint differs from the equity `alerts` service.
