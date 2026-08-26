"""Value types shared across the OI scanner.

Two units traps live in NSE's F&O bhavcopy and both are handled here rather
than at every call site:

* ``OpnIntrst`` / ``ChngInOpnIntrst`` are quoted in **units** (shares), so a
  raw "OI > 10,000" floor means wildly different things for IDEA (lot size
  71,475) and RELIANCE (lot size 500). Everything downstream compares
  :attr:`OptionRow.oi_lots` instead.
* ``TtlTradgVol`` is quoted in **lots**, unlike OI. Verified against
  ``TtlTrfVal``: turnover / (volume x lot size) reproduces the underlying
  price, turnover / volume does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class OptionType(str, Enum):
    CALL = "CE"
    PUT = "PE"


class Tier(str, Enum):
    """Severity band a hit falls into. Ordered weakest to strongest."""

    NONE = "NONE"
    WATCH = "WATCH"
    STRONG = "STRONG"
    EXTREME = "EXTREME"

    @property
    def rank(self) -> int:
        return {"NONE": 0, "WATCH": 1, "STRONG": 2, "EXTREME": 3}[self.value]

    @property
    def emoji(self) -> str:
        return {"NONE": "", "WATCH": "👀", "STRONG": "🔥", "EXTREME": "🚨"}[self.value]


class Buildup(str, Enum):
    """What the OI move plus the price move imply about positioning.

    The classic four-box table (long buildup / short buildup / short covering /
    long unwinding) is written for *futures*, where price and position have one
    unambiguous direction. Applied to an option contract it describes activity
    in that contract, which is why :class:`Bias` exists separately -- a call
    being written and a put being written are both "short buildup" here but
    point at opposite outcomes for the underlying.
    """

    LONG_BUILDUP = "LONG_BUILDUP"
    SHORT_BUILDUP = "SHORT_BUILDUP"
    SHORT_COVERING = "SHORT_COVERING"
    LONG_UNWINDING = "LONG_UNWINDING"
    UNKNOWN = "UNKNOWN"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class Bias(str, Enum):
    """Directional read on the *underlying*, once CE/PE is folded in."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    @property
    def emoji(self) -> str:
        return {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}[self.value]


@dataclass(frozen=True)
class ContractKey:
    """Identity of one option contract, stable across sessions."""

    symbol: str
    expiry: date
    strike: float
    option_type: OptionType

    def __str__(self) -> str:
        return (
            f"{self.symbol} {self.strike:g} {self.option_type.value} "
            f"{self.expiry:%d%b%y}".upper()
        )

    @property
    def slug(self) -> str:
        """Filesystem/DB-safe identifier."""
        return (
            f"{self.symbol}|{self.expiry:%Y-%m-%d}|{self.strike:g}|"
            f"{self.option_type.value}"
        )


@dataclass
class OptionRow:
    """One option contract on one trading session."""

    key: ContractKey
    trade_date: date
    lot_size: int

    oi_units: float
    delta_oi_units: float
    volume_lots: float
    turnover: float

    close: float
    prev_close: float
    underlying: float
    open_price: float = 0.0

    # --- derived -----------------------------------------------------------
    @property
    def prev_oi_units(self) -> float:
        """Yesterday's OI. Exact: verified to reproduce the prior session's
        ``OpnIntrst`` on 100% of ~35k contracts."""
        return self.oi_units - self.delta_oi_units

    @property
    def oi_lots(self) -> float:
        return self.oi_units / self.lot_size if self.lot_size else 0.0

    @property
    def prev_oi_lots(self) -> float:
        return self.prev_oi_units / self.lot_size if self.lot_size else 0.0

    @property
    def delta_oi_lots(self) -> float:
        return self.delta_oi_units / self.lot_size if self.lot_size else 0.0

    @property
    def is_new_contract(self) -> bool:
        """No OI yesterday: the % change is undefined, not infinite.

        The Pine original divided by zero here and its guard returned 0.0,
        silently discarding exactly the contracts that went from nothing to
        something -- often the most interesting case.
        """
        return self.prev_oi_units <= 0

    @property
    def oi_pct_change(self) -> float:
        """Percent change in OI against the previous session.

        Mirrors the Pine ``(oi - oi[1]) / oi[1] * 100``. Returns ``inf`` for a
        contract with no prior OI so callers can treat it deliberately.
        """
        prev = self.prev_oi_units
        if prev <= 0:
            return float("inf") if self.oi_units > 0 else 0.0
        return (self.oi_units - prev) / prev * 100.0

    @property
    def price_basis(self) -> str:
        """Which reference price the direction read is computed against.

        ``PrvsClsgPric`` is only a *traded* price when the contract actually
        had a position open. For a listed-but-dormant strike NSE carries the
        last theoretical value forward, which can be wildly stale: GODREJCP's
        930 call sat at a carried-forward 103.25 with zero open interest, and
        the moment the underlying gapped 11% lower it printed 14.20 -- an
        apparent -86% that describes the *underlying's* gap, not any flow in
        that option. Reading buildup off that number invents a signal.

        So the previous close is trusted only when there was open interest to
        justify it; otherwise the day's own open is the honest reference,
        which is exactly the right question anyway: as this position was being
        built today, did the price rise or fall?
        """
        if self.prev_oi_units > 0 and self.prev_close > 0:
            return "prev_close"
        if self.open_price > 0:
            return "open"
        return "none"

    @property
    def price_reference(self) -> float:
        basis = self.price_basis
        if basis == "prev_close":
            return self.prev_close
        if basis == "open":
            return self.open_price
        return 0.0

    @property
    def price_pct_change(self) -> float:
        """Price move over the window the OI change describes.

        Day-over-day for an established contract, intraday for one whose
        position was built from nothing today. See :attr:`price_basis`.
        """
        reference = self.price_reference
        if reference <= 0:
            return 0.0
        return (self.close - reference) / reference * 100.0

    @property
    def notional(self) -> float:
        """Rupee value of the day's traded volume in this contract."""
        return self.volume_lots * self.lot_size * self.underlying

    @property
    def moneyness_pct(self) -> float:
        """Distance of the strike from spot, in percent of spot."""
        if self.underlying <= 0:
            return float("inf")
        return abs(self.key.strike - self.underlying) / self.underlying * 100.0

    def days_to_expiry(self, as_of: Optional[date] = None) -> int:
        return (self.key.expiry - (as_of or self.trade_date)).days


@dataclass
class UnderlyingContext:
    """Symbol-level aggregates for one session, used to score a contract
    against its own symbol rather than against a global constant."""

    symbol: str
    trade_date: date
    spot: float
    prev_spot: float = 0.0

    total_call_oi: float = 0.0
    total_put_oi: float = 0.0
    prev_total_call_oi: float = 0.0
    prev_total_put_oi: float = 0.0

    # Futures leg, when the symbol has one. This is where the four-box
    # buildup table is actually valid, so it is carried as confirmation.
    futures_oi: float = 0.0
    futures_delta_oi: float = 0.0
    futures_close: float = 0.0
    futures_prev_close: float = 0.0

    @property
    def spot_pct_change(self) -> float:
        if self.prev_spot <= 0:
            return 0.0
        return (self.spot - self.prev_spot) / self.prev_spot * 100.0

    @property
    def total_oi(self) -> float:
        return self.total_call_oi + self.total_put_oi

    @property
    def pcr(self) -> float:
        """Put-call OI ratio. >1 is conventionally read as bullish-leaning."""
        return self.total_put_oi / self.total_call_oi if self.total_call_oi else 0.0

    @property
    def prev_pcr(self) -> float:
        if not self.prev_total_call_oi:
            return 0.0
        return self.prev_total_put_oi / self.prev_total_call_oi

    @property
    def futures_price_pct_change(self) -> float:
        if self.futures_prev_close <= 0:
            return 0.0
        return (self.futures_close - self.futures_prev_close) / self.futures_prev_close * 100.0

    @property
    def futures_buildup(self) -> Buildup:
        """The genuine four-box read, on the instrument it was written for."""
        if not self.futures_oi or not self.futures_close:
            return Buildup.UNKNOWN
        return classify_buildup(self.futures_delta_oi, self.futures_price_pct_change)


def classify_buildup(delta_oi: float, price_pct_change: float) -> Buildup:
    """The four-box OI/price table.

    ``price up + OI up`` is fresh longs, ``price down + OI up`` is fresh
    shorts, ``price up + OI down`` is shorts covering, ``price down + OI down``
    is longs unwinding.
    """
    if delta_oi == 0 or price_pct_change == 0:
        return Buildup.UNKNOWN
    if delta_oi > 0:
        return Buildup.LONG_BUILDUP if price_pct_change > 0 else Buildup.SHORT_BUILDUP
    return Buildup.SHORT_COVERING if price_pct_change > 0 else Buildup.LONG_UNWINDING


def option_bias(option_type: OptionType, buildup: Buildup) -> Bias:
    """Translate contract-level activity into a view on the underlying.

    Buying calls and writing puts are both bullish; writing calls and buying
    puts are both bearish. Unwinding is the mirror of the buildup that
    preceded it, and is treated as the weaker, opposite signal.
    """
    is_call = option_type is OptionType.CALL
    if buildup is Buildup.LONG_BUILDUP:  # fresh buying of this option
        return Bias.BULLISH if is_call else Bias.BEARISH
    if buildup is Buildup.SHORT_BUILDUP:  # fresh writing of this option
        return Bias.BEARISH if is_call else Bias.BULLISH
    if buildup is Buildup.SHORT_COVERING:  # writers buying back
        return Bias.BULLISH if is_call else Bias.BEARISH
    if buildup is Buildup.LONG_UNWINDING:  # holders selling out
        return Bias.BEARISH if is_call else Bias.BULLISH
    return Bias.NEUTRAL


@dataclass
class OIAlert:
    """One contract that cleared every gate, ready to be sent."""

    row: OptionRow
    context: UnderlyingContext
    tier: Tier
    buildup: Buildup
    bias: Bias
    score: float
    oi_pct_change: float
    share_of_symbol_oi: float = 0.0
    z_score: float = float("nan")
    reasons: list = field(default_factory=list)

    @property
    def key(self) -> ContractKey:
        return self.row.key

    @property
    def symbol(self) -> str:
        return self.row.key.symbol

    @property
    def headline(self) -> str:
        """e.g. ``RELIANCE 3000 CALL LONG BUILDUP OI +1340%``."""
        side = "CALL" if self.row.key.option_type is OptionType.CALL else "PUT"
        pct = "NEW" if self.row.is_new_contract else f"{self.oi_pct_change:+,.0f}%"
        return (
            f"{self.symbol} {self.row.key.strike:g} {side} "
            f"{self.buildup.label.upper()} OI {pct}"
        )

    def as_row(self) -> dict:
        """Flat mapping for CSV export and the alert table."""
        return {
            "Symbol": self.symbol,
            "Strike": self.row.key.strike,
            "Type": self.row.key.option_type.value,
            "Expiry": self.row.key.expiry.isoformat(),
            "OI%": None if self.row.is_new_contract else round(self.oi_pct_change, 1),
            "OI(lots)": round(self.row.oi_lots),
            "PrevOI(lots)": round(self.row.prev_oi_lots),
            "Vol(lots)": round(self.row.volume_lots),
            "Px%": round(self.row.price_pct_change, 1),
            "PxBasis": self.row.price_basis,
            "Spot": round(self.row.underlying, 2),
            "Spot%": round(self.context.spot_pct_change, 2),
            "Moneyness%": round(self.row.moneyness_pct, 1),
            "DTE": self.row.days_to_expiry(),
            "Buildup": self.buildup.label,
            "Bias": self.bias.value,
            "Tier": self.tier.value,
            "ShareOfOI%": round(self.share_of_symbol_oi * 100, 2),
            "Z": None if self.z_score != self.z_score else round(self.z_score, 2),
            "FutBuildup": self.context.futures_buildup.label,
            "PCR": round(self.context.pcr, 2),
            "Score": round(self.score, 1),
            "Date": self.row.trade_date.isoformat(),
        }
