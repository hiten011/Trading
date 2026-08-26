"""Where option open-interest data comes from.

Two implementations, deliberately different in what they can promise:

``bhavcopy``
    NSE's official end-of-day F&O UDiFF archive. Free, no account, no
    rate limiting, complete (every contract on every one of ~215
    underlyings) and available back to July 2024. End-of-day only, so the
    scan resolution is one trading session -- which is exactly the
    resolution the Pine original ran at on a daily chart. This is the
    default and the only source the backtest uses.

``nselive``
    NSE's ``/api/option-chain-v3`` endpoint, for intraday snapshots -- see
    that module's docstring for why it now works (a missing required
    parameter, not a block) and what it still cannot do (address a past
    date; it only ever has right now).
"""

from custom.oi.sources.bhavcopy import BhavcopySource, SessionData

__all__ = ["BhavcopySource", "SessionData", "get_source"]

# Constructor kwargs each source actually accepts. get_source() is called
# uniformly from the CLI with a superset (e.g. cache_dir, which only
# BhavcopySource uses) -- forwarding all of it unfiltered used to crash
# NseLiveSource with an unexpected-keyword-argument TypeError the moment
# anyone set OI_SOURCE=nselive, which the .env comments openly offer as a
# choice. Filtering here means picking the wrong source fails inside the
# source's own logic (loudly, on purpose -- see nselive.py) instead of on
# construction.
_ACCEPTED_KWARGS = {
    "bhavcopy": {"cache_dir", "session", "request_timeout", "memo_limit"},
    "nselive": {"session", "polite_delay", "request_timeout", "max_expiries"},
}


def get_source(name: str, **kwargs):
    """Resolve a source by name."""
    name = (name or "bhavcopy").strip().lower()
    if name in ("bhavcopy", "eod", "nse-eod"):
        accepted = _ACCEPTED_KWARGS["bhavcopy"]
        return BhavcopySource(**{k: v for k, v in kwargs.items() if k in accepted})
    if name in ("nselive", "live", "nse-live"):
        from custom.oi.sources.nselive import NseLiveSource

        accepted = _ACCEPTED_KWARGS["nselive"]
        return NseLiveSource(**{k: v for k, v in kwargs.items() if k in accepted})
    raise ValueError(f"Unknown OI data source {name!r} (want 'bhavcopy' or 'nselive')")
