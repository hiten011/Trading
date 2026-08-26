"""Synthetic candle builders so tests do not need market data."""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_frame(closes, volumes=None, start="2024-01-01") -> pd.DataFrame:
    """Build an OHLCV frame from a close series, with sane highs/lows."""
    closes = np.asarray(closes, dtype=float)
    index = pd.bdate_range(start=start, periods=len(closes))
    if volumes is None:
        volumes = np.full(len(closes), 1_000_000.0)
    volumes = np.asarray(volumes, dtype=float)
    return pd.DataFrame(
        {
            "Open": closes * 0.995,
            "High": closes * 1.01,
            "Low": closes * 0.99,
            "Close": closes,
            "Volume": volumes,
        },
        index=index,
    )


def flat_frame(periods: int = 120, price: float = 100.0) -> pd.DataFrame:
    """A stock going nowhere: no strategy should fire on this."""
    return make_frame(np.full(periods, price))


def breakout_frame(periods: int = 120) -> pd.DataFrame:
    """An uptrend with normal pullbacks that closes at a new high on volume.

    The wobble matters: a perfectly straight line prints RSI 100 and an ATR of
    almost nothing, which no realistic indicator would ever see.
    """
    trend = np.linspace(100.0, 140.0, periods)
    wobble = 3.0 * np.sin(np.linspace(0, 9 * np.pi, periods))
    closes = trend + wobble
    closes[-1] = closes[-2] * 1.03  # decisive breakout candle
    volumes = np.full(periods, 1_000_000.0)
    volumes[-1] = 5_000_000.0
    return make_frame(closes, volumes)


# ---------------------------------------------------------------------------
# F&O option-chain builders (custom.oi)
# ---------------------------------------------------------------------------

import io as _io
import zipfile as _zipfile
from datetime import date as _date

_BHAVCOPY_COLUMNS = [
    "TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
    "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric",
    "OptnTp", "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric",
    "LastPric", "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst",
    "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId",
    "NewBrdLotQty", "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4",
]


def option_row(
    symbol="TESTCO",
    trade_date=_date(2026, 8, 12),
    expiry=_date(2026, 8, 25),
    strike=100.0,
    option_type="CE",
    oi_units=500_000.0,
    delta_oi_units=450_000.0,
    volume_lots=5_000.0,
    close=10.0,
    prev_close=5.0,
    open_price=8.0,
    underlying=100.0,
    lot_size=500,
):
    """One :class:`custom.oi.models.OptionRow`, with liquid defaults.

    The defaults deliberately clear every gate in the shipped configuration,
    so a test can make exactly one field bad and assert on that gate alone.
    """
    from custom.oi.models import ContractKey, OptionRow, OptionType

    return OptionRow(
        key=ContractKey(symbol, expiry, strike, OptionType(option_type)),
        trade_date=trade_date,
        lot_size=lot_size,
        oi_units=oi_units,
        delta_oi_units=delta_oi_units,
        volume_lots=volume_lots,
        turnover=volume_lots * lot_size * underlying,
        close=close,
        prev_close=prev_close,
        underlying=underlying,
        open_price=open_price,
    )


def underlying_context(symbol="TESTCO", trade_date=_date(2026, 8, 12), spot=100.0, **kwargs):
    from custom.oi.models import UnderlyingContext

    defaults = dict(
        total_call_oi=5_000_000.0,
        total_put_oi=4_000_000.0,
        prev_total_call_oi=4_500_000.0,
        prev_total_put_oi=4_000_000.0,
        futures_oi=1_000_000.0,
        futures_delta_oi=50_000.0,
        futures_close=101.0,
        futures_prev_close=100.0,
        prev_spot=100.0,
    )
    defaults.update(kwargs)
    return UnderlyingContext(symbol=symbol, trade_date=trade_date, spot=spot, **defaults)


def session_from_rows(rows, trade_date=_date(2026, 8, 12), contexts=None):
    """Wrap rows in a SessionData, deriving contexts when not supplied."""
    from collections import defaultdict

    from custom.oi.sources.bhavcopy import SessionData

    if contexts is None:
        call_oi, put_oi = defaultdict(float), defaultdict(float)
        spot = {}
        for row in rows:
            bucket = call_oi if row.key.option_type.value == "CE" else put_oi
            bucket[row.key.symbol] += row.oi_units
            spot[row.key.symbol] = max(spot.get(row.key.symbol, 0.0), row.underlying)
        contexts = {
            symbol: underlying_context(
                symbol=symbol,
                trade_date=trade_date,
                spot=value,
                total_call_oi=call_oi[symbol] or 1.0,
                total_put_oi=put_oi[symbol] or 1.0,
            )
            for symbol, value in spot.items()
        }
    return SessionData(trade_date=trade_date, rows=list(rows), contexts=contexts)


def bhavcopy_zip(records, trade_date=_date(2026, 8, 12)) -> bytes:
    """Build a zipped UDiFF bhavcopy holding ``records``.

    Each record is a dict of column overrides; anything unset gets a sane
    default, so a test only states the fields it cares about.
    """
    import pandas as pd

    rows = []
    for record in records:
        row = {column: "" for column in _BHAVCOPY_COLUMNS}
        row.update(
            {
                "TradDt": trade_date.isoformat(),
                "BizDt": trade_date.isoformat(),
                "Sgmt": "FO",
                "Src": "NSE",
                "FinInstrmTp": "STO",
                "TckrSymb": "TESTCO",
                "XpryDt": "2026-08-25",
                "StrkPric": 100.0,
                "OptnTp": "CE",
                "OpnPric": 8.0,
                "HghPric": 12.0,
                "LwPric": 7.0,
                "ClsPric": 10.0,
                "LastPric": 10.0,
                "PrvsClsgPric": 5.0,
                "UndrlygPric": 100.0,
                "SttlmPric": 10.0,
                "OpnIntrst": 500_000,
                "ChngInOpnIntrst": 450_000,
                "TtlTradgVol": 5_000,
                "TtlTrfVal": 250_000_000.0,
                "TtlNbOfTxsExctd": 900,
                "SsnId": "F1",
                "NewBrdLotQty": 500,
            }
        )
        row.update(record)
        rows.append(row)

    csv_bytes = pd.DataFrame(rows, columns=_BHAVCOPY_COLUMNS).to_csv(index=False).encode()
    buffer = _io.BytesIO()
    with _zipfile.ZipFile(buffer, "w", _zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"BhavCopy_NSE_FO_0_0_0_{trade_date:%Y%m%d}_F_0000.csv", csv_bytes
        )
    return buffer.getvalue()
