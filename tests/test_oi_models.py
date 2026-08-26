"""The value types: OI arithmetic, unit handling, and direction classification."""

import math
from datetime import date

import pytest

from custom.oi.models import (
    Bias,
    Buildup,
    ContractKey,
    OptionType,
    Tier,
    classify_buildup,
    option_bias,
)
from tests.factories import option_row


# ---------------------------------------------------------------------------
# OI arithmetic
# ---------------------------------------------------------------------------

def test_previous_oi_is_todays_oi_minus_the_reported_change():
    """The whole scanner rests on this identity holding in NSE's own files.

    Verified against the live archive: it reproduces the prior session's
    OpnIntrst exactly on 100% of the ~35k contracts common to two consecutive
    bhavcopies, which is why one file is enough to compute a daily change.
    """
    row = option_row(oi_units=500_000, delta_oi_units=450_000)
    assert row.prev_oi_units == 50_000


def test_oi_percent_change_matches_the_pine_formula():
    row = option_row(oi_units=500_000, delta_oi_units=450_000)
    assert row.oi_pct_change == pytest.approx((500_000 - 50_000) / 50_000 * 100)


def test_oi_percent_change_is_infinite_for_a_contract_with_no_prior_position():
    """The Pine original divided by zero here and its guard returned 0.0,
    silently discarding every contract that went from nothing to something."""
    row = option_row(oi_units=500_000, delta_oi_units=500_000)
    assert row.is_new_contract
    assert math.isinf(row.oi_pct_change)


def test_a_contract_with_no_open_interest_at_all_reports_no_change():
    row = option_row(oi_units=0, delta_oi_units=0)
    assert row.oi_pct_change == 0.0


def test_open_interest_is_normalised_from_units_into_lots():
    """Bhavcopy quotes OI in shares, so a raw floor would compare a stock with
    a 71,475 lot against one with a 500 lot and exclude the wrong one."""
    row = option_row(oi_units=500_000, lot_size=500)
    assert row.oi_lots == 1_000
    big_lot = option_row(oi_units=500_000, lot_size=71_475)
    assert big_lot.oi_lots == pytest.approx(6.995, abs=0.01)


def test_notional_uses_lot_size_because_volume_is_quoted_in_lots():
    row = option_row(volume_lots=1_000, lot_size=500, underlying=100.0)
    assert row.notional == pytest.approx(1_000 * 500 * 100.0)


def test_moneyness_is_symmetric_around_spot():
    assert option_row(strike=110.0, underlying=100.0).moneyness_pct == pytest.approx(10.0)
    assert option_row(strike=90.0, underlying=100.0).moneyness_pct == pytest.approx(10.0)


def test_days_to_expiry_counts_from_the_session():
    row = option_row(trade_date=date(2026, 8, 12), expiry=date(2026, 8, 25))
    assert row.days_to_expiry() == 13


# ---------------------------------------------------------------------------
# Price reference
# ---------------------------------------------------------------------------

def test_price_change_uses_the_previous_close_for_an_established_contract():
    row = option_row(oi_units=500_000, delta_oi_units=100_000, close=10.0, prev_close=5.0)
    assert row.price_basis == "prev_close"
    assert row.price_pct_change == pytest.approx(100.0)


def test_price_change_falls_back_to_the_open_when_there_was_no_prior_position():
    """A listed-but-dormant strike carries a stale theoretical previous close.

    GODREJCP's 930 call sat at a carried-forward 103.25 with zero open
    interest; when the underlying gapped 11% lower it printed 14.20. Read
    against that stale number it looks like an -86% collapse in the option,
    which would be classified as heavy writing. Against the day's own open
    (25.00 -> 14.20) it is a real, and much smaller, intraday fall.
    """
    row = option_row(
        oi_units=500_000, delta_oi_units=500_000,
        close=14.20, prev_close=103.25, open_price=25.00,
    )
    assert row.price_basis == "open"
    assert row.price_pct_change == pytest.approx(-43.2, abs=0.1)


def test_price_change_is_zero_when_neither_reference_is_usable():
    row = option_row(oi_units=500_000, delta_oi_units=500_000, open_price=0.0, prev_close=0.0)
    assert row.price_basis == "none"
    assert row.price_pct_change == 0.0


# ---------------------------------------------------------------------------
# Buildup / bias
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "delta_oi, price_change, expected",
    [
        (100.0, 5.0, Buildup.LONG_BUILDUP),
        (100.0, -5.0, Buildup.SHORT_BUILDUP),
        (-100.0, 5.0, Buildup.SHORT_COVERING),
        (-100.0, -5.0, Buildup.LONG_UNWINDING),
        (0.0, 5.0, Buildup.UNKNOWN),
        (100.0, 0.0, Buildup.UNKNOWN),
    ],
)
def test_the_four_box_buildup_table(delta_oi, price_change, expected):
    assert classify_buildup(delta_oi, price_change) is expected


@pytest.mark.parametrize(
    "option_type, buildup, expected",
    [
        # Buying calls and writing puts are both bullish on the underlying.
        (OptionType.CALL, Buildup.LONG_BUILDUP, Bias.BULLISH),
        (OptionType.PUT, Buildup.SHORT_BUILDUP, Bias.BULLISH),
        # Writing calls and buying puts are both bearish.
        (OptionType.CALL, Buildup.SHORT_BUILDUP, Bias.BEARISH),
        (OptionType.PUT, Buildup.LONG_BUILDUP, Bias.BEARISH),
        # Unwinding mirrors the buildup it undoes.
        (OptionType.CALL, Buildup.SHORT_COVERING, Bias.BULLISH),
        (OptionType.PUT, Buildup.SHORT_COVERING, Bias.BEARISH),
        (OptionType.CALL, Buildup.LONG_UNWINDING, Bias.BEARISH),
        (OptionType.PUT, Buildup.LONG_UNWINDING, Bias.BULLISH),
        (OptionType.CALL, Buildup.UNKNOWN, Bias.NEUTRAL),
    ],
)
def test_option_bias_folds_in_which_side_the_contract_is(option_type, buildup, expected):
    """The four-box table is written for futures. On an option, the same
    'short buildup' means opposite things for a call and a put."""
    assert option_bias(option_type, buildup) is expected


def test_tiers_are_ordered_weakest_to_strongest():
    assert Tier.NONE.rank < Tier.WATCH.rank < Tier.STRONG.rank < Tier.EXTREME.rank


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_contract_key_is_hashable_and_stable_across_sessions():
    """Cooldown state and the z-score history both key on this."""
    first = ContractKey("RELIANCE", date(2026, 9, 29), 1310.0, OptionType.PUT)
    second = ContractKey("RELIANCE", date(2026, 9, 29), 1310.0, OptionType.PUT)
    assert first == second and hash(first) == hash(second)
    assert len({first, second}) == 1


def test_contract_key_renders_a_readable_label():
    key = ContractKey("RELIANCE", date(2026, 9, 29), 1310.0, OptionType.PUT)
    assert str(key) == "RELIANCE 1310 PE 29SEP26"
    assert key.slug == "RELIANCE|2026-09-29|1310|PE"
