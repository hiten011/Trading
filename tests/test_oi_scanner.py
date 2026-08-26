"""Every gate in the scanner, one at a time.

Each test starts from a contract that clears everything and breaks exactly
one thing, so a failure names the gate that changed behaviour.
"""

from dataclasses import replace
from datetime import date

import pytest

from custom.oi.config import OISettings
from custom.oi.models import Bias, Tier
from custom.oi.scanner import (
    build_pct_history,
    eligible_expiries,
    scan_session,
    score_alert,
    z_score,
)
from tests.factories import option_row, session_from_rows, underlying_context

TODAY = date(2026, 8, 12)
EXPIRY = date(2026, 8, 25)


@pytest.fixture
def settings():
    """Shipped defaults, with the environment ignored."""
    return OISettings(
        min_tier=Tier.STRONG,
        watch_pct=300.0, strong_pct=1000.0, extreme_pct=2000.0,
        min_oi_lots=500.0, min_prev_oi_lots=50.0, min_delta_oi_lots=250.0,
        min_volume_lots=100.0, min_notional_cr=1.0,
        max_moneyness_pct=10.0,
        min_days_to_expiry=2, max_days_to_expiry=45, max_expiries=2,
        min_share_of_symbol_oi=0.005,
        max_alerts=0, max_per_symbol=0,
    )


def scan_one(row, settings, **session_kwargs):
    session = session_from_rows([row], trade_date=TODAY, **session_kwargs)
    return scan_session(session, settings)


# ---------------------------------------------------------------------------
# The threshold itself
# ---------------------------------------------------------------------------

def test_a_contract_over_the_threshold_alerts(settings):
    # 1,000 -> 20,000 lots is +1900%, comfortably into the strong tier.
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000, lot_size=500)
    result = scan_one(row, settings)
    assert len(result.alerts) == 1
    assert result.alerts[0].tier is Tier.STRONG


def test_a_contract_under_the_threshold_does_not(settings):
    # +25%, but large enough in absolute terms to clear every floor first, so
    # the threshold really is the gate under test.
    row = option_row(oi_units=5_000_000, delta_oi_units=1_000_000, lot_size=500)
    result = scan_one(row, settings)
    assert result.alerts == []
    assert result.rejections["below_threshold"] == 1


@pytest.mark.parametrize(
    "prev_lots, now_lots, expected",
    [(1_000, 5_000, Tier.WATCH), (1_000, 12_000, Tier.STRONG), (1_000, 25_000, Tier.EXTREME)],
)
def test_tiers_band_the_percentage(prev_lots, now_lots, expected, settings):
    settings = replace(settings, min_tier=Tier.WATCH)
    row = option_row(
        oi_units=now_lots * 500,
        delta_oi_units=(now_lots - prev_lots) * 500,
        lot_size=500,
    )
    result = scan_one(row, settings)
    assert result.alerts[0].tier is expected


# ---------------------------------------------------------------------------
# Absolute floors
# ---------------------------------------------------------------------------

def test_a_huge_percentage_off_a_tiny_base_is_rejected(settings):
    """The brief's motivating case: 10 contracts becoming 110 is +1000% and
    means nothing. The previous-OI floor is what removes it."""
    row = option_row(
        oi_units=110 * 500, delta_oi_units=100 * 500, lot_size=500,
        volume_lots=5_000,
    )
    result = scan_one(row, settings)
    assert result.alerts == []
    assert result.rejections["oi_too_small"] == 1


def test_the_previous_oi_floor_rejects_a_thin_base_that_grew_large(settings):
    settings = replace(settings, min_oi_lots=0, min_prev_oi_lots=50.0)
    # 10 -> 3,000 lots: clears the current-OI floor, but started from nothing.
    row = option_row(oi_units=3_000 * 500, delta_oi_units=2_990 * 500, lot_size=500)
    result = scan_one(row, settings)
    assert result.alerts == []
    assert result.rejections["prev_oi_too_small"] == 1


def test_low_volume_contracts_are_rejected(settings):
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000, volume_lots=5)
    result = scan_one(row, settings)
    assert result.rejections["volume_too_small"] == 1


def test_low_notional_contracts_are_rejected(settings):
    """A lots floor alone is not comparable across a Rs.100 stock and a
    Rs.40,000 one, so traded rupee value is gated too."""
    settings = replace(settings, min_volume_lots=0, min_notional_cr=100.0)
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000, volume_lots=200)
    result = scan_one(row, settings)
    assert result.rejections["notional_too_small"] == 1


def test_a_small_absolute_oi_addition_is_rejected(settings):
    settings = replace(settings, min_oi_lots=0, min_prev_oi_lots=0, min_delta_oi_lots=50_000)
    # 19,000 lots added -- a real move, but under a 50,000-lot floor.
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000, lot_size=500)
    result = scan_one(row, settings)
    assert result.rejections["oi_add_too_small"] == 1


# ---------------------------------------------------------------------------
# Near the money
# ---------------------------------------------------------------------------

def test_deep_out_of_the_money_strikes_are_rejected(settings):
    """Far strikes swing enormous percentages on a handful of lots."""
    row = option_row(
        strike=200.0, underlying=100.0,
        oi_units=10_000_000, delta_oi_units=9_500_000,
    )
    result = scan_one(row, settings)
    assert result.rejections["not_near_the_money"] == 1


def test_a_strike_inside_the_moneyness_band_survives(settings):
    row = option_row(
        strike=105.0, underlying=100.0,
        oi_units=10_000_000, delta_oi_units=9_500_000,
    )
    assert len(scan_one(row, settings).alerts) == 1


# ---------------------------------------------------------------------------
# Expiry hygiene
# ---------------------------------------------------------------------------

def test_contracts_inside_the_expiry_window_are_rejected(settings):
    """Expiry-week open interest is dominated by mechanical unwinding."""
    row = option_row(
        trade_date=TODAY, expiry=date(2026, 8, 13),
        oi_units=10_000_000, delta_oi_units=9_500_000,
    )
    result = scan_one(row, settings)
    assert result.rejections["expiry_too_near"] == 1


def test_far_dated_contracts_are_rejected(settings):
    row = option_row(
        trade_date=TODAY, expiry=date(2027, 6, 24),
        oi_units=10_000_000, delta_oi_units=9_500_000,
    )
    result = scan_one(row, settings)
    assert result.rejections["expiry_too_far"] == 1


def test_only_the_nearest_expiries_are_scanned(settings):
    rows = [
        option_row(expiry=date(2026, 8, 25), oi_units=10_000_000, delta_oi_units=9_500_000),
        option_row(expiry=date(2026, 9, 29), oi_units=10_000_000, delta_oi_units=9_500_000),
        option_row(expiry=date(2026, 10, 27), oi_units=10_000_000, delta_oi_units=9_500_000),
    ]
    chosen = eligible_expiries(rows, replace(settings, max_expiries=2), TODAY)
    assert chosen["TESTCO"] == {date(2026, 8, 25), date(2026, 9, 29)}


def test_the_rollover_guard_drops_the_next_month_during_expiry_week():
    """Open interest migrating into the next series before a monthly expiry is
    position transfer, not fresh conviction."""
    settings = OISettings(suppress_rollover=True, rollover_window_days=5, max_expiries=2)
    rows = [
        option_row(expiry=date(2026, 8, 25)),
        option_row(expiry=date(2026, 9, 29)),
    ]
    chosen = eligible_expiries(rows, settings, date(2026, 8, 24))
    assert chosen["TESTCO"] == {date(2026, 8, 25)}


def test_the_rollover_guard_leaves_weekly_expiries_alone():
    """Index weeklies expire every week without a monthly rollover, so a near
    expiry alone must not suppress the following series."""
    settings = OISettings(suppress_rollover=True, rollover_window_days=5, max_expiries=2)
    rows = [
        option_row(symbol="NIFTY", expiry=date(2026, 9, 1)),
        option_row(symbol="NIFTY", expiry=date(2026, 9, 8)),
    ]
    chosen = eligible_expiries(rows, settings, date(2026, 8, 31))
    assert chosen["NIFTY"] == {date(2026, 9, 1), date(2026, 9, 8)}


def test_the_rollover_guard_can_be_switched_off():
    settings = OISettings(suppress_rollover=False, rollover_window_days=5, max_expiries=2)
    rows = [option_row(expiry=date(2026, 8, 25)), option_row(expiry=date(2026, 9, 29))]
    chosen = eligible_expiries(rows, settings, date(2026, 8, 24))
    assert chosen["TESTCO"] == {date(2026, 8, 25), date(2026, 9, 29)}


# ---------------------------------------------------------------------------
# Significance
# ---------------------------------------------------------------------------

def test_a_move_that_is_trivial_against_the_symbols_whole_book_is_rejected(settings):
    settings = replace(settings, min_share_of_symbol_oi=0.10)
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000)
    # The symbol's whole book is 1bn units, so a 9.5m addition is under 1%.
    contexts = {"TESTCO": underlying_context(
        total_call_oi=500_000_000.0, total_put_oi=500_000_000.0
    )}
    result = scan_one(row, settings, contexts=contexts)
    assert result.rejections["insignificant_vs_symbol"] == 1


def test_the_z_score_gate_rejects_a_contract_that_always_moves_like_this(settings):
    """1000% is an arbitrary constant. A contract whose OI routinely doubles
    should need a bigger move than one that never budges."""
    settings = replace(settings, min_z_score=3.0)
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000)
    # Noisy history centred near today's move: today is unremarkable for this
    # contract, so its z-score lands well under 3.
    history = {row.key: [1700.0, 1800.0, 1900.0, 2000.0, 2100.0] * 3}
    session = session_from_rows([row], trade_date=TODAY)
    result = scan_session(session, settings, history=history)
    assert result.rejections["z_below_threshold"] == 1


def test_the_z_score_gate_is_skipped_without_enough_history(settings):
    settings = replace(settings, min_z_score=3.0)
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000)
    session = session_from_rows([row], trade_date=TODAY)
    result = scan_session(session, settings, history={row.key: [100.0, 120.0]})
    assert len(result.alerts) == 1


def test_z_score_needs_a_minimum_sample():
    assert z_score(5.0, [1.0, 2.0], min_samples=8) != z_score(5.0, [1.0, 2.0], min_samples=8)


def test_z_score_measures_deviations_above_the_mean():
    assert z_score(20.0, [10.0] * 5 + [20.0] * 5, min_samples=4) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# New contracts
# ---------------------------------------------------------------------------

def test_a_brand_new_position_alerts_when_it_is_big_enough(settings):
    row = option_row(oi_units=5_000_000, delta_oi_units=5_000_000, lot_size=500)
    result = scan_one(row, settings)
    assert len(result.alerts) == 1
    assert result.alerts[0].row.is_new_contract
    assert "NEW" in result.alerts[0].headline


def test_new_contracts_can_be_excluded(settings):
    settings = replace(settings, include_new_contracts=False)
    row = option_row(oi_units=5_000_000, delta_oi_units=5_000_000)
    result = scan_one(row, settings)
    assert result.rejections["new_contract"] == 1


# ---------------------------------------------------------------------------
# Confirmation and universe
# ---------------------------------------------------------------------------

def test_futures_confirmation_rejects_a_contradicting_signal(settings):
    """A bullish option read against a futures book building shorts is a
    disagreement worth acting on -- by staying out."""
    settings = replace(settings, require_futures_confirmation=True)
    # Call being written -> bearish; futures price up on rising OI -> bullish.
    row = option_row(
        oi_units=10_000_000, delta_oi_units=9_500_000, close=5.0, prev_close=10.0
    )
    contexts = {
        "TESTCO": underlying_context(
            futures_delta_oi=50_000.0, futures_close=105.0, futures_prev_close=100.0
        )
    }
    result = scan_one(row, settings, contexts=contexts)
    assert result.rejections["futures_disagrees"] == 1


def test_futures_confirmation_passes_an_agreeing_signal(settings):
    settings = replace(settings, require_futures_confirmation=True)
    row = option_row(
        oi_units=10_000_000, delta_oi_units=9_500_000, close=5.0, prev_close=10.0
    )
    contexts = {
        "TESTCO": underlying_context(
            futures_delta_oi=50_000.0, futures_close=95.0, futures_prev_close=100.0
        )
    }
    result = scan_one(row, settings, contexts=contexts)
    assert len(result.alerts) == 1
    assert result.alerts[0].bias is Bias.BEARISH


def test_the_symbol_allow_list_is_honoured(settings):
    settings = replace(settings, symbols=["RELIANCE"])
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000)
    assert scan_one(row, settings).rejections["not_in_universe"] == 1


def test_indices_can_be_excluded(settings):
    settings = replace(settings, exclude_indices=True)
    row = option_row(symbol="NIFTY", oi_units=10_000_000, delta_oi_units=9_500_000)
    assert scan_one(row, settings).rejections["index"] == 1


# ---------------------------------------------------------------------------
# Ranking and caps
# ---------------------------------------------------------------------------

def test_one_busy_symbol_cannot_fill_the_whole_alert(settings):
    settings = replace(settings, max_per_symbol=2, max_alerts=10)
    rows = [
        option_row(strike=100.0 + index, oi_units=10_000_000, delta_oi_units=9_500_000)
        for index in range(6)
    ]
    result = scan_session(session_from_rows(rows, trade_date=TODAY), settings)
    assert result.total_hits == 6
    assert len(result.alerts) == 2


def test_alerts_are_capped_and_ranked_by_score(settings):
    settings = replace(settings, max_alerts=2, max_per_symbol=0)
    rows = [
        option_row(symbol=f"SYM{index}", strike=100.0,
                   oi_units=10_000_000 * (index + 1), delta_oi_units=9_500_000 * (index + 1))
        for index in range(4)
    ]
    result = scan_session(session_from_rows(rows, trade_date=TODAY), settings)
    assert len(result.alerts) == 2
    scores = [alert.score for alert in result.alerts]
    assert scores == sorted(scores, reverse=True)


def test_score_is_dominated_by_the_tier():
    """A strong hit on a modest base must never outrank an extreme one."""
    row = option_row()
    extreme = score_alert(row, Tier.EXTREME, share_of_symbol_oi=0.001, z=float("nan"))
    strong = score_alert(row, Tier.STRONG, share_of_symbol_oi=0.09, z=float("nan"))
    assert extreme > strong


# ---------------------------------------------------------------------------
# Context plumbing
# ---------------------------------------------------------------------------

def test_the_previous_session_fills_in_the_underlyings_own_move(settings):
    row = option_row(oi_units=10_000_000, delta_oi_units=9_500_000, underlying=110.0)
    today = session_from_rows([row], trade_date=TODAY)
    yesterday = session_from_rows(
        [option_row(underlying=100.0)], trade_date=date(2026, 8, 11)
    )
    result = scan_session(today, settings, prev_session=yesterday)
    assert result.alerts[0].context.spot_pct_change == pytest.approx(10.0)


def test_history_skips_contracts_that_had_no_prior_position():
    """An infinite percentage would poison the z-score's mean and stdev."""
    new = option_row(oi_units=500_000, delta_oi_units=500_000)
    established = option_row(strike=105.0, oi_units=500_000, delta_oi_units=100_000)
    history = build_pct_history([session_from_rows([new, established], trade_date=TODAY)])
    assert new.key not in history
    assert history[established.key] == [pytest.approx(25.0)]
