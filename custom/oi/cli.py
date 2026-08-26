"""Entry point for the F&O open-interest blast scanner.

    python3 -m custom.oi.cli --once                 scan the latest session
    python3 -m custom.oi.cli --once --dry-run       ...printed, not sent
    python3 -m custom.oi.cli --once --date 2026-08-12   scan one past session
    python3 -m custom.oi.cli --schedule             run on OI_RUN_AT forever
    python3 -m custom.oi.cli --backtest --start 2025-01-01 --end 2025-12-31
    python3 -m custom.oi.cli --check-telegram       verify the bot credentials
    python3 -m custom.oi.cli --warm-cache --start 2024-07-01
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence

from custom.config import configure_logging
from custom.notify import TelegramError, TelegramNotifier
from custom.oi import alerts as alerting
from custom.oi.backtest import DEFAULT_HORIZONS, format_breakdown, run_backtest
from custom.oi.config import OISettings
from custom.oi.models import Tier
from custom.oi.scanner import ScanResult, build_pct_history, scan_session
from custom.oi.sources import get_source
from custom.oi.sources.bhavcopy import BhavcopyUnavailable
from custom.oi.state import AlertState

LOGGER = logging.getLogger("custom.oi.cli")


def _timezone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - fall back to naive local time
        LOGGER.warning("Unknown timezone %r; using system local time", name)
        return None


def now_in(timezone_name: str) -> datetime:
    tzinfo = _timezone(timezone_name)
    return datetime.now(tzinfo) if tzinfo else datetime.now()


def parse_day(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"--date/--start/--end want YYYY-MM-DD, got {value!r}") from exc


# ---------------------------------------------------------------------------
# One scan
# ---------------------------------------------------------------------------

def run_once(
    settings: OISettings,
    notifier: TelegramNotifier,
    state: AlertState,
    as_of: Optional[date] = None,
) -> ScanResult:
    """Scan the most recent published session (or ``as_of``) and alert."""
    source = get_source(settings.source, cache_dir=settings.cache_dir)

    # Only meaningful for a live source: NseLiveSource pays one HTTP round
    # trip per symbol, so a --symbols subset that never reaches the source
    # layer means fetching the whole ~210-name universe to alert on five of
    # them. BhavcopySource ignores this -- one file already holds everything,
    # so pre-filtering the fetch would save nothing there.
    symbols = settings.symbols or None
    session = (
        source.load(as_of, symbols=symbols) if as_of else source.latest(symbols=symbols)
    )
    if session is None:
        raise BhavcopyUnavailable(f"No F&O session published for {as_of}")

    previous = source.previous_session(session.trade_date)

    history = None
    if settings.min_z_score > 0:
        window = []
        cursor = session.trade_date
        for _ in range(settings.z_lookback):
            earlier = source.previous_session(cursor)
            if earlier is None:
                break
            window.append(earlier)
            cursor = earlier.trade_date
        history = build_pct_history(window)

    result = scan_session(session, settings, prev_session=previous, history=history)

    fresh = state.filter_new(result.alerts, settings.cooldown_hours)
    suppressed = len(result.alerts) - len(fresh)

    sent = alerting.deliver(
        fresh, result, settings, notifier,
        as_of=now_in(settings.timezone), suppressed=suppressed,
    )
    if sent and not settings.dry_run:
        state.record_all(fresh)
    result.alerts = fresh
    return result


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def parse_run_times(values: Sequence[str]) -> List[tuple]:
    times = []
    for value in values:
        try:
            hour_text, minute_text = value.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            LOGGER.warning("Ignoring malformed OI_RUN_AT entry %r (want HH:MM)", value)
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            times.append((hour, minute))
        else:
            LOGGER.warning("Ignoring out-of-range OI_RUN_AT entry %r", value)
    return sorted(set(times))


def next_run_at(now: datetime, run_times: Sequence[tuple], trading_days_only: bool) -> datetime:
    candidates = []
    for day_offset in range(0, 8):
        day = now + timedelta(days=day_offset)
        if trading_days_only and day.weekday() >= 5:
            continue
        for hour, minute in run_times:
            moment = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if moment > now:
                candidates.append(moment)
    return min(candidates)


def _guarded(settings: OISettings, notifier: TelegramNotifier, state: AlertState) -> None:
    """One scan that never takes the scheduler down with it."""
    try:
        run_once(settings, notifier, state)
    except BhavcopyUnavailable as exc:
        LOGGER.error("No data: %s", exc)
    except TelegramError as exc:
        LOGGER.error("Telegram error: %s", exc)
    except Exception as exc:  # noqa: BLE001 - keep the loop alive
        LOGGER.exception("Scan failed: %s", exc)


def run_scheduled(
    settings: OISettings, notifier: TelegramNotifier, state: AlertState
) -> int:
    if settings.interval_minutes > 0:
        LOGGER.info("Scanning every %d minute(s)", settings.interval_minutes)
        while True:
            _guarded(settings, notifier, state)
            state.purge_older_than(90)
            time.sleep(settings.interval_minutes * 60)

    run_times = parse_run_times(settings.run_at)
    if not run_times:
        LOGGER.error("Nothing scheduled: set OI_RUN_AT (e.g. 18:30) or OI_INTERVAL_MINUTES")
        return 2

    LOGGER.info(
        "Scheduled for %s %s (%s). NSE publishes the F&O bhavcopy after ~18:00 IST, "
        "so a run before that scans the previous session.",
        ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in run_times),
        "on trading days" if settings.trading_days_only else "every day",
        settings.timezone,
    )
    while True:
        now = now_in(settings.timezone)
        upcoming = next_run_at(now, run_times, settings.trading_days_only)
        seconds = max((upcoming - now).total_seconds(), 1)
        LOGGER.info(
            "Next scan at %s (in %.0f min)", upcoming.strftime("%Y-%m-%d %H:%M"), seconds / 60
        )
        time.sleep(seconds)
        _guarded(settings, notifier, state)
        state.purge_older_than(90)
        time.sleep(60)  # never re-fire inside the same minute


# ---------------------------------------------------------------------------
# Backtest / cache warming
# ---------------------------------------------------------------------------

def run_backtest_cli(settings: OISettings, args) -> int:
    start = parse_day(args.start) or (date.today() - timedelta(days=365))
    end = parse_day(args.end) or date.today()
    source = get_source(settings.source, cache_dir=settings.cache_dir)

    if args.split:
        midpoint = start + (end - start) * 2 // 3
        windows = [("in-sample", start, midpoint), ("out-of-sample", midpoint + timedelta(days=1), end)]
    else:
        windows = [("backtest", start, end)]

    for label, window_start, window_end in windows:
        result = run_backtest(
            source, settings, window_start, window_end, label=label,
            horizons=DEFAULT_HORIZONS,
        )
        print(result.describe())
        for attribute in ("tier", "bias", "buildup", "is_new_contract"):
            print(format_breakdown(result.outcomes, attribute, horizon=5))
        print()
        if args.csv:
            import pandas as pd

            path = args.csv.replace(".csv", f"_{label}.csv")
            pd.DataFrame([o.as_row() for o in result.outcomes]).to_csv(path, index=False)
            print(f"wrote {path}")
    return 0


def warm_cache(settings: OISettings, args) -> int:
    """Pre-download bhavcopy files so a later backtest runs offline."""
    start = parse_day(args.start) or (date.today() - timedelta(days=365))
    end = parse_day(args.end) or date.today()
    source = get_source(settings.source, cache_dir=settings.cache_dir)
    downloaded = 0
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            try:
                if source.fetch_raw(cursor) is not None:
                    downloaded += 1
            except BhavcopyUnavailable as exc:
                LOGGER.warning("%s: %s", cursor, exc)
        cursor += timedelta(days=1)
    print(f"Cached {downloaded} session(s) in {settings.cache_dir}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="custom.oi.cli",
        description="Scan the NSE F&O option chain for open-interest blasts.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Scan one session and exit")
    mode.add_argument("--schedule", action="store_true", help="Run on the OI_RUN_AT schedule")
    mode.add_argument("--backtest", action="store_true", help="Replay history and score the signal")
    mode.add_argument("--warm-cache", action="store_true", help="Pre-download bhavcopy files")
    mode.add_argument("--check-telegram", action="store_true", help="Verify the Telegram credentials")

    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them")
    parser.add_argument("--date", help="Scan this session (YYYY-MM-DD) instead of the latest")
    parser.add_argument("--start", help="Backtest/cache window start (YYYY-MM-DD)")
    parser.add_argument("--end", help="Backtest/cache window end (YYYY-MM-DD)")
    parser.add_argument("--split", action="store_true",
                        help="Split the backtest window into in-sample and out-of-sample thirds")
    parser.add_argument("--csv", help="Write backtest outcomes to this CSV path")
    parser.add_argument("--symbols", help="Comma-separated symbols to restrict the scan to")
    parser.add_argument("--min-tier", choices=[t.value for t in Tier if t is not Tier.NONE],
                        help="Override OI_MIN_TIER for this run")
    parser.add_argument("--log-level", help="DEBUG/INFO/WARNING/ERROR")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = OISettings.from_env()

    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.symbols:
        overrides["symbols"] = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.min_tier:
        overrides["min_tier"] = Tier(args.min_tier)
    if args.log_level:
        overrides["log_level"] = args.log_level.upper()
    if overrides:
        settings = replace(settings, **overrides)

    configure_logging(settings.log_level)
    LOGGER.info("Settings: %s", settings.describe())

    if args.warm_cache:
        return warm_cache(settings, args)
    if args.backtest:
        return run_backtest_cli(settings, args)

    notifier = TelegramNotifier(
        token=settings.telegram_token,
        chat_id=settings.telegram_chat_id,
        dry_run=settings.dry_run,
    )

    if args.check_telegram:
        try:
            username = notifier.check()
        except TelegramError as exc:
            print(f"FAILED: {exc}")
            return 1
        if not settings.telegram_chat_id:
            print(f"Token is valid (@{username}) but no chat id is set.")
            return 1
        notifier.send_message("F&amp;O OI blast scanner is wired up correctly.")
        print(f"OK: sent a test message as @{username} to chat {settings.telegram_chat_id}")
        return 0

    if not settings.dry_run and not settings.telegram_configured:
        LOGGER.error(
            "Telegram is not configured. Set TOKEN and chat_idADMIN in secrets/.env.dev, "
            "or pass --dry-run to print alerts instead."
        )
        return 2

    # A dry run must never write to the real de-dup store: rehearsing a scan
    # would otherwise silence the genuine alert that follows it.
    state = AlertState(":memory:" if settings.dry_run else settings.state_db)
    try:
        if args.schedule:
            return run_scheduled(settings, notifier, state)
        try:
            run_once(settings, notifier, state, as_of=parse_day(args.date))
        except BhavcopyUnavailable as exc:
            LOGGER.error("%s", exc)
            return 3
        except TelegramError as exc:
            LOGGER.error("%s", exc)
            return 4
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
