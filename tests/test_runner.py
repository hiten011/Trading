"""End-to-end: cache file in, Telegram message out. Plus the scheduler maths."""

import os
import pickle
from datetime import datetime

import numpy as np
import pytest

from custom import datarefresh, runner
from custom.config import Settings
from custom.notify import TelegramNotifier
from custom.report import build_message, render_table, to_frame, write_csv
from custom.strategies.base import Signal
from tests.factories import breakout_frame, flat_frame


class _RecordingNotifier(TelegramNotifier):
    """Captures what would have been sent instead of hitting the network."""

    def __init__(self):
        super().__init__(token="t", chat_id="42")
        self.messages = []
        self.documents = []

    def send_message(self, text, parse_mode="HTML", silent=False):
        self.messages.append(text)

    def send_document(self, file_path, caption=""):
        self.documents.append((file_path, caption))


@pytest.fixture
def cache_dir(tmp_path):
    """A PKScreener-shaped cache: one stock that fires, several that do not."""
    payload = {
        "WINNER": breakout_frame(),
        "BORING": flat_frame(),
        "ALSOBORING": flat_frame(price=250.0),
    }
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    with open(data_dir / "stock_data_01012024.pkl", "wb") as handle:
        pickle.dump(payload, handle)
    return str(data_dir)


def _settings(cache_dir, **overrides):
    defaults = dict(
        universe="auto",
        data_dir=cache_dir,
        strategy="my_indicator",
        min_price=1.0,
        max_price=1e9,
        min_avg_volume=0.0,
        attach_csv=False,
        max_alerts=40,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# --- the scan ------------------------------------------------------------

def test_a_scan_finds_the_one_matching_stock_and_alerts(cache_dir):
    notifier = _RecordingNotifier()
    signals = runner.run_scan(_settings(cache_dir), notifier)

    assert [signal.symbol for signal in signals] == ["WINNER"]
    assert len(notifier.messages) == 1
    assert "WINNER" in notifier.messages[0]
    assert "3 stocks scanned" in notifier.messages[0]


def test_nothing_is_sent_when_nothing_matches(cache_dir, fake_strategies):
    notifier = _RecordingNotifier()
    signals = runner.run_scan(_settings(cache_dir, strategy="never_matches"), notifier)
    assert signals == []
    assert notifier.messages == []


def test_notify_empty_sends_the_all_clear(cache_dir, fake_strategies):
    notifier = _RecordingNotifier()
    runner.run_scan(_settings(cache_dir, strategy="never_matches", notify_empty=True), notifier)
    assert len(notifier.messages) == 1
    assert "No matches" in notifier.messages[0]


def test_a_strategy_that_raises_does_not_abort_the_scan(cache_dir, fake_strategies):
    notifier = _RecordingNotifier()
    signals = runner.run_scan(_settings(cache_dir, strategy="always_raises"), notifier)
    assert signals == []  # every symbol failed, but the scan completed


def test_symbols_override_restricts_the_scan(cache_dir):
    notifier = _RecordingNotifier()
    signals = runner.run_scan(_settings(cache_dir), notifier, symbols=["BORING"])
    assert signals == []
    assert notifier.messages == []


def test_liquidity_filters_are_applied_before_the_strategy(cache_dir):
    notifier = _RecordingNotifier()
    # WINNER trades around 140, so a 10,000 floor excludes everything.
    signals = runner.run_scan(_settings(cache_dir, min_price=10_000), notifier)
    assert signals == []


def test_max_alerts_caps_the_message_but_reports_the_true_total(cache_dir, fake_strategies):
    notifier = _RecordingNotifier()
    settings = _settings(cache_dir, strategy="always_matches", max_alerts=2)
    signals = runner.run_scan(settings, notifier)
    assert len(signals) == 2
    assert "top 2 of 3" in notifier.messages[0]


def test_a_strategy_returning_the_wrong_type_is_skipped(cache_dir, fake_strategies):
    notifier = _RecordingNotifier()
    signals = runner.run_scan(_settings(cache_dir, strategy="returns_wrong_type"), notifier)
    assert signals == []


def test_hits_are_ranked_by_score(cache_dir, fake_strategies):
    notifier = _RecordingNotifier()
    signals = runner.run_scan(_settings(cache_dir, strategy="always_matches"), notifier)
    scores = [signal.score for signal in signals]
    assert scores == sorted(scores, reverse=True)


def test_csv_is_attached_when_asked(cache_dir, tmp_path):
    notifier = _RecordingNotifier()
    runner.run_scan(_settings(cache_dir, attach_csv=True), notifier)
    assert len(notifier.documents) == 1
    assert notifier.documents[0][0].endswith(".csv")
    assert os.path.isfile(notifier.documents[0][0])


# --- report rendering ------------------------------------------------------

def test_message_reports_zero_matches_honestly():
    message = build_message([], "My strategy", scanned=1800)
    assert "No matches out of 1800" in message


def test_message_escapes_html_in_a_strategy_name():
    message = build_message([], "A <b>bold</b> & risky idea", scanned=1)
    assert "&lt;b&gt;" in message
    assert "&amp;" in message


def test_table_drops_the_why_column_when_it_would_wrap():
    signals = [
        Signal("RELIANCE", "BUY", 2900.0, "a very long explanation that will not fit on a phone",
               extras={"RSI": 61.0, "Vol x": 3.2})
    ]
    table = render_table(to_frame(signals))
    assert "RELIANCE" in table
    assert "a very long explanation" not in table


def test_write_csv_contains_every_hit(tmp_path):
    signals = [Signal(f"SYM{index}", "BUY", 100.0 + index) for index in range(5)]
    path = write_csv(signals, str(tmp_path), "My strategy")
    assert path is not None
    contents = open(path).read()
    assert all(f"SYM{index}" in contents for index in range(5))


def test_write_csv_skips_an_empty_result(tmp_path):
    assert write_csv([], str(tmp_path), "My strategy") is None


# --- scheduling ------------------------------------------------------------

def test_parse_run_times_sorts_and_deduplicates():
    assert runner.parse_run_times(["15:45", "09:05", "15:45"]) == [(9, 5), (15, 45)]


def test_parse_run_times_ignores_junk():
    assert runner.parse_run_times(["nonsense", "25:00", "12:99", "10:30"]) == [(10, 30)]


def test_next_run_is_later_today_when_one_is_left():
    now = datetime(2024, 5, 1, 10, 0)  # a Wednesday
    assert runner.next_run_at(now, [(9, 5), (15, 45)], True) == datetime(2024, 5, 1, 15, 45)


def test_next_run_rolls_over_to_the_next_day():
    now = datetime(2024, 5, 1, 16, 0)
    assert runner.next_run_at(now, [(9, 5), (15, 45)], True) == datetime(2024, 5, 2, 9, 5)


def test_next_run_skips_the_weekend_on_trading_days_only():
    friday_evening = datetime(2024, 5, 3, 16, 0)
    assert runner.next_run_at(friday_evening, [(15, 45)], True) == datetime(2024, 5, 6, 15, 45)


def test_next_run_includes_the_weekend_when_told_to():
    friday_evening = datetime(2024, 5, 3, 16, 0)
    assert runner.next_run_at(friday_evening, [(15, 45)], False) == datetime(2024, 5, 4, 15, 45)


# --- auto data refresh -----------------------------------------------------

def test_refresh_is_skipped_when_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(runner, "refresh_if_stale", lambda *a, **k: called.append(True))
    settings = Settings(auto_refresh_data=False)
    runner._refresh_data_if_due(settings)
    assert called == []


def test_refresh_is_attempted_when_enabled(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        runner,
        "refresh_if_stale",
        lambda data_dirs, max_age_hours: captured.update(data_dirs=data_dirs, max_age_hours=max_age_hours),
    )
    settings = Settings(
        auto_refresh_data=True,
        data_max_age_hours=12,
        data_dir="/a/results/Data:/a/actions-data-download",
    )
    runner._refresh_data_if_due(settings)
    assert captured["data_dirs"] == ["/a/results/Data", "/a/actions-data-download"]
    assert captured["max_age_hours"] == 12


def test_a_refresh_failure_does_not_crash_the_caller(monkeypatch):
    def boom(*a, **k):
        raise datarefresh.DataRefreshError("downloader exited 1")

    monkeypatch.setattr(runner, "refresh_if_stale", boom)
    settings = Settings(auto_refresh_data=True)
    runner._refresh_data_if_due(settings)  # must not raise


def test_once_mode_never_triggers_a_refresh(cache_dir, monkeypatch):
    """--once must stay fast for `make dry-run` iteration -- no download wait."""
    called = []
    monkeypatch.setattr(runner, "refresh_if_stale", lambda *a, **k: called.append(True))
    notifier = _RecordingNotifier()
    runner.run_scan(_settings(cache_dir, auto_refresh_data=True), notifier)
    assert called == []


def test_scheduled_interval_mode_refreshes_before_each_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_refresh_data_if_due", lambda settings: calls.append("refresh"))
    monkeypatch.setattr(runner, "_guarded_scan", lambda *a, **k: calls.append("scan"))

    call_count = {"n": 0}

    def fake_sleep(_seconds):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    settings = Settings(interval_minutes=5)
    with pytest.raises(KeyboardInterrupt):
        runner.run_scheduled(settings, _RecordingNotifier())

    assert calls[:2] == ["refresh", "scan"]


def test_scheduled_run_at_mode_refreshes_before_each_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_refresh_data_if_due", lambda settings: calls.append("refresh"))
    monkeypatch.setattr(runner, "_guarded_scan", lambda *a, **k: calls.append("scan"))
    monkeypatch.setattr(runner, "now_in", lambda tz: datetime(2024, 5, 1, 15, 44))

    call_count = {"n": 0}

    def fake_sleep(_seconds):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    settings = Settings(run_at=["15:45"], trading_days_only=False)
    with pytest.raises(KeyboardInterrupt):
        runner.run_scheduled(settings, _RecordingNotifier())

    assert calls[:2] == ["refresh", "scan"]


# --- CLI -------------------------------------------------------------------

def test_list_strategies_exits_cleanly(capsys):
    assert runner.main(["--list-strategies"]) == 0
    assert "my_indicator" in capsys.readouterr().out


def test_a_real_run_without_telegram_credentials_refuses_to_start(monkeypatch, cache_dir):
    monkeypatch.setenv("PKS_DATA_DIR", cache_dir)
    assert runner.main(["--once"]) == 2  # no token, and --dry-run was not passed


def test_dry_run_works_without_any_credentials(monkeypatch, cache_dir, capsys):
    monkeypatch.setenv("PKS_DATA_DIR", cache_dir)
    monkeypatch.setenv("PKS_MIN_AVG_VOLUME", "0")
    monkeypatch.setenv("PKS_ATTACH_CSV", "0")
    assert runner.main(["--once", "--dry-run"]) == 0
    assert "WINNER" in capsys.readouterr().out


def test_a_missing_cache_reports_the_fix(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PKS_DATA_DIR", str(tmp_path))
    assert runner.main(["--once", "--dry-run"]) == 3
