"""Did the signal actually predict anything?

This module exists to answer one question honestly: when a contract's open
interest blew past the threshold on session D, what did the *underlying* do
afterwards, and was that different from what it would have done anyway?

Methodology, and the reasons for it:

**Entry timing.** NSE publishes the F&O bhavcopy after the close, around
18:00-19:00 IST. A scan of session D therefore cannot be acted on during
session D. Entry is taken at the **close of D+1**, which is deliberately
conservative -- a real trader alerted at 18:30 on D could enter at D+1's open
and usually do better. Measuring from D's close instead would be look-ahead
bias and would flatter the results considerably, because the OI move and the
price move that caused it happen on the same day.

**Direction.** Each alert carries a bullish or bearish bias, so raw returns
are useless for scoring. Every return is *signed*: multiplied by +1 for a
bullish alert and -1 for a bearish one. A signed return above zero means the
alert pointed the right way.

**Benchmark.** A signed return of +0.4% means nothing on its own -- if every
F&O stock rose that week, a bullish-heavy signal would show a "profit" that is
just market drift. So every horizon is also computed over *every symbol on
every day in the same window*, with the same signing convention applied to the
same mix of bullish/bearish calls. That is the base rate the signal has to
beat, and the edge is the difference.

**Sample splitting.** The in-sample window is for choosing settings; the
out-of-sample window is scored once with those settings. Tuning on everything
and reporting the result is how backtests lie.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from statistics import StatisticsError, mean, median, pstdev
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from custom.oi.config import OISettings
from custom.oi.models import Bias, OIAlert, Tier
from custom.oi.scanner import ScanResult, build_pct_history, scan_session
from custom.oi.sources.bhavcopy import BhavcopySource, SessionData

LOGGER = logging.getLogger("custom.oi.backtest")

# Sessions after entry at which the position is marked to market.
DEFAULT_HORIZONS = (1, 2, 5, 10)


@dataclass
class AlertOutcome:
    """One historical alert plus what happened next."""

    trade_date: date
    symbol: str
    contract: str
    tier: Tier
    bias: Bias
    buildup: str
    oi_pct_change: float
    is_new_contract: bool
    share_of_symbol_oi: float
    volume_lots: float
    notional_cr: float
    moneyness_pct: float
    days_to_expiry: int
    entry_date: Optional[date] = None
    entry_price: float = 0.0
    # horizon (sessions after entry) -> signed return in percent
    returns: Dict[int, float] = field(default_factory=dict)
    raw_returns: Dict[int, float] = field(default_factory=dict)
    reaction_return: float = float("nan")

    @property
    def sign(self) -> int:
        if self.bias is Bias.BULLISH:
            return 1
        if self.bias is Bias.BEARISH:
            return -1
        return 0

    def as_row(self) -> dict:
        row = {
            "Date": self.trade_date.isoformat(),
            "Symbol": self.symbol,
            "Contract": self.contract,
            "Tier": self.tier.value,
            "Bias": self.bias.value,
            "Buildup": self.buildup,
            "OI%": None if self.is_new_contract else round(self.oi_pct_change, 1),
            "New": self.is_new_contract,
            "ShareOfOI%": round(self.share_of_symbol_oi * 100, 2),
            "Vol(lots)": round(self.volume_lots),
            "Notional(Cr)": round(self.notional_cr, 1),
            "Moneyness%": round(self.moneyness_pct, 1),
            "DTE": self.days_to_expiry,
            "EntryDate": self.entry_date.isoformat() if self.entry_date else None,
            "EntryPx": round(self.entry_price, 2),
            "ReactionRet%": None if math.isnan(self.reaction_return) else round(self.reaction_return, 2),
        }
        for horizon, value in sorted(self.returns.items()):
            row[f"Signed{horizon}d%"] = round(value, 2)
        return row


@dataclass
class HorizonStats:
    """Aggregate performance at one holding period."""

    horizon: int
    count: int
    hit_rate: float
    mean_return: float
    median_return: float
    stdev: float
    base_rate_mean: float
    base_rate_hit_rate: float

    @property
    def edge(self) -> float:
        """Mean signed return above the same-window benchmark."""
        return self.mean_return - self.base_rate_mean

    @property
    def hit_rate_edge(self) -> float:
        return self.hit_rate - self.base_rate_hit_rate

    @property
    def t_statistic(self) -> float:
        """How many standard errors the edge sits above zero.

        Not a p-value: alerts cluster (one news event fires eight contracts on
        the same name on the same day), so the effective sample is smaller
        than ``count`` and this overstates significance. Treat |t| < 2 as
        indistinguishable from noise and |t| slightly above 2 with suspicion.
        """
        if self.count < 2 or self.stdev <= 0:
            return float("nan")
        return self.edge / (self.stdev / math.sqrt(self.count))

    def describe(self) -> str:
        return (
            f"{self.horizon:>3}d  n={self.count:>5}  "
            f"hit={self.hit_rate * 100:5.1f}% (base {self.base_rate_hit_rate * 100:5.1f}%)  "
            f"mean={self.mean_return:+6.2f}% (base {self.base_rate_mean:+6.2f}%)  "
            f"edge={self.edge:+6.2f}%  t={self.t_statistic:+5.2f}"
        )


@dataclass
class BacktestResult:
    """Everything a run produced."""

    label: str
    start: date
    end: date
    sessions: int = 0
    outcomes: List[AlertOutcome] = field(default_factory=list)
    stats: Dict[int, HorizonStats] = field(default_factory=dict)
    alerts_per_session: float = 0.0
    sessions_with_alerts: int = 0

    def describe(self) -> str:
        lines = [
            f"=== {self.label}: {self.start} → {self.end} ===",
            f"{self.sessions} sessions, {len(self.outcomes)} alerts "
            f"({self.alerts_per_session:.1f}/session, "
            f"{self.sessions_with_alerts} sessions fired)",
        ]
        if not self.stats:
            lines.append("No scored alerts.")
            return "\n".join(lines)
        lines.append("Signed forward returns of the underlying, entry at D+1 close:")
        for horizon in sorted(self.stats):
            lines.append("  " + self.stats[horizon].describe())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Price series
# ---------------------------------------------------------------------------

class SpotSeries:
    """Underlying closing prices by symbol and session, built as we stream."""

    def __init__(self) -> None:
        self._by_symbol: Dict[str, Dict[date, float]] = defaultdict(dict)
        self._dates: List[date] = []
        self._index: Dict[date, int] = {}

    def add_session(self, session: SessionData) -> None:
        for symbol, context in session.contexts.items():
            if context.spot > 0:
                self._by_symbol[symbol][session.trade_date] = context.spot
        if session.trade_date not in self._index:
            self._index[session.trade_date] = len(self._dates)
            self._dates.append(session.trade_date)

    @property
    def dates(self) -> List[date]:
        return self._dates

    @property
    def symbols(self) -> List[str]:
        return sorted(self._by_symbol)

    def session_offset(self, day: date, offset: int) -> Optional[date]:
        """The session ``offset`` trading days after ``day``."""
        position = self._index.get(day)
        if position is None:
            return None
        target = position + offset
        if 0 <= target < len(self._dates):
            return self._dates[target]
        return None

    def price(self, symbol: str, day: date) -> Optional[float]:
        return self._by_symbol.get(symbol, {}).get(day)

    def forward_return(
        self, symbol: str, entry_day: date, horizon: int
    ) -> Optional[float]:
        """Percent change in the underlying from ``entry_day`` to ``horizon``
        sessions later. ``None`` when either end is missing."""
        entry = self.price(symbol, entry_day)
        exit_day = self.session_offset(entry_day, horizon)
        if entry is None or exit_day is None or entry <= 0:
            return None
        exit_price = self.price(symbol, exit_day)
        if exit_price is None:
            return None
        return (exit_price - entry) / entry * 100.0


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

def base_rate(
    spots: SpotSeries,
    horizons: Sequence[int],
    entry_days: Sequence[date],
    bias_mix: Dict[int, int],
    sample_symbols: Optional[Sequence[str]] = None,
) -> Dict[int, Tuple[float, float]]:
    """What an equivalent set of random picks would have returned.

    ``bias_mix`` is the alert population's split of long (+1) and short (-1)
    calls, so the benchmark is signed the same way the signal was rather than
    being a pure long-only drift number. Without this, a signal that happened
    to be 80% bearish during a falling market would look brilliant.
    """
    symbols = list(sample_symbols or spots.symbols)
    total_signed = sum(abs(count) for count in bias_mix.values()) or 1
    long_share = bias_mix.get(1, 0) / total_signed
    short_share = bias_mix.get(-1, 0) / total_signed

    results: Dict[int, Tuple[float, float]] = {}
    for horizon in horizons:
        returns: List[float] = []
        for entry_day in entry_days:
            for symbol in symbols:
                value = spots.forward_return(symbol, entry_day, horizon)
                if value is not None:
                    returns.append(value)
        if not returns:
            results[horizon] = (0.0, 0.0)
            continue
        # Signing a symmetric population by a fixed mix is equivalent to
        # scaling the mean by (long_share - short_share).
        raw_mean = mean(returns)
        signed_mean = raw_mean * (long_share - short_share)
        up_rate = sum(1 for value in returns if value > 0) / len(returns)
        signed_hit = up_rate * long_share + (1 - up_rate) * short_share
        results[horizon] = (signed_mean, signed_hit)
    return results


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def collect_alerts(
    source: BhavcopySource,
    settings: OISettings,
    start: date,
    end: date,
    spots: Optional[SpotSeries] = None,
) -> Tuple[List[OIAlert], SpotSeries, int, List[date]]:
    """Stream every session in the window, scanning each one.

    Streaming rather than loading the range up front: a two-year window is far
    too large to hold as parsed objects, and the scan only ever needs the
    current session plus the one before it.
    """
    # The live path caps a message at OI_MAX_ALERTS and OI_MAX_PER_SYMBOL so
    # a phone notification stays readable. Those caps would silently bias the
    # sample here -- they keep the highest-scoring hits, which is exactly the
    # subset whose performance is in question -- so the backtest measures the
    # uncapped signal and the caps are reported separately as a delivery
    # concern.
    settings = replace(settings, max_alerts=0, max_per_symbol=0)

    spots = spots if spots is not None else SpotSeries()
    alerts: List[OIAlert] = []
    session_dates: List[date] = []
    previous: Optional[SessionData] = None
    history_window: List[SessionData] = []
    count = 0

    for session in source.iter_range(start, end, progress_every=0):
        spots.add_session(session)
        session_dates.append(session.trade_date)
        count += 1

        history = None
        if settings.min_z_score > 0 and history_window:
            history = build_pct_history(history_window)

        result = scan_session(
            session, settings, prev_session=previous, history=history
        )
        alerts.extend(result.alerts)

        if settings.min_z_score > 0:
            history_window.append(session)
            if len(history_window) > settings.z_lookback:
                history_window.pop(0)
        previous = session
        if count % 25 == 0:
            LOGGER.info("Scanned %d sessions (%d alerts so far)", count, len(alerts))

    return alerts, spots, count, session_dates


def score_alerts(
    alerts: Sequence[OIAlert],
    spots: SpotSeries,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> List[AlertOutcome]:
    """Attach forward returns to each alert, entering at the close of D+1."""
    outcomes: List[AlertOutcome] = []
    for alert in alerts:
        row = alert.row
        outcome = AlertOutcome(
            trade_date=row.trade_date,
            symbol=alert.symbol,
            contract=str(row.key),
            tier=alert.tier,
            bias=alert.bias,
            buildup=alert.buildup.label,
            oi_pct_change=alert.oi_pct_change,
            is_new_contract=row.is_new_contract,
            share_of_symbol_oi=alert.share_of_symbol_oi,
            volume_lots=row.volume_lots,
            notional_cr=row.notional / 1e7,
            moneyness_pct=row.moneyness_pct,
            days_to_expiry=row.days_to_expiry(),
        )

        entry_day = spots.session_offset(row.trade_date, 1)
        if entry_day is None:
            outcomes.append(outcome)
            continue
        entry_price = spots.price(alert.symbol, entry_day)
        if entry_price is None:
            outcomes.append(outcome)
            continue

        outcome.entry_date = entry_day
        outcome.entry_price = entry_price

        signal_day_price = spots.price(alert.symbol, row.trade_date)
        if signal_day_price:
            reaction = (entry_price - signal_day_price) / signal_day_price * 100.0
            outcome.reaction_return = reaction * outcome.sign

        for horizon in horizons:
            value = spots.forward_return(alert.symbol, entry_day, horizon)
            if value is not None:
                outcome.raw_returns[horizon] = value
                outcome.returns[horizon] = value * outcome.sign
        outcomes.append(outcome)
    return outcomes


def summarise(
    outcomes: Sequence[AlertOutcome],
    spots: SpotSeries,
    label: str,
    start: date,
    end: date,
    sessions: int,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    benchmark_symbols: Optional[Sequence[str]] = None,
) -> BacktestResult:
    """Aggregate outcomes into per-horizon statistics against the base rate."""
    result = BacktestResult(label=label, start=start, end=end, sessions=sessions)
    result.outcomes = list(outcomes)
    if sessions:
        result.alerts_per_session = len(outcomes) / sessions
    result.sessions_with_alerts = len({o.trade_date for o in outcomes})

    scored = [o for o in outcomes if o.returns]
    if not scored:
        return result

    bias_mix: Dict[int, int] = defaultdict(int)
    for outcome in scored:
        bias_mix[outcome.sign] += 1

    entry_days = sorted({o.entry_date for o in scored if o.entry_date})
    benchmark = base_rate(
        spots, horizons, entry_days, dict(bias_mix), sample_symbols=benchmark_symbols
    )

    for horizon in horizons:
        values = [o.returns[horizon] for o in scored if horizon in o.returns]
        if not values:
            continue
        base_mean, base_hit = benchmark.get(horizon, (0.0, 0.0))
        try:
            spread = pstdev(values) if len(values) > 1 else 0.0
        except StatisticsError:
            spread = 0.0
        result.stats[horizon] = HorizonStats(
            horizon=horizon,
            count=len(values),
            hit_rate=sum(1 for v in values if v > 0) / len(values),
            mean_return=mean(values),
            median_return=median(values),
            stdev=spread,
            base_rate_mean=base_mean,
            base_rate_hit_rate=base_hit,
        )
    return result


def run_backtest(
    source: BhavcopySource,
    settings: OISettings,
    start: date,
    end: date,
    label: str = "backtest",
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    lookahead_days: int = 30,
) -> BacktestResult:
    """Scan ``[start, end]`` and score every alert it produced.

    Args:
        lookahead_days: Extra calendar days streamed past ``end`` purely to
            price the exits of alerts fired near the end of the window.
            Without it the last few weeks of alerts score as unmeasurable.
    """
    from datetime import timedelta

    alerts, spots, sessions, _ = collect_alerts(source, settings, start, end)

    # Extend the price series past the window so late alerts can be exited.
    for session in source.iter_range(
        end + timedelta(days=1), end + timedelta(days=lookahead_days), progress_every=0
    ):
        spots.add_session(session)

    outcomes = score_alerts(alerts, spots, horizons=horizons)
    return summarise(
        outcomes, spots, label=label, start=start, end=end,
        sessions=sessions, horizons=horizons,
    )


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------

def breakdown(
    outcomes: Sequence[AlertOutcome], attribute: str, horizon: int = 5
) -> List[Tuple[str, int, float, float]]:
    """``(group, n, hit_rate, mean_signed_return)`` sliced by one attribute.

    Useful for asking which part of the signal, if any, is carrying it --
    the extreme tier, the bearish calls, the brand-new contracts.
    """
    groups: Dict[str, List[float]] = defaultdict(list)
    for outcome in outcomes:
        if horizon not in outcome.returns:
            continue
        value = getattr(outcome, attribute)
        key = value.value if hasattr(value, "value") else str(value)
        groups[key].append(outcome.returns[horizon])

    rows = []
    for key, values in groups.items():
        rows.append(
            (
                key,
                len(values),
                sum(1 for v in values if v > 0) / len(values),
                mean(values),
            )
        )
    return sorted(rows, key=lambda item: -item[1])


def format_breakdown(
    outcomes: Sequence[AlertOutcome], attribute: str, horizon: int = 5
) -> str:
    rows = breakdown(outcomes, attribute, horizon)
    if not rows:
        return f"  (no scored alerts for {attribute})"
    lines = [f"  by {attribute} at {horizon}d:"]
    for key, count, hit_rate, mean_return in rows:
        lines.append(
            f"    {key:<18} n={count:>5}  hit={hit_rate * 100:5.1f}%  mean={mean_return:+6.2f}%"
        )
    return "\n".join(lines)
