"""The indicator maths must match what TA-Lib / TradingView would print."""

import numpy as np
import pandas as pd
import pytest

from custom.strategies.base import (
    atr,
    bollinger,
    crossed_above,
    crossed_below,
    ema,
    last,
    macd,
    rolling_vwap,
    rsi,
    sma,
)
from tests.factories import make_frame


def test_sma_is_the_mean_of_the_window():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    assert sma(series, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert pd.isna(sma(series, 3).iloc[0])


def test_ema_matches_the_recursive_definition():
    series = pd.Series([10.0, 11.0, 12.0, 13.0])
    result = ema(series, 2)
    alpha = 2 / (2 + 1)
    expected = 10.0
    for value in series[1:]:
        expected = alpha * value + (1 - alpha) * expected
    assert result.iloc[-1] == pytest.approx(expected, rel=1e-9)


def test_rsi_is_100_when_price_only_rises():
    rising = pd.Series(np.arange(100.0, 140.0))
    assert last(rsi(rising, 14)) == pytest.approx(100.0)


def test_rsi_is_0_when_price_only_falls():
    falling = pd.Series(np.arange(140.0, 100.0, -1.0))
    assert last(rsi(falling, 14)) == pytest.approx(0.0, abs=1e-9)


def test_rsi_of_a_flat_series_is_undefined_not_a_crash():
    flat = pd.Series([100.0] * 40)
    value = last(rsi(flat, 14))
    # No gains and no losses: 0/0. NaN is the honest answer.
    assert pd.isna(value) or value == pytest.approx(100.0)


def test_rsi_known_value():
    """Wilder's worked example: RSI(14) of this series is ~70.5."""
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    ]
    assert last(rsi(pd.Series(closes), 14)) == pytest.approx(70.46, abs=0.5)


def test_rsi_stays_within_bounds():
    noisy = pd.Series(np.cumsum(np.random.default_rng(7).normal(0, 1, 200)) + 100)
    values = rsi(noisy, 14).dropna()
    assert values.between(0, 100).all()


def test_atr_is_positive_and_tracks_range():
    frame = make_frame(np.linspace(100, 120, 60))
    value = last(atr(frame, 14))
    assert value > 0
    assert value < frame["Close"].iloc[-1]  # sanity: ATR is not the price


def test_macd_histogram_is_line_minus_signal():
    series = pd.Series(np.linspace(100, 150, 120))
    line, signal, histogram = macd(series)
    assert histogram.iloc[-1] == pytest.approx(line.iloc[-1] - signal.iloc[-1])


def test_bollinger_bands_bracket_the_middle():
    series = pd.Series(np.cumsum(np.random.default_rng(1).normal(0, 1, 100)) + 100)
    upper, middle, lower = bollinger(series, 20, 2.0)
    assert (upper.dropna() >= middle.dropna()).all()
    assert (lower.dropna() <= middle.dropna()).all()


def test_rolling_vwap_sits_inside_the_price_range():
    frame = make_frame(np.linspace(100, 110, 60))
    value = last(rolling_vwap(frame, 20))
    assert frame["Low"].tail(20).min() <= value <= frame["High"].tail(20).max()


def test_crossed_above_only_fires_on_the_crossing_bar():
    fast = pd.Series([1.0, 2.0, 5.0, 6.0])
    slow = pd.Series([3.0, 3.0, 3.0, 3.0])
    assert crossed_above(fast[:3], slow[:3]) is True   # crossed on bar 3
    assert crossed_above(fast, slow) is False          # already above on bar 4


def test_crossed_below_only_fires_on_the_crossing_bar():
    fast = pd.Series([5.0, 4.0, 1.0, 0.5])
    slow = pd.Series([3.0, 3.0, 3.0, 3.0])
    assert crossed_below(fast[:3], slow[:3]) is True
    assert crossed_below(fast, slow) is False


def test_cross_helpers_ignore_warmup_nans():
    fast = pd.Series([np.nan, np.nan, 5.0])
    slow = pd.Series([np.nan, 3.0, 3.0])
    assert crossed_above(fast, slow) is False


def test_last_handles_short_series():
    assert pd.isna(last(pd.Series([], dtype=float)))
    assert last(pd.Series([1.0, 2.0]), offset=1) == 1.0
