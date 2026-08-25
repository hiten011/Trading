"""
===============================================================================
 THIS IS THE FILE YOU EDIT.
===============================================================================

Write your own indicator inside ``evaluate`` below. It is called once per stock,
for every listed Indian stock, with that stock's daily candles. Return a
``Signal`` to raise a Telegram alert, or ``None`` to stay quiet.

Contract
--------
    evaluate(symbol: str, df: pandas.DataFrame) -> Signal | None

``df`` is a DataFrame indexed by date, oldest row first, newest row last, with
columns ``Open, High, Low, Close, Volume``. ``df.iloc[-1]`` is the most recent
candle. Roughly ``PKS_LOOKBACK_DAYS`` rows are provided (250 by default).

Anything you raise is caught and logged, so a bug in one stock will not stop
the scan -- check the container logs if a symbol goes missing.

Try it without spamming yourself::

    docker compose run --rm alerts --once --dry-run
    docker compose run --rm alerts --once --dry-run --symbols RELIANCE,TCS,INFY

The example below is a volume-backed momentum breakout. Replace the rules with
your own; keep the shape.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

# crossed_above and macd are imported for the worked examples at the bottom of
# this file, so uncommenting one just works.
from custom.strategies.base import (  # noqa: F401
    Signal,
    atr,
    crossed_above,
    ema,
    last,
    macd,
    rsi,
    sma,
)

# Shown in the Telegram alert header.
NAME = "Momentum breakout"
DESCRIPTION = "20d high breakout, in an EMA uptrend, on 2x average volume"

# -----------------------------------------------------------------------------
# Tunables. Keep your knobs up here so you can adjust without hunting through
# the logic below.
# -----------------------------------------------------------------------------
FAST_EMA = 20
SLOW_EMA = 50
RSI_PERIOD = 14
RSI_FLOOR = 55.0          # below this the move has no strength behind it
BREAKOUT_LOOKBACK = 20    # close must be the highest in this many candles
VOLUME_MULTIPLE = 2.0     # today's volume vs its 20-day average
VOLUME_WINDOW = 20
ATR_PERIOD = 14
MAX_ATR_EXTENSION = 4.0   # how far above the fast EMA we will still buy, in ATRs
MIN_CANDLES = SLOW_EMA + 10


def evaluate(symbol: str, df: pd.DataFrame) -> Optional[Signal]:
    """Return a Signal when ``symbol`` matches, otherwise None."""

    # 0) Enough history to compute the slow EMA?
    if len(df) < MIN_CANDLES:
        return None

    close = df["Close"]
    volume = df["Volume"]

    # 1) Compute the indicators.
    fast = ema(close, FAST_EMA)
    slow = ema(close, SLOW_EMA)
    strength = rsi(close, RSI_PERIOD)
    average_volume = sma(volume, VOLUME_WINDOW)
    volatility = atr(df, ATR_PERIOD)

    last_close = last(close)
    last_fast = last(fast)
    last_slow = last(slow)
    last_rsi = last(strength)
    last_volume = last(volume)
    last_average_volume = last(average_volume)
    last_atr = last(volatility)

    # Any NaN means an indicator has not warmed up yet.
    values = [last_close, last_fast, last_slow, last_rsi, last_volume, last_average_volume, last_atr]
    if any(pd.isna(value) for value in values) or last_average_volume <= 0 or last_atr <= 0:
        return None

    # 2) The rules. Every one must hold.
    in_uptrend = last_close > last_fast > last_slow
    has_strength = last_rsi >= RSI_FLOOR

    # Highest close of the last N candles, today excluded from its own window.
    prior_high = float(close.iloc[-(BREAKOUT_LOOKBACK + 1) : -1].max())
    is_breakout = last_close > prior_high

    volume_ratio = last_volume / last_average_volume
    has_volume = volume_ratio >= VOLUME_MULTIPLE

    # Don't chase: a stock 6 ATRs above its 20 EMA has already made its move.
    extension = (last_close - last_fast) / last_atr
    is_sane_entry = extension <= MAX_ATR_EXTENSION

    if not (in_uptrend and has_strength and is_breakout and has_volume and is_sane_entry):
        return None

    # 3) Build the alert. `extras` become columns in the Telegram table;
    #    `score` ranks hits so the strongest survive the PKS_MAX_ALERTS cap.
    return Signal(
        symbol=symbol,
        direction="BUY",
        price=last_close,
        reason=f"{BREAKOUT_LOOKBACK}d breakout on {volume_ratio:.1f}x volume",
        score=volume_ratio,
        extras={
            "RSI": last_rsi,
            "Vol x": volume_ratio,
            f"{BREAKOUT_LOOKBACK}d High": prior_high,
            "SL": last_close - 1.5 * last_atr,
        },
    )


# -----------------------------------------------------------------------------
# Other patterns you might want. Copy a block into evaluate() above.
# -----------------------------------------------------------------------------
#
# MACD bullish crossover:
#     macd_line, signal_line, _ = macd(close)
#     if crossed_above(macd_line, signal_line):
#         return Signal(symbol, "BUY", last_close, "MACD crossed up")
#
# Oversold bounce (a SELL/short screen works the same way, direction="SELL"):
#     if last(rsi(close, 14)) < 30:
#         return Signal(symbol, "BUY", last_close, "RSI oversold")
#
# Using TA-Lib instead of the pandas helpers (it is installed in the image):
#     import talib
#     adx = talib.ADX(df["High"].values, df["Low"].values, df["Close"].values, timeperiod=14)
#     if adx[-1] > 25:
#         ...
#
# Gap up on the open:
#     if df["Open"].iloc[-1] > df["High"].iloc[-2] * 1.02:
#         return Signal(symbol, "BUY", last_close, "Gapped up 2%")
