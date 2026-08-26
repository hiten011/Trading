"""Cache-freshness detection and the subprocess wrapper around PKScreener's
own downloader, which is what lets a single --schedule container stay useful
for weeks without an external cron job re-running `make data`."""

import os
import subprocess
import time

import pytest

from custom import datarefresh


def _touch(path, age_hours=0.0):
    with open(path, "wb") as handle:
        handle.write(b"\x80\x04}\x94.")  # pickled {} -- content doesn't matter here
    if age_hours:
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
    return path


# --- newest_cache_age_hours -------------------------------------------------

def test_no_cache_files_is_infinitely_stale(tmp_path):
    assert datarefresh.newest_cache_age_hours([str(tmp_path)]) == float("inf")


def test_a_fresh_file_is_close_to_zero_hours_old(tmp_path):
    _touch(tmp_path / "stock_data_01012024.pkl")
    assert datarefresh.newest_cache_age_hours([str(tmp_path)]) < 0.01


def test_age_reflects_the_files_actual_mtime(tmp_path):
    _touch(tmp_path / "stock_data_01012024.pkl", age_hours=30)
    age = datarefresh.newest_cache_age_hours([str(tmp_path)])
    assert 29.9 < age < 30.1


def test_intraday_files_do_not_count(tmp_path):
    _touch(tmp_path / "intraday_stock_data_01012024.pkl", age_hours=0)
    # Only the intraday file exists, and it's excluded -- so still "no cache".
    assert datarefresh.newest_cache_age_hours([str(tmp_path)]) == float("inf")


def test_picks_the_newest_across_multiple_directories(tmp_path):
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    _touch(old_dir / "stock_data_01012024.pkl", age_hours=48)
    _touch(new_dir / "stock_data_02012024.pkl", age_hours=1)
    age = datarefresh.newest_cache_age_hours([str(old_dir), str(new_dir)])
    assert 0.9 < age < 1.1


def test_missing_directories_do_not_raise(tmp_path):
    assert datarefresh.newest_cache_age_hours(["/nope/nowhere", str(tmp_path)]) == float("inf")


# --- refresh_if_stale --------------------------------------------------------

def test_refresh_if_stale_skips_a_fresh_cache(tmp_path, monkeypatch):
    _touch(tmp_path / "stock_data_01012024.pkl", age_hours=1)
    called = []
    monkeypatch.setattr(datarefresh, "refresh", lambda **kwargs: called.append(True))
    ran = datarefresh.refresh_if_stale([str(tmp_path)], max_age_hours=20)
    assert ran is False
    assert called == []


def test_refresh_if_stale_refreshes_an_old_cache(tmp_path, monkeypatch):
    _touch(tmp_path / "stock_data_01012024.pkl", age_hours=25)
    called = []
    monkeypatch.setattr(datarefresh, "refresh", lambda **kwargs: called.append(True))
    ran = datarefresh.refresh_if_stale([str(tmp_path)], max_age_hours=20)
    assert ran is True
    assert called == [True]


def test_refresh_if_stale_refreshes_when_theres_no_cache_at_all(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(datarefresh, "refresh", lambda **kwargs: called.append(True))
    ran = datarefresh.refresh_if_stale([str(tmp_path)], max_age_hours=20)
    assert ran is True


def test_a_refresh_failure_propagates_to_the_caller(tmp_path, monkeypatch):
    _touch(tmp_path / "stock_data_01012024.pkl", age_hours=25)

    def boom(**kwargs):
        raise datarefresh.DataRefreshError("downloader exited 1")

    monkeypatch.setattr(datarefresh, "refresh", boom)
    with pytest.raises(datarefresh.DataRefreshError):
        datarefresh.refresh_if_stale([str(tmp_path)], max_age_hours=20)


# --- refresh (the subprocess wrapper) ---------------------------------------

def test_refresh_raises_cleanly_when_pkscreener_is_not_installed(monkeypatch):
    monkeypatch.setattr(datarefresh, "PKSCREENER_ENTRYPOINT", "/nonexistent/pkscreenercli.py")
    with pytest.raises(datarefresh.DataRefreshError, match="not found"):
        datarefresh.refresh()


def test_refresh_raises_on_a_nonzero_exit(monkeypatch, tmp_path):
    fake_entrypoint = tmp_path / "pkscreener" / "pkscreenercli.py"
    fake_entrypoint.parent.mkdir(parents=True)
    fake_entrypoint.write_text("")
    monkeypatch.setattr(datarefresh, "PKSCREENER_ENTRYPOINT", str(fake_entrypoint))
    monkeypatch.setattr(datarefresh, "PKSCREENER_ROOT", str(tmp_path))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(datarefresh.subprocess, "run", fake_run)
    with pytest.raises(datarefresh.DataRefreshError, match="exited 1"):
        datarefresh.refresh()


def test_refresh_raises_on_timeout(monkeypatch, tmp_path):
    fake_entrypoint = tmp_path / "pkscreener" / "pkscreenercli.py"
    fake_entrypoint.parent.mkdir(parents=True)
    fake_entrypoint.write_text("")
    monkeypatch.setattr(datarefresh, "PKSCREENER_ENTRYPOINT", str(fake_entrypoint))
    monkeypatch.setattr(datarefresh, "PKSCREENER_ROOT", str(tmp_path))

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pkscreenercli.py", timeout=1)

    monkeypatch.setattr(datarefresh.subprocess, "run", fake_run)
    with pytest.raises(datarefresh.DataRefreshError, match="did not finish"):
        datarefresh.refresh(timeout=1)


def test_refresh_succeeds_and_sets_the_runner_env_var(monkeypatch, tmp_path):
    fake_entrypoint = tmp_path / "pkscreener" / "pkscreenercli.py"
    fake_entrypoint.parent.mkdir(parents=True)
    fake_entrypoint.write_text("")
    monkeypatch.setattr(datarefresh, "PKSCREENER_ENTRYPOINT", str(fake_entrypoint))
    monkeypatch.setattr(datarefresh, "PKSCREENER_ROOT", str(tmp_path))
    monkeypatch.delenv("RUNNER", raising=False)

    captured = {}

    def fake_run(cmd, cwd=None, env=None, timeout=None, capture_output=None, text=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(datarefresh.subprocess, "run", fake_run)
    datarefresh.refresh()  # must not raise

    assert captured["cmd"] == ["python3", "pkscreener/pkscreenercli.py", "-a", "Y", "-e", "-d"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["RUNNER"] == "GitHub_Actions"


def test_refresh_does_not_override_an_existing_runner_value(monkeypatch, tmp_path):
    fake_entrypoint = tmp_path / "pkscreener" / "pkscreenercli.py"
    fake_entrypoint.parent.mkdir(parents=True)
    fake_entrypoint.write_text("")
    monkeypatch.setattr(datarefresh, "PKSCREENER_ENTRYPOINT", str(fake_entrypoint))
    monkeypatch.setattr(datarefresh, "PKSCREENER_ROOT", str(tmp_path))
    monkeypatch.setenv("RUNNER", "some_other_value")

    captured = {}

    def fake_run(cmd, cwd=None, env=None, timeout=None, capture_output=None, text=None):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(datarefresh.subprocess, "run", fake_run)
    datarefresh.refresh()
    assert captured["env"]["RUNNER"] == "some_other_value"
