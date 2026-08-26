"""Rendering alerts for Telegram, and the config that drives them."""

from dataclasses import replace
from datetime import date, datetime

import pytest

from custom.oi.alerts import build_message, format_alert, to_frame, write_csv
from custom.oi.config import OISettings
from custom.oi.models import Bias, Buildup, OIAlert, OptionType, Tier
from custom.oi.scanner import ScanResult
from tests.factories import option_row, underlying_context

TODAY = date(2026, 8, 12)


def make_alert(
    tier=Tier.STRONG, bias=Bias.BULLISH, buildup=Buildup.SHORT_BUILDUP,
    symbol="RELIANCE", oi_pct_change=1340.0, **row_kwargs,
):
    row = option_row(symbol=symbol, trade_date=TODAY, **row_kwargs)
    return OIAlert(
        row=row, context=underlying_context(symbol=symbol, trade_date=TODAY, spot=row.underlying),
        tier=tier, buildup=buildup, bias=bias, score=100.0,
        oi_pct_change=oi_pct_change, share_of_symbol_oi=0.03,
    )


@pytest.fixture
def result():
    return ScanResult(trade_date=TODAY, scanned_contracts=35_472, scanned_symbols=214, total_hits=1)


@pytest.fixture
def settings():
    return OISettings()


# ---------------------------------------------------------------------------
# Headline
# ---------------------------------------------------------------------------

def test_the_headline_matches_the_format_the_brief_asked_for():
    alert = make_alert(strike=3000.0, option_type="CE", buildup=Buildup.LONG_BUILDUP)
    assert alert.headline == "RELIANCE 3000 CALL LONG BUILDUP OI +1,340%"


def test_a_new_contract_says_new_rather_than_an_infinite_percentage():
    alert = make_alert(oi_units=500_000, delta_oi_units=500_000)
    alert.oi_pct_change = float("inf")
    assert alert.headline.endswith("OI NEW")


def test_puts_are_labelled_put():
    assert " PUT " in make_alert(option_type="PE").headline


# ---------------------------------------------------------------------------
# Message body
# ---------------------------------------------------------------------------

def test_the_message_carries_the_numbers_that_justify_the_alert(result, settings):
    message = build_message([make_alert()], result, settings, as_of=datetime(2026, 8, 12, 18, 30))
    assert "RELIANCE" in message
    assert "of book" in message       # significance against the symbol
    assert "Vol" in message and "Cr" in message  # volume and notional
    assert "Spot" in message
    assert "12 Aug 2026" in message


def test_an_empty_scan_says_what_was_checked(result, settings):
    result.total_hits = 0
    message = build_message([], result, settings)
    assert "No contract cleared" in message
    assert "35,472" in message


def test_truncation_is_disclosed(result, settings):
    result.total_hits = 40
    message = build_message([make_alert()], result, settings)
    assert "top 1 of 40" in message


def test_cooldown_suppression_is_disclosed(result, settings):
    message = build_message([make_alert()], result, settings, suppressed=7)
    assert "7 repeat(s) held back" in message


def test_the_tier_breakdown_is_shown(result, settings):
    result.total_hits = 2
    message = build_message(
        [make_alert(tier=Tier.EXTREME), make_alert(tier=Tier.STRONG)], result, settings
    )
    assert "1 extreme" in message and "1 strong" in message


def test_html_in_a_symbol_name_is_escaped(result, settings):
    """Telegram parses the body as HTML; an unescaped '&' breaks the send."""
    message = build_message([make_alert(symbol="A&B")], result, settings)
    assert "A&amp;B" in message
    assert "<b>A&B" not in message


def test_the_message_says_it_is_not_advice(result, settings):
    assert "not a trade recommendation" in build_message([make_alert()], result, settings)


def test_the_price_basis_is_disclosed_in_the_block():
    """A reader has to know whether the price move is day-over-day or
    intraday, because the two mean different things."""
    established = format_alert(make_alert(oi_units=500_000, delta_oi_units=100_000))
    assert "vs prev close" in established
    fresh = format_alert(make_alert(oi_units=500_000, delta_oi_units=500_000))
    assert "intraday" in fresh


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def test_the_csv_carries_every_field_the_message_had_to_drop(tmp_path):
    path = write_csv([make_alert()], str(tmp_path), trade_date=TODAY)
    frame = __import__("pandas").read_csv(path)
    for column in ("Symbol", "Strike", "Type", "OI%", "Bias", "Tier", "Z", "FutBuildup", "PCR"):
        assert column in frame.columns


def test_no_csv_is_written_for_an_empty_result(tmp_path):
    assert write_csv([], str(tmp_path)) is None


def test_a_new_contract_exports_a_blank_percentage_not_infinity():
    """Infinity round-trips through CSV as the string 'inf' and breaks
    anything reading the file back."""
    alert = make_alert(oi_units=500_000, delta_oi_units=500_000)
    assert to_frame([alert])["OI%"].isna().all()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pct, expected",
    [(100, Tier.NONE), (300, Tier.WATCH), (999, Tier.WATCH),
     (1000, Tier.STRONG), (1999, Tier.STRONG), (2000, Tier.EXTREME)],
)
def test_tier_boundaries_are_inclusive(pct, expected, settings):
    assert settings.tier_for(pct) is expected


def test_a_new_contract_is_graded_strong_rather_than_extreme(settings):
    """Its percentage is undefined, not enormous; the absolute floors decide
    whether it matters."""
    assert settings.tier_for(float("inf"), is_new_contract=True) is Tier.STRONG


def test_settings_describe_masks_the_bot_token():
    settings = OISettings(secrets={"TOKEN": "8846506721:AAFtjMdNO1YFkP7abcdefghij"})
    described = settings.describe()
    assert "AAFtjMdNO1YFkP7abcdefghij" not in described
    assert "884650" in described


def test_custom_thresholds_are_respected():
    settings = OISettings(watch_pct=50.0, strong_pct=100.0, extreme_pct=200.0)
    assert settings.tier_for(150) is Tier.STRONG
    assert settings.tier_for(250) is Tier.EXTREME
