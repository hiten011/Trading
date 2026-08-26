"""NSE's live option-chain endpoint, for intraday scanning.

Corrected after being wrong about this for a while: an earlier version of this
module claimed ``https://www.nseindia.com/api/option-chain-v3`` was throttled
to an empty body (``{}``) for non-residential IPs. That conclusion was drawn
from calling it with a warmed cookie jar and correct headers but *no*
``expiry`` query parameter -- and it turns out the endpoint requires one
explicitly; without it, NSE returns ``{}`` regardless of IP or headers. Passing
one resolved from ``/api/option-chain-contract-info`` (called first, same as
the ``jugaad-data`` library does) returns real, current data from this same
environment. Verified independently of any third-party library: plain
``requests``, this project's own cookie warm-up, no bot-evasion tricks --
the fix is the missing parameter, not the network path. Cross-checked against
the bhavcopy archive too: the live feed's "previous OI" baseline for a
contract matched bhavcopy's most recent published closing OI for that same
contract exactly, which a stale cache or fabricated response could not do.

So: **this now works**, including from a datacenter IP, and is a genuine route
to intraday OI with no broker account needed. It is still not the default and
the backtest still does not use it, for a different reason than before: it is
a live snapshot with no historical archive, so it cannot replay the past the
way bhavcopy can, and a signal's thresholds should be tuned and backtested at
the resolution they will run at (see docs/OI_SCANNER.md on why the shipped
percentage bands are not assumed to transfer to an intraday cadence untested).

A broker feed (Kite Connect, Upstox, Angel One, Dhan) remains the more robust
option for a production intraday deployment -- this endpoint is still an
undocumented website API NSE could change without notice -- but it is no
longer accurate to call it broken. :class:`NseLiveSource` deliberately shares
the :class:`~custom.oi.sources.bhavcopy.SessionData` shape so a broker adapter
can be dropped in beside it without the scanner changing at all.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional

import requests

from custom.oi.models import ContractKey, OptionRow, OptionType, UnderlyingContext
from custom.oi.sources.bhavcopy import SessionData

LOGGER = logging.getLogger("custom.oi.nselive")

HOME_URL = "https://www.nseindia.com/option-chain"
CONTRACT_INFO_URL = "https://www.nseindia.com/api/option-chain-contract-info"
OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-v3"
MASTER_QUOTE_URL = "https://www.nseindia.com/api/master-quote"
DEFAULT_MAX_EXPIRIES = 2  # matches OISettings.max_expiries -- the nearest N series

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


class OptionChainUnavailable(RuntimeError):
    """NSE returned nothing usable for a symbol."""


_EXPIRY_FORMATS = ("%d-%m-%Y", "%d-%b-%Y")


def _parse_expiry(entry: dict) -> Optional[date]:
    """Pull the expiry date out of one option-chain-v3 row.

    The confirmed real shape is a row-level ``expiryDates`` key (singular
    value despite the plural name) in ``DD-MM-YYYY``, e.g. ``"29-09-2026"`` --
    not ``expiryDate``, which does not exist at the row level and made every
    row silently unparseable before this was caught. The same value is
    duplicated inside each ``CE``/``PE`` leg under the differently-spelled
    ``expiryDate``, kept here as a fallback in case a row's top-level key is
    ever missing. ``%d-%b-%Y`` (month abbreviated) is tried last for older
    NSE response variants that used it.
    """
    candidates = [entry.get("expiryDates")]
    for leg in (entry.get("CE"), entry.get("PE")):
        if leg:
            candidates.append(leg.get("expiryDate"))

    for raw in candidates:
        if not raw:
            continue
        for fmt in _EXPIRY_FORMATS:
            try:
                return datetime.strptime(str(raw), fmt).date()
            except ValueError:
                continue
    return None


class NseLiveSource:
    """Intraday option-chain snapshots straight from NSE's website API.

    Args:
        polite_delay: Seconds between symbol requests. NSE tightens the screws
            quickly; scanning 215 symbols in a tight loop is the fastest way
            to get an IP blocked outright.
    """

    name = "nselive"
    resolution = "snapshot"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        polite_delay: float = 0.7,
        request_timeout: int = 30,
        max_expiries: int = DEFAULT_MAX_EXPIRIES,
    ) -> None:
        self.polite_delay = polite_delay
        self.request_timeout = request_timeout
        self.max_expiries = max(max_expiries, 1)
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": HOME_URL,
            }
        )
        self._warmed = False

    def _warm_up(self) -> None:
        """Pick up the cookies NSE's API checks for."""
        if self._warmed:
            return
        try:
            self._session.get(HOME_URL, timeout=self.request_timeout)
            self._warmed = True
        except requests.RequestException as exc:
            LOGGER.warning("NSE warm-up failed: %s", exc)

    def _get_json(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        self._warm_up()
        try:
            # Always via requests' own `params=`, never hand-built query
            # strings: several genuinely liquid F&O names contain characters
            # a naive f-string would corrupt -- M&M (Mahindra & Mahindra, a
            # top-tier F&O name by volume) turns into `symbol=M` plus a
            # bogus bare `M` parameter if the `&` is not percent-encoded,
            # and NSE silently answers as if the symbol does not exist.
            # Confirmed against the real endpoint, not just reasoned about.
            response = self._session.get(url, params=params, timeout=self.request_timeout)
        except requests.RequestException as exc:
            LOGGER.warning("GET %s failed: %s", url, exc)
            return None
        if not response.ok:
            LOGGER.warning("GET %s -> HTTP %s", url, response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            LOGGER.warning("GET %s returned non-JSON (%d bytes)", url, len(response.content))
            return None
        if not payload:
            # option-chain-v3 answers "{}" for a missing/invalid `expiry`
            # parameter, among other malformed requests -- see this module's
            # docstring. Anything reaching here already passed a resolved
            # expiry, so an empty body at this point means the symbol itself
            # has nothing on offer (delisted from F&O, wrong name, etc).
            LOGGER.warning("GET %s (params=%s) returned an empty body", url, params)
            return None
        return payload

    def _resolve_expiries(self, symbol: str) -> List[str]:
        """The nearest ``max_expiries`` expiry strings NSE has on file.

        ``option-chain-v3`` answers exactly one expiry per call and returns
        nothing at all if the parameter is omitted, so this has to run first.
        """
        payload = self._get_json(CONTRACT_INFO_URL, params={"symbol": symbol})
        expiries = (payload or {}).get("expiryDates") or []
        return expiries[: self.max_expiries]

    def fetch_symbol(self, symbol: str) -> Optional[dict]:
        """Merged option-chain JSON for one symbol across its nearest expiries."""
        symbol = symbol.upper().strip()
        is_index = symbol in INDEX_SYMBOLS
        kind = "Indices" if is_index else "Equity"

        expiries = self._resolve_expiries(symbol)
        if not expiries:
            return None

        merged_rows: List[dict] = []
        underlying_value = 0.0
        for expiry in expiries:
            payload = self._get_json(
                OPTION_CHAIN_URL, params={"type": kind, "symbol": symbol, "expiry": expiry}
            )
            records = (payload or {}).get("records", {})
            rows = records.get("data")
            if not rows:
                continue
            merged_rows.extend(rows)
            underlying_value = float(records.get("underlyingValue") or underlying_value)

        if not merged_rows:
            return None
        return {"records": {"underlyingValue": underlying_value, "data": merged_rows}}

    def fno_symbols(self) -> List[str]:
        """The F&O underlying list NSE publishes."""
        payload = self._get_json(MASTER_QUOTE_URL)
        if isinstance(payload, list):
            return sorted(str(item).upper() for item in payload)
        return []

    # -- normalisation -----------------------------------------------------
    @staticmethod
    def _parse_symbol(payload: dict, symbol: str, trade_date: date) -> SessionData:
        """Map NSE's option-chain JSON onto the bhavcopy-shaped types.

        NSE gives ``changeinOpenInterest`` against the previous *session*,
        same as bhavcopy, so the derived previous OI lines up. It reports OI
        in **lots** here (unlike bhavcopy's units), so a lot size of 1 keeps
        :attr:`OptionRow.oi_lots` correct without inventing a conversion.
        """
        records = payload.get("records", {})
        spot = float(records.get("underlyingValue") or 0.0)
        rows: List[OptionRow] = []
        call_oi = put_oi = prev_call_oi = prev_put_oi = 0.0

        for entry in records.get("data", []):
            expiry = _parse_expiry(entry)
            if expiry is None:
                continue
            strike = float(entry.get("strikePrice") or 0.0)
            if strike <= 0:
                continue
            for side, option_type in (("CE", OptionType.CALL), ("PE", OptionType.PUT)):
                leg = entry.get(side)
                if not leg:
                    continue
                oi = float(leg.get("openInterest") or 0.0)
                delta_oi = float(leg.get("changeinOpenInterest") or 0.0)
                close = float(leg.get("lastPrice") or 0.0)
                change = float(leg.get("change") or 0.0)
                if option_type is OptionType.CALL:
                    call_oi += oi
                    prev_call_oi += oi - delta_oi
                else:
                    put_oi += oi
                    prev_put_oi += oi - delta_oi
                rows.append(
                    OptionRow(
                        key=ContractKey(symbol, expiry, strike, option_type),
                        trade_date=trade_date,
                        lot_size=1,  # this feed already quotes OI in lots
                        oi_units=oi,
                        delta_oi_units=delta_oi,
                        volume_lots=float(leg.get("totalTradedVolume") or 0.0),
                        turnover=0.0,
                        close=close,
                        prev_close=close - change,
                        open_price=float(leg.get("openPrice") or 0.0),
                        underlying=spot,
                    )
                )

        context = UnderlyingContext(
            symbol=symbol,
            trade_date=trade_date,
            spot=spot,
            total_call_oi=call_oi,
            total_put_oi=put_oi,
            prev_total_call_oi=prev_call_oi,
            prev_total_put_oi=prev_put_oi,
        )
        return SessionData(trade_date=trade_date, rows=rows, contexts={symbol: context})

    def load_symbols(self, symbols: Iterable[str], as_of: Optional[date] = None) -> SessionData:
        """Snapshot several symbols into one :class:`SessionData`."""
        trade_date = as_of or date.today()
        combined = SessionData(trade_date=trade_date)
        failures: List[str] = []
        for index, symbol in enumerate(symbols):
            payload = self.fetch_symbol(symbol)
            if payload is None:
                failures.append(symbol)
            else:
                part = self._parse_symbol(payload, symbol.upper(), trade_date)
                combined.rows.extend(part.rows)
                combined.contexts.update(part.contexts)
            if self.polite_delay and index:
                time.sleep(self.polite_delay)
        if failures:
            LOGGER.warning(
                "NSE returned nothing for %d/%d symbols (e.g. %s)",
                len(failures), len(failures) + len(combined.contexts), ", ".join(failures[:5]),
            )
        if not combined.rows:
            raise OptionChainUnavailable(
                "NSE's live option-chain API returned no data for any symbol. "
                "This used to mean the endpoint was blocked, but it usually means "
                "something more mundane now -- a bad symbol list, a market holiday "
                "with nothing to quote, or NSE's website having a bad day. Use the "
                "bhavcopy source for anything unattended. See this module's docstring."
            )
        return combined

    def latest(self, as_of: Optional[date] = None, symbols: Optional[Iterable[str]] = None) -> SessionData:
        return self.load_symbols(symbols or self.fno_symbols(), as_of=as_of)

    # -- interface parity with BhavcopySource -------------------------------
    # custom.oi.cli.run_once() is written against BhavcopySource's date-
    # addressable archive (load(day), previous_session(day)) and calls both
    # unconditionally. A live snapshot cannot honour either request the same
    # way -- it only ever has right now -- so these exist to fail with an
    # explanation instead of an AttributeError the first time OI_SOURCE=nselive
    # is actually run unattended.
    def load(
        self, day: date, force: bool = False, symbols: Optional[Iterable[str]] = None
    ) -> Optional[SessionData]:
        if day is not None and day != date.today():
            raise NotImplementedError(
                f"NseLiveSource has no historical archive -- it cannot load {day}, "
                "only today. Use the bhavcopy source for a specific past session, "
                "or omit --date to scan the live snapshot now."
            )
        return self.latest(as_of=day, symbols=symbols)

    def previous_session(self, day: date, max_lookback: int = 10) -> Optional[SessionData]:
        """No separate lookup: a live snapshot already carries its own
        change-vs-this-morning figures in ``changeinOpenInterest``, which is
        what :meth:`_parse_symbol` reads. There is nothing else to fetch."""
        return None
