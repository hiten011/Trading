"""The shipped example indicator, and the strategy-loading contract."""

import numpy as np
import pytest

from custom import strategies
from custom.strategies import my_indicator
from custom.strategies.base import Signal
from tests.factories import breakout_frame, flat_frame, make_frame


def test_example_indicator_fires_on_a_volume_backed_breakout():
    signal = my_indicator.evaluate("TESTCO", breakout_frame())
    assert isinstance(signal, Signal)
    assert signal.symbol == "TESTCO"
    assert signal.direction == "BUY"
    assert signal.price > 0
    assert "Vol x" in signal.extras
    assert signal.score >= my_indicator.VOLUME_MULTIPLE


def test_example_indicator_stays_quiet_on_a_flat_stock():
    assert my_indicator.evaluate("BORING", flat_frame()) is None


def test_example_indicator_needs_volume_confirmation():
    frame = breakout_frame()
    frame.loc[frame.index[-1], "Volume"] = 1_000_000.0  # breakout, but no volume
    assert my_indicator.evaluate("NOVOL", frame) is None


def test_example_indicator_needs_a_new_high():
    frame = breakout_frame()
    # Same volume spike, but today closes below the recent range.
    frame.loc[frame.index[-1], "Close"] = float(frame["Close"].iloc[-30:-1].min()) * 0.99
    assert my_indicator.evaluate("NOBREAK", frame) is None


def test_example_indicator_refuses_to_chase_an_extended_stock():
    frame = breakout_frame()
    # A vertical +50% candle: still a breakout on volume, but far too extended
    # above the fast EMA to be a sane entry.
    frame.loc[frame.index[-1], "Close"] = float(frame["Close"].iloc[-2]) * 1.5
    frame.loc[frame.index[-1], "High"] = float(frame["Close"].iloc[-1]) * 1.01
    assert my_indicator.evaluate("EXTENDED", frame) is None


def test_example_indicator_reports_a_stop_loss_below_the_price():
    signal = my_indicator.evaluate("TESTCO", breakout_frame())
    assert 0 < signal.extras["SL"] < signal.price


def test_example_indicator_needs_enough_history():
    assert my_indicator.evaluate("NEWLISTING", make_frame(np.linspace(100, 120, 20))) is None


def test_example_indicator_handles_a_zero_volume_stock():
    frame = breakout_frame()
    frame["Volume"] = 0.0
    assert my_indicator.evaluate("SUSPENDED", frame) is None


def test_signal_as_row_rounds_and_keeps_column_order():
    signal = Signal(
        symbol="RELIANCE",
        direction="BUY",
        price=2345.6789,
        reason="because",
        extras={"RSI": 61.23456, "Note": "text"},
    )
    row = signal.as_row()
    assert list(row) == ["Symbol", "Signal", "Price", "RSI", "Note", "Why"]
    assert row["Price"] == 2345.68
    assert row["RSI"] == 61.23
    assert row["Note"] == "text"


# --- loader ---------------------------------------------------------------

def test_the_example_strategy_is_discoverable():
    assert "my_indicator" in strategies.available()


def test_base_is_not_offered_as_a_strategy():
    assert "base" not in strategies.available()


def test_loading_the_example_strategy_works():
    module = strategies.load("my_indicator")
    assert callable(module.evaluate)
    assert module.NAME


def test_loading_an_unknown_strategy_says_what_is_available():
    with pytest.raises(strategies.StrategyError, match="my_indicator"):
        strategies.load("does_not_exist")


def test_loading_nothing_is_an_error():
    with pytest.raises(strategies.StrategyError, match="No strategy configured"):
        strategies.load("")


def test_a_module_without_evaluate_is_rejected(fake_strategies):
    with pytest.raises(strategies.StrategyError, match="evaluate"):
        strategies.load("no_evaluate")
