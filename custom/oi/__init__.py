"""NSE F&O option open-interest "blast" scanner.

A Python port of the TradingView Pine indicator that flagged option contracts
whose open interest jumped more than a threshold percentage against the
previous bar -- except this one covers the entire F&O universe (~215
underlyings) instead of the ~32 that Pine's 64-``request.security()`` cap
allows, and it runs unattended.

Layout::

    models.py       the value types: one contract-session, one alert
    config.py       every knob, resolved from the environment
    sources/        where option data comes from (bhavcopy, live chain)
    scanner.py      the actual signal: % OI change -> filters -> alert
    state.py        per-contract cooldown so alerts do not repeat
    alerts.py       Telegram rendering
    backtest.py     historical replay + forward-return evaluation
    cli.py          ``python3 -m custom.oi.cli``
"""

__all__ = ["models", "config", "scanner", "state", "alerts", "backtest"]
