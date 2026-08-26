"""Unit tests for scripts/nse_oi_buildup_scanner.py.

This is a standalone script (own CLI, own CSV, own Telegram sender), not part
of the ``custom.oi`` package, so it is imported directly by path rather than
as a package module. Its network calls (``NSELive``, live NSE endpoints) are
never exercised here -- see the module's own docstring for the documented
"works from a residential IP, not reliably from a datacenter one" caveat, and
this repo's ``custom/oi/`` scanner for the data source this project actually
runs unattended. What is tested here is everything that does not need a
network: the threshold/filtering logic, the dataclass, CSV export, and table
rendering -- using NSE's real option-chain-v3 JSON shape as fixtures, since
that is what production data actually looks like.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "nse_oi_buildup_scanner.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("nse_oi_buildup_scanner", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_module()


class FakeNSELive:
    """Stands in for jugaad_data.nse.NSELive without touching the network."""

    def __init__(self, chains):
        self._chains = chains
        self.s = None

    def equities_option_chain(self, symbol):
        return self._chains[symbol]

    def index_option_chain(self, symbol):
        return self._chains[symbol]


def leg(oi=1000, delta=200, pct=25.0, price=10.0, volume=500, spot=1300.0, **overrides):
    """One CE/PE leg, NSE's real option-chain-v3 field names."""
    row = {
        "openInterest": oi,
        "changeinOpenInterest": delta,
        "pchangeinOpenInterest": pct,
        "lastPrice": price,
        "totalTradedVolume": volume,
        "underlyingValue": spot,
    }
    row.update(overrides)
    return row


def chain(rows):
    return {"records": {"data": rows}}


# ---------------------------------------------------------------------------
# scan_symbol / threshold logic
# ---------------------------------------------------------------------------

def test_a_leg_over_the_threshold_is_flagged():
    data = chain([{"strikePrice": 1300, "expiryDate": "26-Sep-2026",
                    "CE": leg(pct=2500.0), "PE": leg(pct=10.0)}])
    nse = FakeNSELive({"RELIANCE": data})
    hits = scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=2000.0)
    assert len(hits) == 1
    assert hits[0].option_type == "CE"
    assert hits[0].pct_change_oi == 2500.0


def test_both_legs_can_fire_independently():
    data = chain([{"strikePrice": 1300, "expiryDate": "26-Sep-2026",
                    "CE": leg(pct=2500.0), "PE": leg(pct=-3000.0)}])
    nse = FakeNSELive({"RELIANCE": data})
    hits = scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=2000.0)
    assert {h.option_type for h in hits} == {"CE", "PE"}


def test_the_threshold_is_on_the_absolute_value():
    """A -3000% OI collapse is exactly as extreme as +3000% growth."""
    data = chain([{"strikePrice": 1300, "expiryDate": "26-Sep-2026",
                    "CE": leg(pct=-2500.0), "PE": leg(pct=10.0)}])
    nse = FakeNSELive({"RELIANCE": data})
    hits = scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=2000.0)
    assert len(hits) == 1 and hits[0].pct_change_oi == -2500.0


def test_a_leg_under_the_threshold_is_not_flagged():
    data = chain([{"strikePrice": 1300, "expiryDate": "26-Sep-2026",
                    "CE": leg(pct=500.0), "PE": leg(pct=10.0)}])
    nse = FakeNSELive({"RELIANCE": data})
    assert scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=2000.0) == []


def test_the_threshold_boundary_is_exclusive():
    """The script's own comparison is `abs(pct) > threshold`, so an exact
    match at the boundary must not fire -- pin that down so a future change
    to >= is a deliberate decision, not a silent one."""
    data = chain([{"strikePrice": 1300, "expiryDate": "26-Sep-2026",
                    "CE": leg(pct=2000.0), "PE": leg(pct=10.0)}])
    nse = FakeNSELive({"RELIANCE": data})
    assert scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=2000.0) == []


def test_a_leg_with_no_pchangeinopeninterest_field_is_skipped_not_crashed():
    """NSE omits this field for a handful of illiquid strikes; None must not
    reach `abs(None)` and blow up the scan."""
    data = chain([{"strikePrice": 1300, "expiryDate": "26-Sep-2026",
                    "CE": {"openInterest": 0}, "PE": leg(pct=10.0)}])
    nse = FakeNSELive({"RELIANCE": data})
    # threshold above PE's 10.0 too, so a non-empty result here could only
    # mean the missing-field CE leg was mishandled.
    assert scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=50.0) == []


def test_a_row_missing_one_side_entirely_is_handled():
    """Deep ITM/OTM strikes sometimes have only a CE or only a PE listed."""
    data = chain([{"strikePrice": 5000, "expiryDate": "26-Sep-2026", "CE": leg(pct=3000.0)}])
    nse = FakeNSELive({"RELIANCE": data})
    hits = scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=2000.0)
    assert len(hits) == 1 and hits[0].option_type == "CE"


def test_an_empty_chain_yields_no_hits():
    nse = FakeNSELive({"RELIANCE": {"records": {"data": []}}})
    assert scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=100.0) == []


def test_fetch_failure_yields_no_hits_not_an_exception():
    """scan_symbol must survive a symbol NSE has nothing for."""

    class AlwaysFails(FakeNSELive):
        def equities_option_chain(self, symbol):
            raise RuntimeError("simulated NSE failure")

    nse = AlwaysFails({})
    assert scanner.scan_symbol(nse, "RELIANCE", is_index=False, threshold_pct=100.0) == []


def test_fetch_option_chain_retries_before_giving_up(monkeypatch):
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class FlakyOnce(FakeNSELive):
        def equities_option_chain(self, symbol):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return chain([{"strikePrice": 100, "expiryDate": "26-Sep-2026", "CE": leg()}])

    nse = FlakyOnce({})
    result = scanner.fetch_option_chain(nse, "RELIANCE", is_index=False)
    assert result is not None
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# OIBuildup / CSV / table rendering
# ---------------------------------------------------------------------------

def make_hit(**overrides):
    fields = dict(
        symbol="RELIANCE", option_type="CE", strike_price=1300.0,
        expiry_date="26-Sep-2026", open_interest=5000, change_in_oi=4000,
        pct_change_oi=2500.0, ltp=15.5, volume=1200, underlying_value=1300.0,
    )
    fields.update(overrides)
    return scanner.OIBuildup(**fields)


def test_save_csv_round_trips_every_field(tmp_path):
    hits = [make_hit(), make_hit(symbol="TCS", pct_change_oi=-3200.0)]
    path = str(tmp_path / "out.csv")
    scanner.save_csv(hits, path)
    rows = list(csv.DictReader(open(path)))
    assert len(rows) == 2
    assert rows[0]["symbol"] == "RELIANCE"
    assert float(rows[1]["pct_change_oi"]) == -3200.0


def test_save_csv_writes_nothing_for_an_empty_list(tmp_path):
    path = str(tmp_path / "out.csv")
    scanner.save_csv([], path)
    assert not Path(path).exists()


def test_print_table_reports_no_matches_when_empty():
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        scanner.print_table([])
    assert "No strikes crossed the threshold" in buffer.getvalue()


def test_print_table_shows_every_hit():
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        scanner.print_table([make_hit(), make_hit(symbol="TCS")])
    output = buffer.getvalue()
    assert "RELIANCE" in output and "TCS" in output


def test_hits_are_sorted_by_absolute_magnitude(monkeypatch):
    """scan_all sorts so the most extreme moves lead, regardless of sign."""
    data = {
        "A": chain([{"strikePrice": 1, "expiryDate": "26-Sep-2026", "CE": leg(pct=500.0)}]),
        "B": chain([{"strikePrice": 1, "expiryDate": "26-Sep-2026", "CE": leg(pct=-9000.0)}]),
        "C": chain([{"strikePrice": 1, "expiryDate": "26-Sep-2026", "CE": leg(pct=3000.0)}]),
    }
    monkeypatch.setattr(scanner, "NSELive", lambda: FakeNSELive(data))
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)
    # scan_all() calls get_fno_universe() unconditionally, even when --symbols
    # is given (see test_scan_all_fetches_the_universe_even_with_explicit_symbols
    # below) -- faked out here since it is irrelevant to what this test checks.
    monkeypatch.setattr(scanner, "get_fno_universe", lambda nse: ([], []))
    hits = scanner.scan_all(threshold_pct=100.0, symbols=["A", "B", "C"])
    assert [h.symbol for h in hits] == ["B", "C", "A"]


def test_scan_all_only_scans_the_given_symbols(monkeypatch):
    """--symbols must restrict what gets *scanned*, even though (see below)
    it does not currently skip the universe *fetch*."""
    data = {"RELIANCE": chain([{"strikePrice": 1, "expiryDate": "26-Sep-2026", "CE": leg(pct=3000.0)}])}
    monkeypatch.setattr(scanner, "NSELive", lambda: FakeNSELive(data))
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(scanner, "get_fno_universe", lambda nse: (["UNRELATED"], []))
    hits = scanner.scan_all(threshold_pct=100.0, symbols=["RELIANCE"])
    assert [h.symbol for h in hits] == ["RELIANCE"]


def test_scan_all_fetches_the_universe_even_with_explicit_symbols(monkeypatch):
    """Documents a real inefficiency in the script as shipped: get_fno_universe()
    is called unconditionally in scan_all(), so even `--symbols RELIANCE` pays
    for one extra network round-trip to /api/underlying-information that its
    result (the index/stock split) is not used for in that path. Not a
    functional bug -- the scan still runs correctly -- but worth knowing if
    a single-symbol run feels slower than it should. If this starts failing,
    that inefficiency has been fixed; the test can go with it."""
    calls = []
    monkeypatch.setattr(scanner, "NSELive", lambda: FakeNSELive(
        {"RELIANCE": chain([{"strikePrice": 1, "expiryDate": "26-Sep-2026", "CE": leg(pct=3000.0)}])}
    ))
    monkeypatch.setattr(scanner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(scanner, "get_fno_universe", lambda nse: (calls.append(1), ([], []))[1])
    scanner.scan_all(threshold_pct=100.0, symbols=["RELIANCE"])
    assert calls == [1]


def test_send_telegram_alert_does_nothing_for_no_hits(monkeypatch):
    """Must not call the network (or need a token) when there is nothing to send.

    send_telegram_alert does `import requests` *inside the function body*, so
    the patch target is the real top-level requests module, not an attribute
    of the script -- patching the latter silently does nothing, since the
    function's local import re-resolves the name from sys.modules every call.
    """
    import requests as real_requests

    called = []
    monkeypatch.setattr(real_requests, "post", lambda *a, **k: called.append(1))
    scanner.send_telegram_alert([], "token", "chat")
    assert called == []


def test_send_telegram_alert_caps_to_twenty_rows(monkeypatch):
    """Telegram messages have a length cap; the script itself documents
    slicing to 20 -- confirm the slice actually happens."""
    import requests as real_requests

    captured = {}

    def fake_post(url, data, timeout):
        captured["text"] = data["text"]

    monkeypatch.setattr(real_requests, "post", fake_post)
    hits = [make_hit(symbol=f"SYM{i}") for i in range(30)]
    scanner.send_telegram_alert(hits, "token", "chat")
    assert captured["text"].count("SYM") == 20
