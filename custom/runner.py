"""Entry point: scan every Indian stock with your indicator, alert on Telegram.

    python3 -m custom.runner --once                one scan, then exit
    python3 -m custom.runner --once --dry-run      same, printed to stdout
    python3 -m custom.runner --schedule            run at PKS_RUN_AT forever
    python3 -m custom.runner --check-telegram      verify the bot credentials
    python3 -m custom.runner --list-strategies     what is available to run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from custom import strategies
from custom.config import Settings, configure_logging
from custom.data import NoDataAvailable, apply_liquidity_filters, load_candles
from custom.datarefresh import DataRefreshError, refresh_if_stale
from custom.notify import TelegramError, TelegramNotifier
from custom.report import build_message, write_csv
from custom.strategies.base import Signal
from custom.universe import resolve as resolve_universe

LOGGER = logging.getLogger("custom.runner")


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


# ---------------------------------------------------------------------------
# One scan
# ---------------------------------------------------------------------------

def run_scan(
    settings: Settings,
    notifier: TelegramNotifier,
    symbols: Optional[List[str]] = None,
) -> List[Signal]:
    """Resolve the universe, run the strategy over it, and alert. Returns hits."""
    started = time.monotonic()
    strategy = strategies.load(settings.strategy)
    strategy_name = getattr(strategy, "NAME", settings.strategy)
    strategy_description = getattr(strategy, "DESCRIPTION", "")

    if symbols is None:
        symbols = resolve_universe(
            mode=settings.universe,
            data_dirs=settings.data_dirs,
            universe_file=settings.universe_file,
            index_option=settings.index_option,
        )

    frames = load_candles(
        data_dirs=settings.data_dirs,
        symbols=symbols,
        lookback_days=settings.lookback_days,
    )
    frames = apply_liquidity_filters(
        frames,
        min_price=settings.min_price,
        max_price=settings.max_price,
        min_avg_volume=settings.min_avg_volume,
    )

    signals: List[Signal] = []
    failures = 0
    for symbol, frame in frames.items():
        try:
            signal = strategy.evaluate(symbol, frame)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the scan
            failures += 1
            LOGGER.debug("evaluate(%s) raised %s", symbol, exc, exc_info=True)
            continue
        if signal is None:
            continue
        if not isinstance(signal, Signal):
            LOGGER.warning("evaluate(%s) returned %r, expected Signal or None", symbol, type(signal))
            continue
        if not signal.symbol:
            signal.symbol = symbol
        signals.append(signal)

    if failures:
        LOGGER.warning("%d symbols raised inside the strategy (run with PKS_LOG_LEVEL=DEBUG)", failures)

    signals.sort(key=lambda item: (item.score, item.symbol), reverse=True)
    total_hits = len(signals)
    if settings.max_alerts > 0:
        signals = signals[: settings.max_alerts]

    elapsed = time.monotonic() - started
    LOGGER.info(
        "Scanned %d stocks in %.1fs -> %d match(es)%s",
        len(frames),
        elapsed,
        total_hits,
        f", alerting on top {len(signals)}" if total_hits > len(signals) else "",
    )

    _deliver(
        settings=settings,
        notifier=notifier,
        signals=signals,
        scanned=len(frames),
        total_hits=total_hits,
        strategy_name=strategy_name,
        strategy_description=strategy_description,
    )
    return signals


def _deliver(
    settings: Settings,
    notifier: TelegramNotifier,
    signals: Sequence[Signal],
    scanned: int,
    total_hits: int,
    strategy_name: str,
    strategy_description: str,
) -> None:
    if not signals and not settings.notify_empty:
        LOGGER.info("No matches and PKS_NOTIFY_EMPTY=0, so nothing is sent")
        return

    message = build_message(
        signals=signals,
        strategy_name=strategy_name,
        strategy_description=strategy_description,
        scanned=scanned,
        as_of=now_in(settings.timezone),
        truncated_from=total_hits,
    )

    try:
        notifier.send_message(message)
        if settings.attach_csv and signals:
            csv_path = write_csv(signals, directory=settings.reports_dir, strategy_name=strategy_name)
            if csv_path:
                notifier.send_document(csv_path, caption=f"{strategy_name}: {total_hits} match(es)")
    except TelegramError as exc:
        LOGGER.error("Telegram delivery failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def parse_run_times(values: Sequence[str]) -> List[tuple]:
    """Parse ``["15:45", "09:05"]`` into sorted ``(hour, minute)`` pairs."""
    times = []
    for value in values:
        try:
            hour_text, minute_text = value.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            LOGGER.warning("Ignoring malformed PKS_RUN_AT entry %r (want HH:MM)", value)
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            times.append((hour, minute))
        else:
            LOGGER.warning("Ignoring out-of-range PKS_RUN_AT entry %r", value)
    return sorted(set(times))


def next_run_at(now: datetime, run_times: Sequence[tuple], trading_days_only: bool) -> datetime:
    """The next moment we should scan, skipping weekends when asked to."""
    candidates = []
    for day_offset in range(0, 8):
        day = now + timedelta(days=day_offset)
        if trading_days_only and day.weekday() >= 5:  # 5=Sat, 6=Sun
            continue
        for hour, minute in run_times:
            moment = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if moment > now:
                candidates.append(moment)
    return min(candidates)


def _refresh_data_if_due(settings: Settings) -> None:
    """Keep the cache from going stale over a long-running container's lifetime.

    Only called from run_scheduled -- --once stays fast and predictable for
    `make dry-run` iteration, which would otherwise wait on a ~10-30 minute
    download every single test run.
    """
    if not settings.auto_refresh_data:
        return
    try:
        refresh_if_stale(settings.data_dirs, settings.data_max_age_hours)
    except DataRefreshError as exc:
        LOGGER.warning("Data refresh failed, scanning against the existing cache: %s", exc)


def run_scheduled(
    settings: Settings, notifier: TelegramNotifier, symbols: Optional[List[str]] = None
) -> int:
    run_times = parse_run_times(settings.run_at)

    if settings.interval_minutes > 0:
        LOGGER.info("Scanning every %d minute(s)", settings.interval_minutes)
        while True:
            _refresh_data_if_due(settings)
            _guarded_scan(settings, notifier, symbols)
            time.sleep(settings.interval_minutes * 60)

    if not run_times:
        LOGGER.error(
            "Nothing scheduled: set PKS_RUN_AT (e.g. 15:45) or PKS_INTERVAL_MINUTES in .env"
        )
        return 2

    LOGGER.info(
        "Scheduled for %s %s (%s)",
        ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in run_times),
        "on trading days" if settings.trading_days_only else "every day",
        settings.timezone,
    )
    while True:
        now = now_in(settings.timezone)
        upcoming = next_run_at(now, run_times, settings.trading_days_only)
        seconds = max((upcoming - now).total_seconds(), 1)
        LOGGER.info("Next scan at %s (in %.0f min)", upcoming.strftime("%Y-%m-%d %H:%M"), seconds / 60)
        time.sleep(seconds)
        _refresh_data_if_due(settings)
        _guarded_scan(settings, notifier, symbols)
        time.sleep(60)  # do not re-fire inside the same minute


def _guarded_scan(
    settings: Settings, notifier: TelegramNotifier, symbols: Optional[List[str]] = None
) -> None:
    """Run a scan; a failure logs and waits for the next slot rather than exiting."""
    try:
        run_scan(settings, notifier, symbols=symbols)
    except NoDataAvailable as exc:
        LOGGER.error("No market data: %s", exc)
    except TelegramError as exc:
        LOGGER.error("Telegram error: %s", exc)
    except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
        LOGGER.exception("Scan failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="custom.runner",
        description="Screen every Indian stock with your own indicator and alert on Telegram.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one scan and exit")
    mode.add_argument("--schedule", action="store_true", help="Run on the PKS_RUN_AT schedule")
    mode.add_argument("--check-telegram", action="store_true", help="Verify the Telegram credentials")
    mode.add_argument("--list-strategies", action="store_true", help="List available strategies")

    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them")
    parser.add_argument("--strategy", help="Override PKS_STRATEGY for this run")
    parser.add_argument("--symbols", help="Comma-separated symbols to scan instead of the full universe")
    parser.add_argument("--log-level", help="Override PKS_LOG_LEVEL (DEBUG/INFO/WARNING/ERROR)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    settings = Settings.from_env()
    if args.strategy:
        settings.strategy = args.strategy
    if args.dry_run:
        settings.dry_run = True
    if args.log_level:
        settings.log_level = args.log_level.upper()

    configure_logging(settings.log_level)
    LOGGER.info("Settings: %s", settings.describe())

    if args.list_strategies:
        found = strategies.available()
        print("Available strategies (set PKS_STRATEGY to one of these):")
        for name in found:
            print(f"  - {name}")
        if not found:
            print("  (none found in custom/strategies/)")
        return 0

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
            print("Add chat_idADMIN to secrets/.env.dev - get yours from @userinfobot on Telegram.")
            return 1
        notifier.send_message("PKScreener alerts are wired up correctly.")
        print(f"OK: sent a test message as @{username} to chat {settings.telegram_chat_id}")
        return 0

    if not settings.dry_run and not settings.telegram_configured:
        LOGGER.error(
            "Telegram is not configured. Set TOKEN and chat_idADMIN in secrets/.env.dev, "
            "or pass --dry-run to print alerts instead."
        )
        return 2

    symbols = None
    if args.symbols:
        symbols = [part.strip().upper() for part in args.symbols.split(",") if part.strip()]

    if args.schedule:
        return run_scheduled(settings, notifier, symbols=symbols)

    try:
        run_scan(settings, notifier, symbols=symbols)
    except NoDataAvailable as exc:
        LOGGER.error("%s", exc)
        return 3
    except TelegramError as exc:
        LOGGER.error("%s", exc)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
