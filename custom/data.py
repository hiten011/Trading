"""Loading OHLCV candles for the whole Indian market.

Primary source is the pickle cache PKScreener maintains at
``results/Data/stock_data_<DDMMYYYY>.pkl`` -- a single file holding daily
candles for every NSE symbol it tracks. Reusing it means our alert runner does
no market-data downloading of its own and cannot get rate limited.

If that cache is missing (first run before ``data-refresh`` has completed), we
fall back to yfinance, which the base image already carries.
"""

from __future__ import annotations

import glob
import logging
import os
import pickle
from typing import Dict, Iterable, List, Optional, Sequence, Union

import pandas as pd

LOGGER = logging.getLogger("custom.data")

OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# Cache files, most specific first. Daily candles are what indicators want;
# intraday is only used if explicitly asked for.
DAILY_PATTERN = "stock_data_*.pkl"
INTRADAY_PATTERN = "intraday_stock_data_*.pkl"

# Where a normal scan writes its cache, and where `pkscreenercli.py -d`
# (download-only) writes it instead. Both are searched, newest file wins.
DEFAULT_DATA_DIRS = (
    "/PKScreener-main/results/Data",
    "/PKScreener-main/actions-data-download",
)


def _as_dirs(data_dirs: Union[str, Sequence[str]]) -> List[str]:
    """Accept a single directory or a list of them."""
    if isinstance(data_dirs, str):
        # A ":"-separated list is convenient to pass through an env var.
        return [part for part in data_dirs.split(":") if part]
    return [part for part in data_dirs if part]


class NoDataAvailable(RuntimeError):
    """Raised when neither the PKScreener cache nor a fallback yields candles."""


def find_cache_file(
    data_dirs: Union[str, Sequence[str]], intraday: bool = False
) -> Optional[str]:
    """Return the most recently modified PKScreener cache file, if any."""
    pattern = INTRADAY_PATTERN if intraday else DAILY_PATTERN
    directories = _as_dirs(data_dirs)

    matches: List[str] = []
    for directory in directories:
        matches.extend(glob.glob(os.path.join(directory, pattern)))

    if not intraday:
        # The daily glob also matches intraday_stock_data_*.pkl, since that
        # filename ends with the pattern we asked for.
        matches = [m for m in matches if not os.path.basename(m).startswith("intraday_")]

    if not matches:
        LOGGER.warning("No %s found under %s", pattern, ", ".join(directories))
        return None

    newest = max(matches, key=os.path.getmtime)
    LOGGER.info("Using PKScreener cache %s", newest)
    return newest


def read_cache(cache_path: str) -> Dict[str, object]:
    """Read PKScreener's pickled ``{symbol: candles}`` dictionary."""
    with open(cache_path, "rb") as handle:
        payload = pickle.load(handle)
    if not isinstance(payload, dict):
        raise NoDataAvailable(f"{cache_path} does not contain a symbol dictionary")
    LOGGER.info("Cache holds %d symbols", len(payload))
    return payload


def to_frame(raw: object) -> Optional[pd.DataFrame]:
    """Normalise one cache entry into an OHLCV DataFrame.

    PKScreener stores entries either as a DataFrame or as ``df.to_dict("split")``
    depending on which code path wrote them, so handle both.
    """
    frame: Optional[pd.DataFrame] = None

    if isinstance(raw, pd.DataFrame):
        frame = raw.copy()
    elif isinstance(raw, dict):
        if {"index", "columns", "data"} <= set(raw.keys()):
            frame = pd.DataFrame(raw["data"], index=raw["index"], columns=raw["columns"])
        else:
            try:
                frame = pd.DataFrame(raw)
            except ValueError:
                return None
    if frame is None or frame.empty:
        return None

    # Column names arrive in mixed case across sources.
    renames = {}
    for column in frame.columns:
        canonical = str(column).strip().title()
        if canonical in OHLCV_COLUMNS:
            renames[column] = canonical
    frame = frame.rename(columns=renames)

    missing = [column for column in OHLCV_COLUMNS if column not in frame.columns]
    if missing:
        return None

    frame = frame[OHLCV_COLUMNS].apply(pd.to_numeric, errors="coerce")

    try:
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    except (TypeError, ValueError):
        return None
    frame = frame[frame.index.notna()]
    if frame.empty:
        return None

    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.dropna(subset=["Close"])
    return frame if not frame.empty else None


def load_from_cache(
    data_dirs: Union[str, Sequence[str]],
    symbols: Optional[Iterable[str]] = None,
    lookback_days: int = 250,
    intraday: bool = False,
) -> Dict[str, pd.DataFrame]:
    """Load candles for ``symbols`` (or every symbol) from PKScreener's cache."""
    cache_path = find_cache_file(data_dirs, intraday=intraday)
    if cache_path is None:
        return {}

    payload = read_cache(cache_path)
    wanted = {s.upper() for s in symbols} if symbols else None

    frames: Dict[str, pd.DataFrame] = {}
    skipped = 0
    for symbol, raw in payload.items():
        name = str(symbol).upper().replace(".NS", "")
        if wanted is not None and name not in wanted:
            continue
        frame = to_frame(raw)
        if frame is None:
            skipped += 1
            continue
        frames[name] = frame.tail(lookback_days) if lookback_days > 0 else frame

    LOGGER.info("Parsed candles for %d symbols (%d unusable entries)", len(frames), skipped)
    return frames


def load_from_yfinance(
    symbols: List[str],
    lookback_days: int = 250,
    chunk_size: int = 200,
) -> Dict[str, pd.DataFrame]:
    """Fallback download straight from Yahoo, in chunks, for NSE tickers."""
    try:
        import yfinance as yf
    except ImportError:
        LOGGER.error("yfinance is not installed; cannot use the fallback data source")
        return {}

    period = f"{max(lookback_days, 30) + 40}d"
    frames: Dict[str, pd.DataFrame] = {}

    for start in range(0, len(symbols), chunk_size):
        chunk = symbols[start : start + chunk_size]
        tickers = [f"{symbol}.NS" for symbol in chunk]
        LOGGER.info("yfinance download %d-%d of %d", start + 1, start + len(chunk), len(symbols))
        try:
            downloaded = yf.download(
                tickers=" ".join(tickers),
                period=period,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001 - network failures are expected
            LOGGER.warning("yfinance chunk failed: %s", exc)
            continue

        for symbol, ticker in zip(chunk, tickers):
            try:
                raw = downloaded[ticker] if isinstance(downloaded.columns, pd.MultiIndex) else downloaded
            except KeyError:
                continue
            frame = to_frame(raw)
            if frame is not None:
                frames[symbol] = frame.tail(lookback_days) if lookback_days > 0 else frame

    LOGGER.info("yfinance returned candles for %d symbols", len(frames))
    return frames


def load_candles(
    data_dirs: Union[str, Sequence[str]],
    symbols: Optional[List[str]] = None,
    lookback_days: int = 250,
    allow_yfinance_fallback: bool = True,
) -> Dict[str, pd.DataFrame]:
    """Load candles, preferring PKScreener's cache over a live download."""
    frames = load_from_cache(data_dirs, symbols=symbols, lookback_days=lookback_days)
    if frames:
        return frames

    if not allow_yfinance_fallback or not symbols:
        raise NoDataAvailable(
            f"No usable candles in {', '.join(_as_dirs(data_dirs))}. "
            "Run `make data` (docker compose run --rm data-refresh) first."
        )

    LOGGER.warning("Cache empty - falling back to yfinance for %d symbols", len(symbols))
    frames = load_from_yfinance(symbols, lookback_days=lookback_days)
    if not frames:
        raise NoDataAvailable("Neither the PKScreener cache nor yfinance returned candles")
    return frames


def apply_liquidity_filters(
    frames: Dict[str, pd.DataFrame],
    min_price: float = 0.0,
    max_price: float = float("inf"),
    min_avg_volume: float = 0.0,
    min_candles: int = 30,
    avg_volume_window: int = 20,
) -> Dict[str, pd.DataFrame]:
    """Drop illiquid / penny / suspended names before the indicator sees them."""
    kept: Dict[str, pd.DataFrame] = {}
    for symbol, frame in frames.items():
        if len(frame) < min_candles:
            continue
        last_close = float(frame["Close"].iloc[-1])
        if not (min_price <= last_close <= max_price):
            continue
        avg_volume = float(frame["Volume"].tail(avg_volume_window).mean())
        if pd.isna(avg_volume) or avg_volume < min_avg_volume:
            continue
        kept[symbol] = frame

    LOGGER.info("%d of %d symbols passed the liquidity filters", len(kept), len(frames))
    return kept
