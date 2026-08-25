"""The contract every strategy implements, plus indicator building blocks.

The indicator helpers here are plain pandas so they work anywhere -- including
your laptop without TA-Lib, and inside the test suite. TA-Lib *is* available in
the container if you prefer it::

    import talib
    rsi = talib.RSI(df["Close"].values, timeperiod=14)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

# Column names your ``evaluate`` receives, always in this order.
OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


@dataclass
class Signal:
    """One alert-worthy hit.

    Args:
        symbol: NSE symbol, no ``.NS`` suffix.
        direction: ``"BUY"``, ``"SELL"`` or any label you like.
        price: The price to quote in the alert (usually the last close).
        reason: One short line explaining why this triggered.
        extras: Extra columns for the alert table, e.g. ``{"RSI": 62.4}``.
                Floats are rounded to two decimals when rendered.
        score: Optional ranking value. Hits are sorted by score, descending,
               so your strongest setups survive ``PKS_MAX_ALERTS``.
    """

    symbol: str
    direction: str = "BUY"
    price: float = 0.0
    reason: str = ""
    extras: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    def as_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "Symbol": self.symbol,
            "Signal": self.direction,
            "Price": round(float(self.price), 2),
        }
        for key, value in self.extras.items():
            row[key] = round(value, 2) if isinstance(value, (int, float, np.floating)) else value
        if self.reason:
            row["Why"] = self.reason
        return row


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing, seeded with a simple average.

    TA-Lib and TradingView seed the running average with the plain mean of the
    first ``period`` observations and only then apply the recursive smoothing.
    Feeding the raw series straight into ``ewm`` instead seeds with a single
    value, which is visibly wrong for a few dozen bars -- long enough to matter
    on a 250-bar window.
    """
    values = series.to_numpy(dtype=float)
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size < period:
        return pd.Series(np.nan, index=series.index, dtype=float)

    seed_position = valid[period - 1]
    seeded = np.full(values.shape, np.nan)
    seeded[seed_position] = np.nanmean(values[valid[0] : seed_position + 1])
    seeded[seed_position + 1 :] = values[seed_position + 1 :]
    return pd.Series(seeded, index=series.index).ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI -- the same smoothing TA-Lib and TradingView use."""
    delta = series.diff()
    avg_gain = wilder_smooth(delta.clip(lower=0.0), period)
    avg_loss = wilder_smooth(-delta.clip(upper=0.0), period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 means an unbroken run of gains: RSI is 100 by definition.
    return result.where(avg_loss != 0.0, 100.0).where(avg_gain.notna())


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["Close"].shift(1)
    ranges = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - previous_close).abs(),
            (frame["Low"] - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average true range, Wilder-smoothed."""
    return wilder_smooth(true_range(frame), period)


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> "tuple[pd.Series, pd.Series, pd.Series]":
    """Return ``(macd_line, signal_line, histogram)``."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line, macd_line - signal_line


def bollinger(
    series: pd.Series, period: int = 20, deviations: float = 2.0
) -> "tuple[pd.Series, pd.Series, pd.Series]":
    """Return ``(upper, middle, lower)`` Bollinger bands."""
    middle = sma(series, period)
    spread = series.rolling(window=period, min_periods=period).std(ddof=0) * deviations
    return middle + spread, middle, middle - spread


def rolling_vwap(frame: pd.DataFrame, period: int = 20) -> pd.Series:
    """Volume weighted average price over a rolling window."""
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
    weighted = (typical * frame["Volume"]).rolling(window=period, min_periods=period).sum()
    volume = frame["Volume"].rolling(window=period, min_periods=period).sum()
    return weighted / volume.replace(0.0, np.nan)


def pct_change(series: pd.Series, periods: int = 1) -> pd.Series:
    """Percentage change over ``periods`` bars, expressed 0-100."""
    return series.pct_change(periods=periods) * 100.0


def crossed_above(fast: pd.Series, slow: pd.Series) -> bool:
    """True when ``fast`` closed above ``slow`` on the latest bar only."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    previous = fast.iloc[-2] <= slow.iloc[-2]
    current = fast.iloc[-1] > slow.iloc[-1]
    return bool(previous and current) and not _has_nan(fast, slow)


def crossed_below(fast: pd.Series, slow: pd.Series) -> bool:
    """True when ``fast`` closed below ``slow`` on the latest bar only."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    previous = fast.iloc[-2] >= slow.iloc[-2]
    current = fast.iloc[-1] < slow.iloc[-1]
    return bool(previous and current) and not _has_nan(fast, slow)


def _has_nan(*series: pd.Series) -> bool:
    return any(pd.isna(item.iloc[-1]) or pd.isna(item.iloc[-2]) for item in series)


def last(series: pd.Series, offset: int = 0) -> float:
    """Latest value (``offset=1`` is the previous bar); NaN-safe as a float."""
    if series is None or len(series) <= offset:
        return float("nan")
    return float(series.iloc[-1 - offset])


__all__ = [
    "Signal",
    "OHLCV_COLUMNS",
    "sma",
    "ema",
    "rsi",
    "wilder_smooth",
    "atr",
    "true_range",
    "macd",
    "bollinger",
    "rolling_vwap",
    "pct_change",
    "crossed_above",
    "crossed_below",
    "last",
]
