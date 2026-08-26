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
    NSE's ``/api/option-chain-*`` endpoints, for intraday snapshots. Kept
    behind the same interface, but see the module docstring: NSE actively
    degrades this endpoint for non-browser clients and it currently returns
    an empty body from most hosts. Treat it as best-effort.
"""

from custom.oi.sources.bhavcopy import BhavcopySource, SessionData

__all__ = ["BhavcopySource", "SessionData", "get_source"]


def get_source(name: str, **kwargs):
    """Resolve a source by name."""
    name = (name or "bhavcopy").strip().lower()
    if name in ("bhavcopy", "eod", "nse-eod"):
        return BhavcopySource(**kwargs)
    if name in ("nselive", "live", "nse-live"):
        from custom.oi.sources.nselive import NseLiveSource

        return NseLiveSource(**kwargs)
    raise ValueError(f"Unknown OI data source {name!r} (want 'bhavcopy' or 'nselive')")
