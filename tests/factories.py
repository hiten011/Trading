"""Synthetic candle builders so tests do not need market data."""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_frame(closes, volumes=None, start="2024-01-01") -> pd.DataFrame:
    """Build an OHLCV frame from a close series, with sane highs/lows."""
    closes = np.asarray(closes, dtype=float)
    index = pd.bdate_range(start=start, periods=len(closes))
    if volumes is None:
        volumes = np.full(len(closes), 1_000_000.0)
    volumes = np.asarray(volumes, dtype=float)
    return pd.DataFrame(
        {
            "Open": closes * 0.995,
            "High": closes * 1.01,
            "Low": closes * 0.99,
            "Close": closes,
            "Volume": volumes,
        },
        index=index,
    )


def flat_frame(periods: int = 120, price: float = 100.0) -> pd.DataFrame:
    """A stock going nowhere: no strategy should fire on this."""
    return make_frame(np.full(periods, price))


def breakout_frame(periods: int = 120) -> pd.DataFrame:
    """An uptrend with normal pullbacks that closes at a new high on volume.

    The wobble matters: a perfectly straight line prints RSI 100 and an ATR of
    almost nothing, which no realistic indicator would ever see.
    """
    trend = np.linspace(100.0, 140.0, periods)
    wobble = 3.0 * np.sin(np.linspace(0, 9 * np.pi, periods))
    closes = trend + wobble
    closes[-1] = closes[-2] * 1.03  # decisive breakout candle
    volumes = np.full(periods, 1_000_000.0)
    volumes[-1] = 5_000_000.0
    return make_frame(closes, volumes)
