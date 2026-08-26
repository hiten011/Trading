"""NSE's live option-chain endpoint, for intraday scanning.

Status, measured rather than assumed: from this environment (and from most
cloud/datacentre hosts) ``https://www.nseindia.com/api/option-chain-v3`` and
its predecessors answer **HTTP 200 with an empty JSON body** (``{}``), with
and without a warmed-up cookie jar, correct ``Referer``, browser
``User-Agent`` and ``Accept-Language``. Other NSE JSON endpoints on the same
host and cookie jar (``/api/master-quote``) return real data at the same
moment, so this is endpoint-specific throttling of the most-scraped path, not
a blanket block or a session problem.

So: this source is real code, exercised by tests, and will work from a
network NSE does not degrade (a home/office ISP in India, typically). It is
**not** the default and the backtest never uses it, because a data feed that
returns nothing from a server is not a foundation for an unattended alerting
service.

The supported route to genuine intraday OI is a broker feed -- Kite Connect,
Upstox, Angel One, Dhan -- all of which need an account and most of which
need a paid API subscription. :class:`NseLiveSource` deliberately shares the
:class:`~custom.oi.sources.bhavcopy.SessionData` shape so a broker adapter
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
EQUITY_URL = "https://www.nseindia.com/api/option-chain-v3?type=Equity&symbol={symbol}"
INDEX_URL = "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol={symbol}"
LEGACY_EQUITY_URL = "https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
LEGACY_INDEX_URL = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
MASTER_QUOTE_URL = "https://www.nseindia.com/api/master-quote"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


class OptionChainUnavailable(RuntimeError):
    """NSE returned nothing usable for a symbol."""


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
    ) -> None:
        self.polite_delay = polite_delay
        self.request_timeout = request_timeout
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

    def _get_json(self, url: str) -> Optional[dict]:
        self._warm_up()
        try:
            response = self._session.get(url, timeout=self.request_timeout)
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
            # The documented failure mode: 200 with "{}".
            LOGGER.warning("GET %s returned an empty body (NSE is throttling)", url)
            return None
        return payload

    def fetch_symbol(self, symbol: str) -> Optional[dict]:
        """Raw option-chain JSON for one symbol, trying v3 then the legacy path."""
        symbol = symbol.upper().strip()
        is_index = symbol in INDEX_SYMBOLS
        candidates = (
            [INDEX_URL, LEGACY_INDEX_URL] if is_index else [EQUITY_URL, LEGACY_EQUITY_URL]
        )
        for template in candidates:
            payload = self._get_json(template.format(symbol=symbol))
            if payload and payload.get("records", {}).get("data"):
                return payload
        return None

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
            expiry_raw = entry.get("expiryDate")
            try:
                expiry = datetime.strptime(str(expiry_raw), "%d-%b-%Y").date()
            except (TypeError, ValueError):
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
                "NSE's live option-chain API returned no data for any symbol. This is "
                "its normal behaviour for non-residential IPs -- use the bhavcopy "
                "source, or plug in a broker feed. See this module's docstring."
            )
        return combined

    def latest(self, as_of: Optional[date] = None, symbols: Optional[Iterable[str]] = None) -> SessionData:
        return self.load_symbols(symbols or self.fno_symbols(), as_of=as_of)
