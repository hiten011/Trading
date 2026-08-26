"""The backtest's own arithmetic: entry timing, signing, and the benchmark.

If these are wrong the whole exercise reports a confident number about
nothing, so the look-ahead rule in particular is pinned down here.
"""

from datetime import date

import pytest

from custom.oi.backtest import (
    AlertOutcome,
    SpotSeries,
    base_rate,
    breakdown,
    score_alerts,
    summarise,
)
from custom.oi.models import Bias, Buildup, OIAlert, Tier
from tests.factories import option_row, session_from_rows, underlying_context

DAYS = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)]


def series(prices_by_day, symbol="TESTCO"):
    spots = SpotSeries()
    for day, price in zip(DAYS, prices_by_day):
        spots.add_session(
            session_from_rows(
                [option_row(symbol=symbol, trade_date=day, underlying=price)],
                trade_date=day,
            )
        )
    return spots


def make_alert(bias=Bias.BULLISH, trade_date=DAYS[0], symbol="TESTCO", tier=Tier.STRONG):
    row = option_row(symbol=symbol, trade_date=trade_date, underlying=100.0)
    return OIAlert(
        row=row, context=underlying_context(symbol=symbol, trade_date=trade_date),
        tier=tier, buildup=Buildup.LONG_BUILDUP, bias=bias,
        score=1.0, oi_pct_change=1200.0, share_of_symbol_oi=0.02,
    )


# ---------------------------------------------------------------------------
# Price series
# ---------------------------------------------------------------------------

def test_session_offset_counts_trading_days_not_calendar_days():
    spots = series([100, 101, 102, 103, 104])
    assert spots.session_offset(DAYS[0], 1) == DAYS[1]
    assert spots.session_offset(DAYS[0], 3) == DAYS[3]


def test_session_offset_past_the_end_of_the_data_is_none():
    spots = series([100, 101, 102, 103, 104])
    assert spots.session_offset(DAYS[4], 1) is None


def test_forward_return_measures_the_underlying_move():
    spots = series([100, 100, 110, 103, 104])
    assert spots.forward_return("TESTCO", DAYS[1], 1) == pytest.approx(10.0)


def test_forward_return_is_none_when_the_exit_is_unpriced():
    spots = series([100, 101, 102, 103, 104])
    assert spots.forward_return("TESTCO", DAYS[4], 1) is None


# ---------------------------------------------------------------------------
# Entry timing -- the look-ahead rule
# ---------------------------------------------------------------------------

def test_entry_is_the_session_after_the_signal():
    """NSE publishes the bhavcopy after the close, so a signal from session D
    cannot be traded during session D. Entering at D's close would be
    look-ahead bias, and would flatter the result badly: the OI move and the
    price move that produced it happen on the same day.
    """
    spots = series([100, 120, 132, 132, 132])
    outcomes = score_alerts([make_alert(trade_date=DAYS[0])], spots, horizons=[1])
    outcome = outcomes[0]
    assert outcome.entry_date == DAYS[1]
    assert outcome.entry_price == 120  # not 100
    assert outcome.returns[1] == pytest.approx(10.0)  # 120 -> 132, not 100 -> 132


def test_the_signal_day_move_is_recorded_separately_as_the_reaction():
    """Informative, but only partly capturable -- so it is never mixed into
    the tradeable horizons."""
    spots = series([100, 120, 132, 132, 132])
    outcome = score_alerts([make_alert(trade_date=DAYS[0])], spots, horizons=[1])[0]
    assert outcome.reaction_return == pytest.approx(20.0)
    assert 0 not in outcome.returns


def test_an_alert_with_no_following_session_scores_nothing():
    spots = series([100, 101, 102, 103, 104])
    outcome = score_alerts([make_alert(trade_date=DAYS[4])], spots, horizons=[1])[0]
    assert outcome.returns == {}
    assert outcome.entry_date is None


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def test_a_bullish_alert_scores_positive_when_the_underlying_rises():
    spots = series([100, 100, 110, 110, 110])
    outcome = score_alerts([make_alert(Bias.BULLISH)], spots, horizons=[1])[0]
    assert outcome.returns[1] == pytest.approx(10.0)


def test_a_bearish_alert_scores_positive_when_the_underlying_falls():
    """Raw returns are useless for scoring a signal that calls both
    directions, so every return is signed by the alert's own bias."""
    spots = series([100, 100, 90, 90, 90])
    outcome = score_alerts([make_alert(Bias.BEARISH)], spots, horizons=[1])[0]
    assert outcome.returns[1] == pytest.approx(10.0)
    assert outcome.raw_returns[1] == pytest.approx(-10.0)


def test_a_bearish_alert_scores_negative_when_the_underlying_rises():
    spots = series([100, 100, 110, 110, 110])
    outcome = score_alerts([make_alert(Bias.BEARISH)], spots, horizons=[1])[0]
    assert outcome.returns[1] == pytest.approx(-10.0)


def test_a_neutral_alert_contributes_nothing_either_way():
    spots = series([100, 100, 110, 110, 110])
    outcome = score_alerts([make_alert(Bias.NEUTRAL)], spots, horizons=[1])[0]
    assert outcome.returns[1] == 0.0


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

def test_the_base_rate_is_signed_by_the_same_bullish_bearish_mix():
    """Otherwise a mostly-bearish signal in a falling market looks brilliant
    when it has only matched the drift."""
    spots = series([100, 100, 110, 110, 110])
    all_long = base_rate(spots, [1], [DAYS[1]], {1: 10, -1: 0})
    all_short = base_rate(spots, [1], [DAYS[1]], {1: 0, -1: 10})
    assert all_long[1][0] == pytest.approx(10.0)
    assert all_short[1][0] == pytest.approx(-10.0)


def test_a_balanced_mix_has_a_base_rate_near_zero():
    spots = series([100, 100, 110, 110, 110])
    assert base_rate(spots, [1], [DAYS[1]], {1: 5, -1: 5})[1][0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def test_summarise_reports_the_edge_over_the_benchmark():
    spots = series([100, 100, 110, 110, 110])
    outcomes = score_alerts([make_alert(Bias.BULLISH)], spots, horizons=[1])
    result = summarise(outcomes, spots, "test", DAYS[0], DAYS[4], sessions=5, horizons=[1])
    stats = result.stats[1]
    assert stats.count == 1
    assert stats.mean_return == pytest.approx(10.0)
    # Only one symbol exists here, so the benchmark equals the alert.
    assert stats.edge == pytest.approx(0.0)


def test_summarise_counts_alerts_per_session():
    spots = series([100, 100, 110, 110, 110])
    outcomes = score_alerts(
        [make_alert(trade_date=DAYS[0]), make_alert(trade_date=DAYS[0])], spots, horizons=[1]
    )
    result = summarise(outcomes, spots, "test", DAYS[0], DAYS[4], sessions=4, horizons=[1])
    assert result.alerts_per_session == pytest.approx(0.5)
    assert result.sessions_with_alerts == 1


def test_the_t_statistic_is_undefined_for_a_single_observation():
    spots = series([100, 100, 110, 110, 110])
    outcomes = score_alerts([make_alert()], spots, horizons=[1])
    stats = summarise(outcomes, spots, "t", DAYS[0], DAYS[4], 5, [1]).stats[1]
    assert stats.t_statistic != stats.t_statistic  # NaN


def test_breakdown_groups_by_an_attribute():
    spots = series([100, 100, 110, 110, 110])
    outcomes = score_alerts(
        [make_alert(tier=Tier.STRONG), make_alert(tier=Tier.EXTREME)], spots, horizons=[1]
    )
    rows = dict((key, count) for key, count, _hit, _mean in breakdown(outcomes, "tier", 1))
    assert rows == {"STRONG": 1, "EXTREME": 1}


def test_summarise_handles_a_run_that_produced_nothing():
    spots = series([100, 101, 102, 103, 104])
    result = summarise([], spots, "empty", DAYS[0], DAYS[4], sessions=5, horizons=[1])
    assert result.stats == {}
    assert "No scored alerts" in result.describe()
