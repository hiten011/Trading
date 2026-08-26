"""Keeping the candle cache fresh from inside a single long-running container.

`data-refresh` (the compose service) runs PKScreener's downloader once and
exits -- fine for a local one-shot warmup, not enough for a container meant
to run unattended on a server for weeks. This module lets `alerts` refresh
its own cache periodically, by shelling out to the exact same downloader
PKScreener's own image ships (our Dockerfile is FROM pkjmesra/pkscreener, so
it's the same package, same binary, already installed) -- no separate
container, no external cron needed for the common case.
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import time
from typing import Sequence

LOGGER = logging.getLogger("custom.datarefresh")

PKSCREENER_ROOT = "/PKScreener-main"
PKSCREENER_ENTRYPOINT = os.path.join(PKSCREENER_ROOT, "pkscreener", "pkscreenercli.py")
DEFAULT_TIMEOUT_SECONDS = 20 * 60  # a full NSE download; generous but not unbounded


class DataRefreshError(RuntimeError):
    """Raised when PKScreener's own downloader can't run or exits non-zero."""


def newest_cache_age_hours(data_dirs: Sequence[str]) -> float:
    """Hours since the most recently modified daily cache file, or inf if none exist."""
    newest_mtime = None
    for directory in data_dirs:
        for path in glob.glob(os.path.join(directory, "stock_data_*.pkl")):
            if os.path.basename(path).startswith("intraday_"):
                continue
            mtime = os.path.getmtime(path)
            if newest_mtime is None or mtime > newest_mtime:
                newest_mtime = mtime
    if newest_mtime is None:
        return float("inf")
    return (time.time() - newest_mtime) / 3600.0


def refresh(timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
    """Run PKScreener's own downloader in-process, the same command
    `docker compose run --rm data-refresh` runs, just from inside this
    container instead of a separate one.
    """
    if not os.path.isfile(PKSCREENER_ENTRYPOINT):
        raise DataRefreshError(
            f"{PKSCREENER_ENTRYPOINT} not found -- this image isn't built FROM "
            "pkjmesra/pkscreener, so it has no downloader to call."
        )

    env = dict(os.environ)
    # Bypasses PKScreener's first-run OTP login prompt; see the RUNNER
    # comment in docker-compose.yml for why this is always set there too.
    env.setdefault("RUNNER", "GitHub_Actions")

    LOGGER.info("Refreshing the market data cache (this can take a while)...")
    try:
        result = subprocess.run(
            ["python3", "pkscreener/pkscreenercli.py", "-a", "Y", "-e", "-d"],
            cwd=PKSCREENER_ROOT,
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise DataRefreshError(f"Downloader did not finish within {timeout}s") from exc

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-2000:]
        raise DataRefreshError(f"Downloader exited {result.returncode}: {tail}")
    LOGGER.info("Market data cache refreshed.")


def refresh_if_stale(data_dirs: Sequence[str], max_age_hours: float) -> bool:
    """Refresh only if the newest cache file is older than max_age_hours.

    Returns True if a refresh actually ran. A refresh failure propagates as
    DataRefreshError -- callers running unattended should catch it and keep
    scanning against whatever cache already exists rather than crash the loop.
    """
    age = newest_cache_age_hours(data_dirs)
    if age < max_age_hours:
        LOGGER.debug("Cache is %.1fh old (< %.0fh threshold) -- not refreshing", age, max_age_hours)
        return False
    LOGGER.info("Cache is %.1fh old (>= %.0fh threshold) -- refreshing", age, max_age_hours)
    refresh()
    return True
