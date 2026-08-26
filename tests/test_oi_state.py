"""Cooldown and de-duplication: an alert fires once per crossing."""

import time
from datetime import date

import pytest

from custom.oi.models import Bias, Buildup, ContractKey, OIAlert, OptionType, Tier
from custom.oi.state import AlertState
from tests.factories import option_row, underlying_context

KEY = ContractKey("RELIANCE", date(2026, 9, 29), 1310.0, OptionType.PUT)
TRADE_DATE = date(2026, 8, 25)


def make_alert(tier=Tier.STRONG, key=KEY):
    row = option_row(
        symbol=key.symbol, expiry=key.expiry, strike=key.strike,
        option_type=key.option_type.value, trade_date=TRADE_DATE,
    )
    return OIAlert(
        row=row, context=underlying_context(symbol=key.symbol, trade_date=TRADE_DATE),
        tier=tier, buildup=Buildup.SHORT_BUILDUP, bias=Bias.BULLISH,
        score=100.0, oi_pct_change=1340.0,
    )


@pytest.fixture
def state():
    store = AlertState(":memory:")
    yield store
    store.close()


def test_a_contract_alerts_the_first_time(state):
    assert state.should_alert(KEY, TRADE_DATE, Tier.STRONG, cooldown_hours=12)


def test_the_same_contract_does_not_re_alert_inside_the_cooldown(state):
    """Without this the scanner re-sends everything still above the threshold
    on every cycle."""
    now = time.time()
    state.record(make_alert(), now=now)
    assert not state.should_alert(KEY, TRADE_DATE, Tier.STRONG, 12, now=now + 3600)


def test_the_contract_alerts_again_once_the_cooldown_expires(state):
    now = time.time()
    state.record(make_alert(), now=now)
    assert state.should_alert(KEY, TRADE_DATE, Tier.STRONG, 12, now=now + 13 * 3600)


def test_escalating_to_a_higher_tier_bypasses_the_cooldown(state):
    """A move from 1000% to 2500% is new information, not a repeat."""
    now = time.time()
    state.record(make_alert(Tier.STRONG), now=now)
    assert state.should_alert(KEY, TRADE_DATE, Tier.EXTREME, 12, now=now + 60)


def test_dropping_back_to_a_lower_tier_does_not_re_alert(state):
    now = time.time()
    state.record(make_alert(Tier.EXTREME), now=now)
    assert not state.should_alert(KEY, TRADE_DATE, Tier.STRONG, 12, now=now + 60)


def test_a_zero_cooldown_always_allows_the_alert(state):
    now = time.time()
    state.record(make_alert(), now=now)
    assert state.should_alert(KEY, TRADE_DATE, Tier.STRONG, 0, now=now + 1)


def test_a_different_contract_is_tracked_separately(state):
    other = ContractKey("RELIANCE", date(2026, 9, 29), 1320.0, OptionType.PUT)
    now = time.time()
    state.record(make_alert(), now=now)
    assert state.should_alert(other, TRADE_DATE, Tier.STRONG, 12, now=now + 60)


def test_filter_new_keeps_fresh_alerts_and_drops_repeats(state):
    now = time.time()
    first = make_alert()
    second = make_alert(key=ContractKey("TCS", date(2026, 9, 29), 3000.0, OptionType.CALL))
    state.record(first, now=now)
    kept = state.filter_new([first, second], cooldown_hours=12, now=now + 60)
    assert [alert.symbol for alert in kept] == ["TCS"]


def test_state_survives_a_restart(tmp_path):
    """The point of SQLite over an in-memory set: a container restart must not
    replay every alert already sent."""
    path = str(tmp_path / "oi_state.sqlite")
    now = time.time()
    with AlertState(path) as store:
        store.record(make_alert(), now=now)
    with AlertState(path) as reopened:
        assert not reopened.should_alert(KEY, TRADE_DATE, Tier.STRONG, 12, now=now + 60)


def test_recording_the_same_contract_twice_updates_rather_than_duplicates(state):
    now = time.time()
    state.record(make_alert(Tier.STRONG), now=now)
    state.record(make_alert(Tier.EXTREME), now=now + 60)
    rank, fired_at = state.last_alert(KEY, TRADE_DATE)
    assert rank == Tier.EXTREME.rank
    assert fired_at == pytest.approx(now + 60)


def test_an_infinite_percentage_is_stored_as_null(state):
    """New contracts have an undefined percentage; SQLite cannot hold inf."""
    alert = make_alert()
    alert.oi_pct_change = float("inf")
    state.record(alert)
    assert state.last_alert(KEY, TRADE_DATE) is not None


def test_old_records_are_purged(state):
    now = time.time()
    state.record(make_alert(), now=now)
    assert state.purge_older_than(days=30, now=now + 31 * 86400) == 1
    assert state.should_alert(KEY, TRADE_DATE, Tier.STRONG, 12, now=now + 31 * 86400)


def test_purge_leaves_recent_records_alone(state):
    now = time.time()
    state.record(make_alert(), now=now)
    assert state.purge_older_than(days=30, now=now + 86400) == 0
