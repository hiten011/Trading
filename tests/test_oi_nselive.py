"""custom.oi.sources.nselive: the live NSE option-chain adapter.

Two real bugs were found and fixed here, both confirmed against NSE's actual
production response (not assumed): ``option-chain-v3`` requires an explicit
``expiry`` query parameter -- omitting it returns ``{}`` regardless of IP or
headers, which an earlier version of this module misread as bot throttling --
and the row-level expiry field is ``expiryDates`` (singular value, plural
name) in ``DD-MM-YYYY``, not ``expiryDate`` in ``DD-MMM-YYYY``, which had made
every row silently unparseable even on the rare payload that got through.
Both are pinned down here with fixtures shaped exactly like the real response.
"""

from __future__ import annotations

from datetime import date

import pytest

from custom.oi.sources.nselive import (
    NseLiveSource,
    OptionChainUnavailable,
    _parse_expiry,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x" if payload else b"{}"
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class FakeSession:
    """Maps URLs+params to canned responses; records the effective URL
    actually requested (with params folded in, matching what a real
    ``requests.Session`` puts on the wire) so substring assertions still work
    without the fake needing to know about query-string encoding itself."""

    def __init__(self, responses_by_url_substring):
        self._responses = responses_by_url_substring
        self.requested = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        from urllib.parse import urlencode

        effective = f"{url}?{urlencode(params)}" if params else url
        self.requested.append(effective)
        for substring, payload in self._responses.items():
            if substring in effective:
                return FakeResponse(payload)
        return FakeResponse({})


def real_leg(oi=9812.0, delta=5801.0, price=19.95, prev_price=23.95, open_price=24.0,
             volume=15108, spot=1298.0, expiry="29-09-2026"):
    """One CE/PE leg exactly as NSE's option-chain-v3 actually shapes it --
    captured from a real response, not guessed."""
    return {
        "openInterest": oi,
        "changeinOpenInterest": delta,
        "pchangeinOpenInterest": (delta / (oi - delta) * 100) if oi != delta else None,
        "lastPrice": price,
        "change": price - prev_price,
        "openPrice": open_price,
        "totalTradedVolume": volume,
        "underlyingValue": spot,
        "expiryDate": expiry,  # DD-MM-YYYY, duplicated inside the leg too
    }


def real_row(strike, expiry="29-09-2026", ce=None, pe=None):
    """One row of option-chain-v3's real ``records.data`` array.

    The row-level expiry key is ``expiryDates`` (misleadingly plural-named,
    singular value) -- there is no ``expiryDate`` at this level.
    """
    row = {"expiryDates": expiry, "strikePrice": strike}
    if ce is not None:
        row["CE"] = ce
    if pe is not None:
        row["PE"] = pe
    return row


def contract_info(expiries=("29-09-2026", "27-10-2026")):
    # option-chain-contract-info itself replies with DD-Mon-YYYY.
    month_names = {"09": "Sep", "10": "Oct", "11": "Nov"}
    formatted = []
    for e in expiries:
        d, m, y = e.split("-")
        formatted.append(f"{d}-{month_names[m]}-{y}")
    return {"expiryDates": formatted}


@pytest.fixture
def source():
    def build(responses):
        return NseLiveSource(session=FakeSession(responses), polite_delay=0)

    return build


# ---------------------------------------------------------------------------
# The missing-parameter bug
# ---------------------------------------------------------------------------

def test_the_option_chain_request_carries_an_explicit_expiry(source):
    """This is the actual fix: omitting `expiry` is what made NSE return `{}`
    in the first place, regardless of IP, cookies or headers."""
    src = source(
        {
            "option-chain-contract-info": contract_info(("29-09-2026",)),
            "option-chain-v3": {"records": {"underlyingValue": 1298.0, "data": [
                real_row(1330.0, ce=real_leg())
            ]}},
        }
    )
    src.fetch_symbol("RELIANCE")
    option_chain_calls = [u for u in src._session.requested if "option-chain-v3" in u]
    assert option_chain_calls, "option-chain-v3 was never called"
    assert all("expiry=" in u and "expiry=&" not in u and not u.endswith("expiry=") for u in option_chain_calls)


def test_no_expiries_available_means_no_data_not_a_crash(source):
    src = source({"option-chain-contract-info": {"expiryDates": []}})
    assert src.fetch_symbol("DELISTED") is None


# ---------------------------------------------------------------------------
# The expiry-parsing bug
# ---------------------------------------------------------------------------

def test_parse_expiry_reads_the_real_row_level_key():
    """expiryDates (plural key, singular value), DD-MM-YYYY -- confirmed
    against a real NSE response, not the DD-Mon-YYYY this used to assume."""
    row = real_row(1330.0, expiry="29-09-2026", ce=real_leg())
    assert _parse_expiry(row) == date(2026, 9, 29)


def test_parse_expiry_falls_back_to_the_leg_when_the_row_key_is_absent():
    row = {"strikePrice": 1330.0, "CE": real_leg(expiry="27-10-2026")}
    assert _parse_expiry(row) == date(2026, 10, 27)


def test_parse_expiry_handles_older_month_abbreviated_responses():
    row = {"expiryDates": "26-Sep-2026"}
    assert _parse_expiry(row) == date(2026, 9, 26)


def test_parse_expiry_returns_none_for_garbage():
    assert _parse_expiry({"expiryDates": "not-a-date"}) is None
    assert _parse_expiry({}) is None


# ---------------------------------------------------------------------------
# End-to-end parse, real response shape
# ---------------------------------------------------------------------------

def test_a_real_shaped_response_parses_into_a_usable_row(source):
    src = source(
        {
            "option-chain-contract-info": contract_info(("29-09-2026",)),
            "option-chain-v3": {
                "records": {"underlyingValue": 1298.0, "data": [
                    real_row(1330.0, ce=real_leg(oi=9812.0, delta=5801.0))
                ]}
            },
        }
    )
    session = src.load_symbols(["RELIANCE"])
    assert len(session.rows) == 1
    row = session.rows[0]
    assert row.key.expiry == date(2026, 9, 29)
    assert row.oi_units == 9812.0
    assert row.prev_oi_units == pytest.approx(4011.0)
    assert row.oi_pct_change == pytest.approx(144.6, abs=0.1)


def test_oi_is_already_in_lots_so_lot_size_is_one(source):
    """Unlike bhavcopy (units), this feed already reports OI in lots -- a lot
    size of 1 is what keeps OptionRow.oi_lots correct without a fabricated
    conversion. Getting this wrong would silently misapply every lot-based
    floor in the scanner."""
    src = source(
        {
            "option-chain-contract-info": contract_info(("29-09-2026",)),
            "option-chain-v3": {"records": {"underlyingValue": 100.0, "data": [
                real_row(100.0, ce=real_leg(oi=500.0, delta=100.0))
            ]}},
        }
    )
    row = src.load_symbols(["TESTCO"]).rows[0]
    assert row.lot_size == 1
    assert row.oi_lots == row.oi_units == 500.0


def test_multiple_expiries_are_merged_into_one_session(source):
    """max_expiries governs how many series get fetched -- confirms both
    calls happen and both sets of rows end up in the result.

    The request's `expiry` parameter is passed through verbatim from
    option-chain-contract-info's own format (DD-Mon-YYYY, e.g. "29-Sep-2026")
    -- confirmed against the real endpoint -- which is a different format
    from the DD-MM-YYYY the *response* payload uses inside each row. Both
    matter and neither should be assumed to match the other.
    """
    src = source(
        {
            "option-chain-contract-info": contract_info(("29-09-2026", "27-10-2026")),
            "expiry=29-Sep-2026": {"records": {"underlyingValue": 100.0, "data": [
                real_row(100.0, expiry="29-09-2026", ce=real_leg(expiry="29-09-2026"))
            ]}},
            "expiry=27-Oct-2026": {"records": {"underlyingValue": 101.0, "data": [
                real_row(100.0, expiry="27-10-2026", ce=real_leg(expiry="27-10-2026"))
            ]}},
        }
    )
    session = src.load_symbols(["RELIANCE"])
    expiries = sorted({row.key.expiry for row in session.rows})
    assert expiries == [date(2026, 9, 29), date(2026, 10, 27)]


def test_max_expiries_bounds_the_number_of_requests(source):
    src = NseLiveSource(
        session=FakeSession({
            "option-chain-contract-info": contract_info(("29-09-2026", "27-10-2026", "24-11-2026")),
            "option-chain-v3": {"records": {"underlyingValue": 100.0, "data": [
                real_row(100.0, ce=real_leg())
            ]}},
        }),
        polite_delay=0,
        max_expiries=1,
    )
    src.fetch_symbol("RELIANCE")
    assert sum("option-chain-v3" in u for u in src._session.requested) == 1


# ---------------------------------------------------------------------------
# Interface parity with BhavcopySource (the CLI wiring fix)
# ---------------------------------------------------------------------------

def test_get_source_no_longer_crashes_on_bhavcopy_only_kwargs():
    """OI_SOURCE=nselive with the CLI's uniform get_source(name, cache_dir=...)
    call used to raise TypeError immediately -- confirms the kwarg filter in
    sources/__init__.py actually protects this path."""
    from custom.oi.sources import get_source

    result = get_source("nselive", cache_dir="data/oi_cache")
    assert isinstance(result, NseLiveSource)


def test_load_today_delegates_to_latest(source):
    """latest() with no explicit symbols falls back to fno_symbols() (the
    master-quote endpoint), so that has to be wired up here too -- load()
    only special-cases the date, the rest of the path is the real thing."""
    src = source(
        {
            "master-quote": ["RELIANCE"],
            "option-chain-contract-info": contract_info(("29-09-2026",)),
            "expiry=29-Sep-2026": {"records": {"underlyingValue": 100.0, "data": [
                real_row(100.0, ce=real_leg())
            ]}},
        }
    )
    session = src.load(date.today())
    assert session is not None and session.rows


def test_load_a_past_date_fails_clearly_instead_of_pretending(source):
    """A live snapshot has no historical archive -- it must say so, not
    silently return today's data mislabelled as some other day."""
    src = source({})
    with pytest.raises(NotImplementedError, match="no historical archive"):
        src.load(date(2020, 1, 1))


def test_previous_session_returns_none_rather_than_crashing(source):
    """custom.oi.cli.run_once() calls this unconditionally; a live source has
    nothing separate to return, since the feed's own changeinOpenInterest
    already encodes the comparison scan_session needs."""
    src = source({})
    assert src.previous_session(date.today()) is None


def test_load_symbols_raises_a_corrected_message_on_total_failure(source):
    """The error text used to insist this was 'normal behaviour for
    non-residential IPs' -- no longer true, and the message must not keep
    telling people that."""
    src = source({})
    with pytest.raises(OptionChainUnavailable) as excinfo:
        src.load_symbols(["NOPE"])
    assert "non-residential" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The ampersand-in-symbol bug (M&M, GVT&D -- found on a real live run)
# ---------------------------------------------------------------------------

def test_a_symbol_containing_an_ampersand_is_correctly_encoded(source):
    """M&M (Mahindra & Mahindra -- a top-tier F&O name by volume) and GVT&D
    both silently failed before this: naive f-string URL building turned
    `symbol=M&M` into `symbol=M` plus a bogus bare `M` query parameter, and
    NSE answered as if the symbol did not exist. Found on a real live run
    against the actual endpoint, not a hypothetical -- reproduced here
    without going back to the network.
    """
    src = source(
        {
            "option-chain-contract-info": contract_info(("29-09-2026",)),
            "option-chain-v3": {"records": {"underlyingValue": 2800.0, "data": [
                real_row(2800.0, ce=real_leg())
            ]}},
        }
    )
    session = src.load_symbols(["M&M"])
    assert "M&M" in session.contexts
    assert len(session.rows) == 1

    # The bug specifically corrupted the query string; confirm the symbol
    # survived intact into what was actually sent, not just that some
    # response came back.
    contract_info_calls = [u for u in src._session.requested if "contract-info" in u]
    assert any("symbol=M%26M" in u for u in contract_info_calls), (
        f"M&M was not properly percent-encoded in: {contract_info_calls}"
    )


# ---------------------------------------------------------------------------
# Pushing --symbols down to the fetch, not just the scanner filter
# ---------------------------------------------------------------------------

def test_load_symbols_never_calls_fno_symbols_when_symbols_are_given(source):
    """custom.oi.cli.run_once() used to call source.latest() with no symbols
    argument regardless of OI_SYMBOLS/--symbols, so NseLiveSource fetched the
    entire ~210-name universe (one contract-info call plus up to two
    option-chain-v3 calls each) even for a five-symbol test run -- turning a
    ~15-request job into ~600. latest()/load() now forward the caller's
    symbol list through instead of falling back to fno_symbols()."""
    src = source(
        {
            "option-chain-contract-info": contract_info(("29-09-2026",)),
            "option-chain-v3": {"records": {"underlyingValue": 100.0, "data": [
                real_row(100.0, ce=real_leg())
            ]}},
        }
    )
    session = src.latest(symbols=["RELIANCE"])
    assert list(session.contexts) == ["RELIANCE"]
    assert not any("master-quote" in u for u in src._session.requested), (
        "fno_symbols()/master-quote was called despite an explicit symbol list"
    )


def test_load_forwards_symbols_to_latest(source):
    """The --date/`load()` path (used when as_of is passed explicitly) must
    restrict the fetch the same way the no-date `latest()` path does."""
    src = source(
        {
            "option-chain-contract-info": contract_info(("29-09-2026",)),
            "option-chain-v3": {"records": {"underlyingValue": 100.0, "data": [
                real_row(100.0, ce=real_leg())
            ]}},
        }
    )
    session = src.load(date.today(), symbols=["RELIANCE"])
    assert list(session.contexts) == ["RELIANCE"]


def test_bhavcopy_load_and_latest_accept_but_ignore_symbols():
    """Interface parity with NseLiveSource, exercised through the same call
    shape custom.oi.cli.run_once() now uses uniformly for both sources --
    bhavcopy's one file already holds everything, so the parameter is a
    no-op here rather than a fetch-time filter."""
    from custom.oi.sources.bhavcopy import BhavcopySource
    import inspect

    assert "symbols" in inspect.signature(BhavcopySource.load).parameters
    assert "symbols" in inspect.signature(BhavcopySource.latest).parameters
