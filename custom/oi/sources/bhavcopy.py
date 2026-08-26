"""NSE F&O end-of-day bhavcopy (UDiFF format) as an option-chain source.

Why this and not a broker feed or the live option-chain endpoint: it is the
only free, official, complete and *historically addressable* source of
per-contract open interest for the full F&O list. One ~1.2 MB zip per trading
session holds every contract on every underlying, and the same file format
goes back to July 2024, so the live scanner and the backtest run identical
code over identical inputs -- which is the only way a backtest result means
anything about live behaviour.

The file gives, per contract: open interest, the day's change in open
interest, traded volume, the option's own close and previous close, and the
underlying's price. That is everything the scanner needs, with no second
lookup for spot.

Two format facts that are load-bearing and were verified against the archive
rather than assumed:

* ``OpnIntrst`` - ``ChngInOpnIntrst`` reproduces the previous session's
  ``OpnIntrst`` exactly, on 100% of the ~35k contracts common to two
  consecutive files. So one file is enough to compute a day-over-day OI
  change; no join required.
* ``OpnIntrst`` is in units (shares) while ``TtlTradgVol`` is in lots.
  Confirmed by reconciling ``TtlTrfVal``: turnover / (volume x lot size)
  recovers the underlying price, turnover / volume does not.
"""

from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd
import requests

from custom.oi.models import (
    ContractKey,
    OptionRow,
    OptionType,
    UnderlyingContext,
)

LOGGER = logging.getLogger("custom.oi.bhavcopy")

ARCHIVE_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 4

# UDiFF instrument type codes.
STOCK_OPTION = "STO"
INDEX_OPTION = "IDO"
STOCK_FUTURE = "STF"
INDEX_FUTURE = "IDF"
OPTION_TYPES = {STOCK_OPTION, INDEX_OPTION}
FUTURE_TYPES = {STOCK_FUTURE, INDEX_FUTURE}

# Columns actually read. Anything else in the file is ignored.
USED_COLUMNS = [
    "TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
    "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric", "UndrlygPric",
    "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "NewBrdLotQty",
]


class BhavcopyUnavailable(RuntimeError):
    """No bhavcopy exists for that date (holiday/weekend), or fetching failed."""


@dataclass
class SessionData:
    """Every F&O contract for one trading session, already normalised."""

    trade_date: date
    rows: List[OptionRow] = field(default_factory=list)
    contexts: Dict[str, UnderlyingContext] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def symbols(self) -> List[str]:
        return sorted(self.contexts)

    def rows_for(self, symbol: str) -> List[OptionRow]:
        return [row for row in self.rows if row.key.symbol == symbol]

    def spot(self, symbol: str) -> float:
        context = self.contexts.get(symbol)
        return context.spot if context else 0.0


def _to_date(value) -> Optional[date]:
    stamp = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(stamp) else stamp.date()


class BhavcopySource:
    """Downloads, caches and parses NSE F&O bhavcopy files.

    Args:
        cache_dir: Where downloaded zips are kept. Re-running a backtest or
            restarting the container never re-downloads a session it has.
        session: Injectable ``requests.Session`` (tests pass a fake).
    """

    name = "bhavcopy"
    resolution = "1D"

    def __init__(
        self,
        cache_dir: str = "data/oi_cache",
        session: Optional[requests.Session] = None,
        request_timeout: int = REQUEST_TIMEOUT,
        memo_limit: int = 4,
    ) -> None:
        self.cache_dir = cache_dir
        self.request_timeout = request_timeout
        # Parsed sessions are held in memory, so the cache has to be bounded:
        # a two-year backtest is ~530 sessions x ~35k contracts, which is tens
        # of millions of objects if nothing is ever evicted. Four is enough
        # for the "current plus previous session" access pattern.
        self.memo_limit = max(memo_limit, 1)
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/",
            }
        )
        self._memo: "OrderedDict[date, SessionData]" = OrderedDict()

    # -- fetching ----------------------------------------------------------
    def _cache_path(self, day: date) -> str:
        return os.path.join(self.cache_dir, f"fo_{day:%Y%m%d}.zip")

    def _missing_marker(self, day: date) -> str:
        """A holiday has no file and never will; remember that so a backtest
        does not re-request the same 404 on every run."""
        return os.path.join(self.cache_dir, f"fo_{day:%Y%m%d}.missing")

    @staticmethod
    def _today_ist() -> date:
        """Today in Indian market time, whatever the host's clock is set to."""
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo("Asia/Kolkata")).date()
        except Exception:  # noqa: BLE001 - fall back to the local clock
            return date.today()

    def _may_still_be_published(self, day: date) -> bool:
        """Whether a 404 for ``day`` could become a real file later.

        NSE publishes the session's file after the close, around 18:00-19:00
        IST. A 404 for a *past* date means a holiday -- permanent, worth
        remembering. A 404 for today means "not yet", and caching that as
        permanent would make every later run that day keep skipping the
        session it was waiting for.
        """
        return day >= self._today_ist()

    def fetch_raw(self, day: date, force: bool = False) -> Optional[bytes]:
        """Return the zip bytes for ``day``, or None if the session had no file."""
        cache_path = self._cache_path(day)
        if not force and os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
            with open(cache_path, "rb") as handle:
                return handle.read()
        if not force and os.path.isfile(self._missing_marker(day)):
            return None

        url = ARCHIVE_URL.format(yyyymmdd=f"{day:%Y%m%d}")
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.get(url, timeout=self.request_timeout)
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning(
                    "bhavcopy %s attempt %d/%d: %s", day, attempt, MAX_ATTEMPTS, exc
                )
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 404:
                if self._may_still_be_published(day):
                    LOGGER.info(
                        "No bhavcopy for %s yet -- NSE publishes after ~18:00 IST. "
                        "Scanning the previous session instead.", day,
                    )
                else:
                    LOGGER.debug("No bhavcopy for %s (holiday)", day)
                    os.makedirs(self.cache_dir, exist_ok=True)
                    open(self._missing_marker(day), "w").close()
                return None
            if response.ok and response.content[:2] == b"PK":
                os.makedirs(self.cache_dir, exist_ok=True)
                with open(cache_path, "wb") as handle:
                    handle.write(response.content)
                return response.content

            # A 200 that is not a zip means NSE served an interstitial.
            last_error = f"HTTP {response.status_code}, {len(response.content)} bytes"
            LOGGER.warning(
                "bhavcopy %s attempt %d/%d: %s", day, attempt, MAX_ATTEMPTS, last_error
            )
            time.sleep(2 ** attempt)

        raise BhavcopyUnavailable(f"Could not fetch bhavcopy for {day}: {last_error}")

    # -- parsing -----------------------------------------------------------
    def parse(self, payload: bytes, day: Optional[date] = None) -> SessionData:
        """Turn zip bytes into a :class:`SessionData`."""
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise BhavcopyUnavailable("Bhavcopy zip contained no CSV")
            with archive.open(names[0]) as handle:
                frame = pd.read_csv(handle, usecols=lambda c: c in USED_COLUMNS)
        return self._build_session(frame, day)

    def _build_session(self, frame: pd.DataFrame, day: Optional[date]) -> SessionData:
        trade_date = day or _to_date(frame["TradDt"].iloc[0])
        frame = frame.copy()
        frame["TckrSymb"] = frame["TckrSymb"].astype(str).str.upper().str.strip()
        for column in (
            "StrkPric", "ClsPric", "PrvsClsgPric", "UndrlygPric",
            "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal",
            "NewBrdLotQty",
        ):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

        options = frame[frame["FinInstrmTp"].isin(OPTION_TYPES)]
        futures = frame[frame["FinInstrmTp"].isin(FUTURE_TYPES)]

        rows = self._build_rows(options, trade_date)
        contexts = self._build_contexts(options, futures, trade_date)
        LOGGER.info(
            "%s: %d option contracts across %d underlyings",
            trade_date, len(rows), len(contexts),
        )
        return SessionData(trade_date=trade_date, rows=rows, contexts=contexts)

    @staticmethod
    def _build_rows(options: pd.DataFrame, trade_date: date) -> List[OptionRow]:
        """Vectorised on purpose.

        Parsing ``XpryDt`` per row with ``pd.to_datetime`` cost ~5.5s per
        session, which is fine once and ruinous over a 500-session backtest.
        Converting the columns once up front and then walking plain numpy
        arrays takes it to well under a second.
        """
        if options.empty:
            return []

        usable = options[options["OptnTp"].isin(("CE", "PE"))]
        usable = usable[(usable["NewBrdLotQty"] > 0) & (usable["StrkPric"] > 0)]
        if usable.empty:
            return []

        expiries = pd.to_datetime(usable["XpryDt"], errors="coerce")
        keep = expiries.notna()
        if not keep.all():
            usable, expiries = usable[keep], expiries[keep]

        symbols = usable["TckrSymb"].to_numpy()
        option_types = usable["OptnTp"].to_numpy()
        strikes = usable["StrkPric"].to_numpy(dtype=float)
        expiry_dates = [stamp.date() for stamp in expiries]
        lots = usable["NewBrdLotQty"].to_numpy(dtype=float)
        oi = usable["OpnIntrst"].fillna(0.0).to_numpy(dtype=float)
        doi = usable["ChngInOpnIntrst"].fillna(0.0).to_numpy(dtype=float)
        volume = usable["TtlTradgVol"].fillna(0.0).to_numpy(dtype=float)
        turnover = usable["TtlTrfVal"].fillna(0.0).to_numpy(dtype=float)
        close = usable["ClsPric"].fillna(0.0).to_numpy(dtype=float)
        open_price = usable["OpnPric"].fillna(0.0).to_numpy(dtype=float)
        prev_close = usable["PrvsClsgPric"].fillna(0.0).to_numpy(dtype=float)
        underlying = usable["UndrlygPric"].fillna(0.0).to_numpy(dtype=float)

        call, put = OptionType.CALL, OptionType.PUT
        rows: List[OptionRow] = []
        for index in range(len(symbols)):
            rows.append(
                OptionRow(
                    key=ContractKey(
                        symbol=symbols[index],
                        expiry=expiry_dates[index],
                        strike=strikes[index],
                        option_type=call if option_types[index] == "CE" else put,
                    ),
                    trade_date=trade_date,
                    lot_size=int(lots[index]),
                    oi_units=oi[index],
                    delta_oi_units=doi[index],
                    volume_lots=volume[index],
                    turnover=turnover[index],
                    close=close[index],
                    open_price=open_price[index],
                    prev_close=prev_close[index],
                    underlying=underlying[index],
                )
            )
        return rows

    @staticmethod
    def _build_contexts(
        options: pd.DataFrame, futures: pd.DataFrame, trade_date: date
    ) -> Dict[str, UnderlyingContext]:
        contexts: Dict[str, UnderlyingContext] = {}
        if options.empty:
            return contexts

        options = options.assign(
            _prev_oi=options["OpnIntrst"] - options["ChngInOpnIntrst"]
        )
        calls = options[options["OptnTp"] == "CE"]
        puts = options[options["OptnTp"] == "PE"]

        call_oi = calls.groupby("TckrSymb")["OpnIntrst"].sum()
        put_oi = puts.groupby("TckrSymb")["OpnIntrst"].sum()
        prev_call_oi = calls.groupby("TckrSymb")["_prev_oi"].sum()
        prev_put_oi = puts.groupby("TckrSymb")["_prev_oi"].sum()
        # UndrlygPric is repeated on every contract of a symbol; take the max
        # so a stray zero on an untraded far contract cannot win.
        spot = options.groupby("TckrSymb")["UndrlygPric"].max()

        # The exchange already says which underlyings are indices, via the
        # instrument type (IDO = index option). Trusting that beats a name
        # list, which quietly misses anything newly listed.
        index_symbols = set(
            options.loc[options["FinInstrmTp"] == INDEX_OPTION, "TckrSymb"].unique()
        )

        # Futures: use the nearest expiry, which is the liquid one.
        futures_by_symbol: Dict[str, pd.Series] = {}
        if not futures.empty:
            ordered = futures.sort_values("XpryDt")
            for symbol, group in ordered.groupby("TckrSymb"):
                futures_by_symbol[str(symbol).upper()] = group.iloc[0]

        for symbol in spot.index:
            future = futures_by_symbol.get(symbol)
            contexts[symbol] = UnderlyingContext(
                symbol=symbol,
                trade_date=trade_date,
                spot=float(spot.get(symbol, 0.0) or 0.0),
                is_index=symbol in index_symbols,
                total_call_oi=float(call_oi.get(symbol, 0.0) or 0.0),
                total_put_oi=float(put_oi.get(symbol, 0.0) or 0.0),
                prev_total_call_oi=float(prev_call_oi.get(symbol, 0.0) or 0.0),
                prev_total_put_oi=float(prev_put_oi.get(symbol, 0.0) or 0.0),
                futures_oi=float(getattr(future, "OpnIntrst", 0.0) or 0.0) if future is not None else 0.0,
                futures_delta_oi=float(getattr(future, "ChngInOpnIntrst", 0.0) or 0.0) if future is not None else 0.0,
                futures_close=float(getattr(future, "ClsPric", 0.0) or 0.0) if future is not None else 0.0,
                futures_prev_close=float(getattr(future, "PrvsClsgPric", 0.0) or 0.0) if future is not None else 0.0,
            )
        return contexts

    # -- public API --------------------------------------------------------
    def load(self, day: date, force: bool = False) -> Optional[SessionData]:
        """Session data for ``day``, or None when the market was shut."""
        if not force and day in self._memo:
            self._memo.move_to_end(day)
            return self._memo[day]
        payload = self.fetch_raw(day, force=force)
        if payload is None:
            return None
        session = self.parse(payload, day=day)
        self._memo[day] = session
        self._memo.move_to_end(day)
        while len(self._memo) > self.memo_limit:
            self._memo.popitem(last=False)
        return session

    def latest(self, as_of: Optional[date] = None, max_lookback: int = 10) -> SessionData:
        """The most recent published session at or before ``as_of``.

        NSE publishes the file after the close, so on a trading day before
        ~18:00 IST the newest available session is the previous one. Walking
        backwards handles that, plus weekends and holidays, without a
        hardcoded holiday calendar.
        """
        cursor = as_of or date.today()
        for _ in range(max_lookback):
            session = self.load(cursor)
            if session is not None and session.rows:
                return session
            cursor -= timedelta(days=1)
        raise BhavcopyUnavailable(
            f"No F&O bhavcopy found in the {max_lookback} days before {as_of or date.today()}"
        )

    def load_range(
        self, start: date, end: date, progress_every: int = 25
    ) -> List[SessionData]:
        """Every published session in ``[start, end]``, oldest first.

        Materialises the whole range, so only use it for short windows --
        :meth:`iter_range` is the streaming version the backtest uses.
        """
        return list(self.iter_range(start, end, progress_every=progress_every))

    def iter_range(
        self, start: date, end: date, progress_every: int = 25
    ) -> "Iterable[SessionData]":
        """Yield each published session in ``[start, end]``, oldest first.

        Streaming rather than returning a list: a two-year range is ~530
        sessions of ~35k contracts each, which does not fit in memory at once.
        """
        cursor, checked, yielded = start, 0, 0
        while cursor <= end:
            if cursor.weekday() < 5:  # never even ask about a weekend
                try:
                    session = self.load(cursor)
                except BhavcopyUnavailable as exc:
                    LOGGER.warning("Skipping %s: %s", cursor, exc)
                    session = None
                if session is not None and session.rows:
                    yielded += 1
                    yield session
                checked += 1
                if progress_every and checked % progress_every == 0:
                    LOGGER.info("Loaded %d sessions (at %s)", yielded, cursor)
            cursor += timedelta(days=1)
        LOGGER.info("Streamed %d trading sessions from %s to %s", yielded, start, end)

    def previous_session(self, day: date, max_lookback: int = 10) -> Optional[SessionData]:
        """The session immediately before ``day``, if it is fetchable."""
        cursor = day - timedelta(days=1)
        for _ in range(max_lookback):
            if cursor.weekday() < 5:
                session = self.load(cursor)
                if session is not None and session.rows:
                    return session
            cursor -= timedelta(days=1)
        return None
