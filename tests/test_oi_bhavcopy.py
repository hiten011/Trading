"""Parsing NSE's F&O bhavcopy, and the caching that keeps a backtest offline."""

from datetime import date

import pytest
import requests

from custom.oi.sources.bhavcopy import BhavcopySource, BhavcopyUnavailable
from tests.factories import bhavcopy_zip

TODAY = date(2026, 8, 12)


class FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content

    @property
    def ok(self):
        return 200 <= self.status_code < 300


class FakeSession:
    """Stands in for requests.Session, recording what was asked for."""

    def __init__(self, responses):
        self.responses = responses
        self.requested = []
        self.headers = {}

    def get(self, url, timeout=None):
        self.requested.append(url)
        response = self.responses.pop(0) if self.responses else FakeResponse(404)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def source(tmp_path):
    def build(responses=None):
        return BhavcopySource(
            cache_dir=str(tmp_path / "cache"),
            session=FakeSession(list(responses or [])),
        )

    return build


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_option_rows_are_parsed_with_their_open_interest(source):
    payload = bhavcopy_zip(
        [{"StrkPric": 100.0, "OptnTp": "CE", "OpnIntrst": 500_000, "ChngInOpnIntrst": 450_000}],
        trade_date=TODAY,
    )
    session = source().parse(payload, day=TODAY)
    assert len(session.rows) == 1
    row = session.rows[0]
    assert row.key.symbol == "TESTCO"
    assert row.oi_units == 500_000
    assert row.prev_oi_units == 50_000
    assert row.oi_pct_change == pytest.approx(900.0)


def test_futures_rows_become_context_not_option_rows(source):
    """Futures are where the four-box buildup table is actually valid, so they
    are carried as confirmation rather than scanned as contracts."""
    payload = bhavcopy_zip(
        [
            {"FinInstrmTp": "STO", "OptnTp": "CE"},
            {"FinInstrmTp": "STF", "OptnTp": "", "StrkPric": 0.0,
             "OpnIntrst": 1_000_000, "ChngInOpnIntrst": 100_000,
             "ClsPric": 105.0, "PrvsClsgPric": 100.0},
        ],
        trade_date=TODAY,
    )
    session = source().parse(payload, day=TODAY)
    assert len(session.rows) == 1
    context = session.contexts["TESTCO"]
    assert context.futures_oi == 1_000_000
    assert context.futures_buildup.value == "LONG_BUILDUP"


def test_symbol_context_aggregates_both_sides_of_the_book(source):
    payload = bhavcopy_zip(
        [
            {"OptnTp": "CE", "StrkPric": 100.0, "OpnIntrst": 600_000, "ChngInOpnIntrst": 100_000},
            {"OptnTp": "PE", "StrkPric": 100.0, "OpnIntrst": 300_000, "ChngInOpnIntrst": 50_000},
        ],
        trade_date=TODAY,
    )
    context = source().parse(payload, day=TODAY).contexts["TESTCO"]
    assert context.total_call_oi == 600_000
    assert context.total_put_oi == 300_000
    assert context.pcr == pytest.approx(0.5)


def test_rows_without_a_lot_size_or_strike_are_skipped(source):
    payload = bhavcopy_zip(
        [
            {"StrkPric": 100.0, "NewBrdLotQty": 0},
            {"StrkPric": 0.0, "NewBrdLotQty": 500},
            {"StrkPric": 110.0, "NewBrdLotQty": 500},
        ],
        trade_date=TODAY,
    )
    session = source().parse(payload, day=TODAY)
    assert [row.key.strike for row in session.rows] == [110.0]


def test_the_underlying_price_survives_a_stray_zero(source):
    """UndrlygPric repeats on every contract; an untraded far strike can carry
    a zero, which must not become the symbol's spot."""
    payload = bhavcopy_zip(
        [
            {"StrkPric": 100.0, "UndrlygPric": 0.0},
            {"StrkPric": 110.0, "UndrlygPric": 1316.95},
        ],
        trade_date=TODAY,
    )
    assert source().parse(payload, day=TODAY).contexts["TESTCO"].spot == pytest.approx(1316.95)


def test_an_empty_zip_is_reported_clearly(source):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "nothing here")
    with pytest.raises(BhavcopyUnavailable, match="no CSV"):
        source().parse(buffer.getvalue(), day=TODAY)


# ---------------------------------------------------------------------------
# Fetching and caching
# ---------------------------------------------------------------------------

def test_a_downloaded_session_is_cached_and_not_re_requested(source):
    payload = bhavcopy_zip([{"StrkPric": 100.0}], trade_date=TODAY)
    src = source([FakeResponse(200, payload)])
    assert src.fetch_raw(TODAY) == payload
    assert src.fetch_raw(TODAY) == payload  # served from disk, no second GET
    assert len(src._session.requested) == 1


def test_a_holiday_is_remembered_so_it_is_not_re_requested(source):
    """A backtest walks the calendar; without this it re-issues the same 404
    for every holiday on every run."""
    src = source([FakeResponse(404)])
    assert src.fetch_raw(TODAY) is None
    assert src.fetch_raw(TODAY) is None
    assert len(src._session.requested) == 1


def test_a_non_zip_two_hundred_is_retried_then_raises(source, monkeypatch):
    """NSE serves an HTML interstitial with a 200 when it is throttling."""
    monkeypatch.setattr("custom.oi.sources.bhavcopy.time.sleep", lambda *_: None)
    src = source([FakeResponse(200, b"<html>go away</html>")] * 4)
    with pytest.raises(BhavcopyUnavailable):
        src.fetch_raw(TODAY)
    assert len(src._session.requested) == 4


def test_a_network_error_is_retried(source, monkeypatch):
    monkeypatch.setattr("custom.oi.sources.bhavcopy.time.sleep", lambda *_: None)
    payload = bhavcopy_zip([{"StrkPric": 100.0}], trade_date=TODAY)
    src = source([requests.ConnectionError("boom"), FakeResponse(200, payload)])
    assert src.fetch_raw(TODAY) == payload


def test_latest_walks_back_past_days_with_no_file(source):
    """NSE publishes after the close, so before ~18:00 IST the newest session
    available is the previous one -- and weekends are several days back."""
    payload = bhavcopy_zip([{"StrkPric": 100.0}], trade_date=date(2026, 8, 10))
    src = source([FakeResponse(404), FakeResponse(404), FakeResponse(200, payload)])
    session = src.latest(as_of=TODAY)
    assert session.trade_date == TODAY - __import__("datetime").timedelta(days=2)


def test_latest_gives_up_with_a_useful_message(source):
    src = source([FakeResponse(404)] * 12)
    with pytest.raises(BhavcopyUnavailable, match="No F&O bhavcopy"):
        src.latest(as_of=TODAY, max_lookback=3)


def test_the_parsed_session_cache_is_bounded(source):
    """A two-year backtest is ~530 sessions of ~35k contracts; nothing may be
    retained indefinitely."""
    responses = [
        FakeResponse(200, bhavcopy_zip([{"StrkPric": 100.0}], trade_date=day))
        for day in (date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12))
    ]
    src = BhavcopySource(
        cache_dir=str(__import__("tempfile").mkdtemp()),
        session=FakeSession(responses),
        memo_limit=2,
    )
    for day in (date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)):
        src.load(day)
    assert len(src._memo) == 2
    assert date(2026, 8, 10) not in src._memo


def test_iter_range_skips_weekends_without_asking(source):
    payload = bhavcopy_zip([{"StrkPric": 100.0}], trade_date=date(2026, 8, 14))
    src = source([FakeResponse(200, payload)])
    # 15th and 16th Aug 2026 are a Saturday and Sunday.
    sessions = list(src.iter_range(date(2026, 8, 14), date(2026, 8, 16), progress_every=0))
    assert len(sessions) == 1
    assert len(src._session.requested) == 1


def test_indices_are_identified_from_the_exchange_instrument_type(source):
    """A hardcoded name list silently misses newly listed indices.

    NIFTYFPI trades 366 option contracts a session and is not a name anyone
    thinks to add to such a list, so OI_EXCLUDE_INDICES would have let it
    through. The exchange already tags index options as IDO; trust that.
    """
    payload = bhavcopy_zip(
        [
            {"FinInstrmTp": "IDO", "TckrSymb": "NIFTYFPI", "StrkPric": 100.0},
            {"FinInstrmTp": "STO", "TckrSymb": "RELIANCE", "StrkPric": 100.0},
        ],
        trade_date=TODAY,
    )
    contexts = source().parse(payload, day=TODAY).contexts
    assert contexts["NIFTYFPI"].is_index
    assert not contexts["RELIANCE"].is_index


def test_a_holiday_marker_is_only_written_for_a_past_date(source, monkeypatch):
    """Today's file is merely late, not absent.

    NSE publishes after ~18:00 IST. Caching a 404 for today as permanent means
    a run at 14:00 poisons the cache, and the 19:00 run keeps skipping the very
    session it was waiting for -- so the scanner would stay a day behind until
    someone cleared the cache by hand.
    """
    import os

    from datetime import date as real_date

    src = source([FakeResponse(404), FakeResponse(404)])
    monkeypatch.setattr(type(src), "_today_ist", staticmethod(lambda: real_date(2026, 8, 26)))

    assert src.fetch_raw(real_date(2026, 8, 26)) is None
    assert not os.path.isfile(src._missing_marker(real_date(2026, 8, 26)))
    # ...so a later run the same day asks again rather than trusting the miss.
    assert src.fetch_raw(real_date(2026, 8, 26)) is None
    assert len(src._session.requested) == 2


def test_todays_file_is_picked_up_once_nse_publishes_it(source, monkeypatch):
    from datetime import date as real_date

    payload = bhavcopy_zip([{"StrkPric": 100.0}], trade_date=real_date(2026, 8, 26))
    src = source([FakeResponse(404), FakeResponse(200, payload)])
    monkeypatch.setattr(type(src), "_today_ist", staticmethod(lambda: real_date(2026, 8, 26)))

    assert src.fetch_raw(real_date(2026, 8, 26)) is None      # 14:00, not out yet
    assert src.fetch_raw(real_date(2026, 8, 26)) == payload   # 19:00, published


def test_a_past_holiday_is_still_remembered(source, monkeypatch):
    """The original behaviour must survive: a backtest walking the calendar
    must not re-request the same holiday on every run."""
    import os

    from datetime import date as real_date

    src = source([FakeResponse(404)])
    monkeypatch.setattr(type(src), "_today_ist", staticmethod(lambda: real_date(2026, 8, 26)))

    assert src.fetch_raw(real_date(2026, 8, 15)) is None      # Independence Day
    assert os.path.isfile(src._missing_marker(real_date(2026, 8, 15)))
    assert src.fetch_raw(real_date(2026, 8, 15)) is None
    assert len(src._session.requested) == 1
