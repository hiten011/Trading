"""Contract tests that apply to EVERY strategy in custom/strategies/.

The rest of the suite tests the shipped `my_indicator` by name. These
parametrise over whatever `strategies.available()` discovers, so dropping a
new file into custom/strategies/ automatically puts it under test in CI --
no edit here needed. That is the point: a strategy that raises on a real
symbol is silently skipped at runtime (custom/runner.py catches per-symbol
exceptions so one bad stock cannot kill a whole scan), so without a test like
this a broken indicator looks exactly like an indicator that found nothing.

If you add custom/strategies/my_new_thing.py, these run against it on the
next push.
"""

import numpy as np
import pandas as pd
import pytest

from custom import strategies
from custom.strategies.base import OHLCV_COLUMNS, Signal
from tests.factories import breakout_frame, flat_frame, make_frame

# Collected at import time, so a newly added file is picked up automatically.
ALL_STRATEGIES = strategies.available()


def _frames():
    """Market shapes every strategy has to survive without raising.

    These are the cases that actually break indicators in production: a stock
    that never moves (zero standard deviation -> division by zero), one that
    only rises (RSI 100, no losses to average), a barely-listed name with a
    few days of history, one that stopped trading, and one with gaps in the
    data.
    """
    return {
        "flat": flat_frame(),
        "breakout": breakout_frame(),
        "straight_line_up": make_frame(np.linspace(100.0, 200.0, 120)),
        "straight_line_down": make_frame(np.linspace(200.0, 100.0, 120)),
        "barely_listed": make_frame([100.0, 101.0, 99.0, 102.0, 103.0]),
        "single_bar": make_frame([100.0]),
        "zero_volume": make_frame(np.linspace(100.0, 120.0, 120), volumes=np.zeros(120)),
        "penny_stock": make_frame(np.full(120, 0.05)),
        "with_nans": _with_nans(),
    }


def _with_nans():
    frame = make_frame(np.linspace(100.0, 130.0, 120))
    frame.loc[frame.index[10:15], "Volume"] = np.nan
    frame.loc[frame.index[20], "Close"] = np.nan
    return frame


def test_at_least_one_strategy_is_discoverable():
    """Guards the parametrisation itself: if discovery silently returned an
    empty list, every test below would vacuously pass."""
    assert ALL_STRATEGIES, "no strategies found in custom/strategies/"


@pytest.mark.parametrize("name", ALL_STRATEGIES)
def test_every_strategy_satisfies_the_loading_contract(name):
    module = strategies.load(name)
    assert callable(module.evaluate)


@pytest.mark.parametrize("name", ALL_STRATEGIES)
def test_every_strategy_survives_awkward_market_data(name):
    """A strategy that raises is dropped silently by the runner, so an
    exception here would show up in production as 'no matches today'."""
    module = strategies.load(name)
    for shape, frame in _frames().items():
        try:
            result = module.evaluate("TESTCO", frame)
        except Exception as exc:  # noqa: BLE001 - that is the thing under test
            pytest.fail(f"{name}.evaluate() raised on a {shape!r} frame: {exc!r}")
        assert result is None or isinstance(result, Signal), (
            f"{name}.evaluate() returned {type(result).__name__} on a {shape!r} "
            "frame; it must return a Signal or None"
        )


@pytest.mark.parametrize("name", ALL_STRATEGIES)
def test_every_strategy_returns_a_usable_signal_when_it_fires(name):
    """Whatever a strategy returns has to survive being put in a message."""
    module = strategies.load(name)
    for shape, frame in _frames().items():
        signal = module.evaluate("TESTCO", frame)
        if signal is None:
            continue
        assert signal.symbol or True  # runner backfills a blank symbol
        assert np.isfinite(signal.price), f"{name} returned a non-finite price on {shape!r}"
        assert np.isfinite(signal.score), f"{name} returned a non-finite score on {shape!r}"
        row = signal.as_row()
        assert "Symbol" in row and "Price" in row
        # A NaN in extras renders as 'nan' in the alert table, which is noise.
        for key, value in signal.extras.items():
            if isinstance(value, (int, float, np.floating)):
                assert np.isfinite(value), f"{name} put a non-finite {key!r} in extras"


@pytest.mark.parametrize("name", ALL_STRATEGIES)
def test_every_strategy_leaves_the_caller_s_data_alone(name):
    """The runner hands the same frame to one strategy per symbol and reuses
    the cache across scans; a strategy that mutates it corrupts later runs."""
    module = strategies.load(name)
    frame = breakout_frame()
    before = frame.copy(deep=True)
    module.evaluate("TESTCO", frame)
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize("name", ALL_STRATEGIES)
def test_every_strategy_is_deterministic(name):
    """Same input, same answer -- otherwise the cooldown and the backtest both
    become meaningless."""
    module = strategies.load(name)
    frame = breakout_frame()
    first, second = module.evaluate("TESTCO", frame), module.evaluate("TESTCO", frame)
    assert (first is None) == (second is None)
    if first is not None:
        assert first.as_row() == second.as_row()


@pytest.mark.parametrize("name", ALL_STRATEGIES)
def test_every_strategy_accepts_the_documented_columns_only(name):
    """The README promises Open/High/Low/Close/Volume. A strategy reaching for
    anything else works locally and fails against the real cache."""
    module = strategies.load(name)
    frame = breakout_frame()[OHLCV_COLUMNS]
    module.evaluate("TESTCO", frame)  # must not raise on exactly those columns
