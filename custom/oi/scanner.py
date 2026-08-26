"""The signal itself: which option contracts had an open-interest blast.

The core calculation is the Pine original's, unchanged in spirit::

    oi_change_pct = (oi - oi[1]) / oi[1] * 100

What surrounds it is the part that makes the number mean something. A bare
1000% screen over ~35,000 live contracts fires overwhelmingly on deep-OTM
strikes that went from 40 lots to 500 on a handful of trades -- technically a
1150% move, and worth nothing. Every gate below exists to remove one specific
class of that noise, and each is individually tunable so the backtest can
measure what it costs and what it buys.

Gates, in the order they run (cheapest and most selective first):

1. universe      -- symbol allow-list, optional index exclusion
2. expiry        -- nearest N expiries, a days-to-expiry band, and a
                    rollover guard for the far month during expiry week
3. moneyness     -- strike within X% of spot
4. liquidity     -- floors on current OI, *previous* OI, the OI added,
                    traded volume, and traded notional value
5. threshold     -- the tiered percentage bands
6. significance  -- the OI added as a share of the symbol's whole OI book,
                    and optionally a per-contract z-score
7. confirmation  -- optionally require the futures leg to agree

Everything downstream of gate 4 is quantities in **lots**, never raw units.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import mean, pstdev
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from custom.oi.config import INDEX_SYMBOLS, OISettings
from custom.oi.models import (
    Bias,
    Buildup,
    ContractKey,
    OIAlert,
    OptionRow,
    Tier,
    UnderlyingContext,
    classify_buildup,
    option_bias,
)
from custom.oi.sources.bhavcopy import SessionData

LOGGER = logging.getLogger("custom.oi.scanner")

CRORE = 1e7


@dataclass
class ScanResult:
    """Everything one scan produced, including why things were dropped."""

    trade_date: date
    alerts: List[OIAlert] = field(default_factory=list)
    scanned_contracts: int = 0
    scanned_symbols: int = 0
    rejections: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total_hits: int = 0

    def summary(self) -> str:
        drops = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(self.rejections.items(), key=lambda kv: -kv[1])
        )
        return (
            f"{self.trade_date}: {self.total_hits} hit(s) from {self.scanned_contracts} "
            f"contracts / {self.scanned_symbols} symbols. Dropped: {drops or 'nothing'}"
        )


# ---------------------------------------------------------------------------
# History for the z-score gate
# ---------------------------------------------------------------------------

def build_pct_history(
    sessions: Sequence[SessionData],
) -> Dict[ContractKey, List[float]]:
    """Per-contract series of past daily OI percent changes.

    Used to ask "is this move unusual *for this contract*" rather than
    "is this move bigger than a number someone picked". Contracts whose OI
    routinely doubles need a higher bar than ones that never move.
    """
    history: Dict[ContractKey, List[float]] = defaultdict(list)
    for session in sessions:
        for row in session.rows:
            if row.is_new_contract:
                continue
            change = row.oi_pct_change
            if math.isfinite(change):
                history[row.key].append(change)
    return history


def z_score(value: float, samples: Sequence[float], min_samples: int = 8) -> float:
    """How many standard deviations ``value`` sits above the sample mean."""
    if len(samples) < min_samples:
        return float("nan")
    spread = pstdev(samples)
    if spread <= 0:
        return float("nan")
    return (value - mean(samples)) / spread


# ---------------------------------------------------------------------------
# Expiry selection
# ---------------------------------------------------------------------------

def eligible_expiries(
    rows: Iterable[OptionRow], settings: OISettings, as_of: date
) -> Dict[str, set]:
    """The expiries worth scanning, per symbol.

    Far-dated series carry tiny base OI, which manufactures enormous
    percentage moves out of ordinary lot sizes -- so only the nearest few
    expiries are ever in play.
    """
    by_symbol: Dict[str, set] = defaultdict(set)
    for row in rows:
        by_symbol[row.key.symbol].add(row.key.expiry)

    chosen: Dict[str, set] = {}
    for symbol, expiries in by_symbol.items():
        future = sorted(expiry for expiry in expiries if (expiry - as_of).days >= 0)
        if not future:
            chosen[symbol] = set()
            continue
        keep = future[: max(settings.max_expiries, 1)]

        if settings.suppress_rollover and len(keep) > 1:
            front = keep[0]
            front_dte = (front - as_of).days
            if front_dte <= settings.rollover_window_days:
                # Only a *monthly* rollover migrates a whole OI book. Index
                # weeklies expire every week without one, so the test is
                # whether the next series sits in a later calendar month --
                # not merely that some expiry is close.
                keep = [front] + [
                    expiry
                    for expiry in keep[1:]
                    if (expiry.year, expiry.month) == (front.year, front.month)
                ]
        chosen[symbol] = set(keep)
    return chosen


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_alert(
    row: OptionRow, tier: Tier, share_of_symbol_oi: float, z: float
) -> float:
    """Rank hits so the strongest survive ``OI_MAX_ALERTS``.

    Deliberately dominated by the tier, then by how much of the symbol's whole
    OI book the move represents -- a contract that added 3% of everything
    outstanding in that name matters more than one that posted a bigger
    percentage on a smaller base. Raw percentage contributes least, and is
    capped, because it is the least trustworthy of the three.
    """
    score = tier.rank * 100.0
    score += min(share_of_symbol_oi, 0.10) * 1000.0
    change = row.oi_pct_change
    if math.isfinite(change):
        score += min(change, 5000.0) / 100.0
    else:
        score += 25.0  # a brand-new contract, graded on its absolute size below
    score += min(row.notional / CRORE, 50.0) / 2.0
    if math.isfinite(z):
        score += min(max(z, 0.0), 10.0) * 2.0
    return score


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def scan_session(
    session: SessionData,
    settings: OISettings,
    prev_session: Optional[SessionData] = None,
    history: Optional[Dict[ContractKey, List[float]]] = None,
) -> ScanResult:
    """Run every gate over one session's contracts.

    Args:
        session: The session to scan.
        prev_session: The session before it, used only to fill in the
            underlying's own price change. Optional; without it the spot
            change reads 0 and the futures leg still carries direction.
        history: Output of :func:`build_pct_history` over earlier sessions.
            Only needed when ``OI_MIN_Z_SCORE`` is above zero.
    """
    result = ScanResult(trade_date=session.trade_date)
    reject = result.rejections

    contexts = _with_previous_spot(session, prev_session)
    allowed = {s.upper() for s in settings.symbols} if settings.symbols else None
    expiries = eligible_expiries(session.rows, settings, session.trade_date)

    result.scanned_contracts = len(session.rows)
    result.scanned_symbols = len(session.contexts)

    hits: List[OIAlert] = []
    for row in session.rows:
        symbol = row.key.symbol

        if allowed is not None and symbol not in allowed:
            reject["not_in_universe"] += 1
            continue
        if settings.exclude_indices and _is_index(symbol, contexts.get(symbol)):
            reject["index"] += 1
            continue

        if row.key.expiry not in expiries.get(symbol, ()):
            reject["expiry_not_selected"] += 1
            continue
        dte = row.days_to_expiry()
        if dte < settings.min_days_to_expiry:
            reject["expiry_too_near"] += 1
            continue
        if dte > settings.max_days_to_expiry:
            reject["expiry_too_far"] += 1
            continue

        if row.underlying <= 0:
            reject["no_underlying_price"] += 1
            continue
        if row.moneyness_pct > settings.max_moneyness_pct:
            reject["not_near_the_money"] += 1
            continue

        # --- liquidity floors, all in lots ---------------------------------
        if row.oi_lots < settings.min_oi_lots:
            reject["oi_too_small"] += 1
            continue
        if row.delta_oi_lots < settings.min_delta_oi_lots:
            reject["oi_add_too_small"] += 1
            continue
        if row.volume_lots < settings.min_volume_lots:
            reject["volume_too_small"] += 1
            continue
        if row.notional < settings.min_notional_cr * CRORE:
            reject["notional_too_small"] += 1
            continue

        is_new = row.is_new_contract
        if is_new and not settings.include_new_contracts:
            reject["new_contract"] += 1
            continue
        if not is_new and row.prev_oi_lots < settings.min_prev_oi_lots:
            # The "10 contracts became 110" case: a huge percentage off a base
            # too small to have meant anything.
            reject["prev_oi_too_small"] += 1
            continue

        # --- the threshold itself ------------------------------------------
        change = row.oi_pct_change
        tier = settings.tier_for(change, is_new_contract=is_new)
        if tier.rank < settings.min_tier.rank:
            reject["below_threshold"] += 1
            continue

        # --- significance ---------------------------------------------------
        context = contexts.get(symbol) or UnderlyingContext(
            symbol=symbol, trade_date=session.trade_date, spot=row.underlying
        )
        total_oi = context.total_oi
        share = (row.delta_oi_units / total_oi) if total_oi > 0 else 0.0
        if share < settings.min_share_of_symbol_oi:
            reject["insignificant_vs_symbol"] += 1
            continue

        contract_z = float("nan")
        if settings.min_z_score > 0:
            samples = (history or {}).get(row.key, [])
            contract_z = z_score(change if math.isfinite(change) else 0.0, samples)
            if math.isfinite(contract_z) and contract_z < settings.min_z_score:
                reject["z_below_threshold"] += 1
                continue

        # --- direction -------------------------------------------------------
        buildup = classify_buildup(row.delta_oi_units, row.price_pct_change)
        bias = option_bias(row.key.option_type, buildup)

        if settings.require_futures_confirmation:
            futures = context.futures_buildup
            if futures is Buildup.UNKNOWN:
                reject["no_futures_leg"] += 1
                continue
            futures_bias = _futures_bias(futures)
            if futures_bias is not Bias.NEUTRAL and futures_bias is not bias:
                reject["futures_disagrees"] += 1
                continue

        hits.append(
            OIAlert(
                row=row,
                context=context,
                tier=tier,
                buildup=buildup,
                bias=bias,
                score=score_alert(row, tier, share, contract_z),
                oi_pct_change=change,
                share_of_symbol_oi=share,
                z_score=contract_z,
                reasons=_reasons(row, tier, share, is_new),
            )
        )

    result.total_hits = len(hits)
    result.alerts = _rank_and_cap(hits, settings)
    LOGGER.info("%s", result.summary())
    return result


def _is_index(symbol: str, context: Optional[UnderlyingContext]) -> bool:
    """Prefer the exchange's own classification over a hardcoded name list.

    Bhavcopy tags index options as ``IDO``, so the flag is authoritative there.
    The live NSE source has no equivalent field, so the name list stays as a
    fallback for it.
    """
    if context is not None and context.is_index:
        return True
    return symbol in INDEX_SYMBOLS


def _futures_bias(buildup: Buildup) -> Bias:
    """The four-box table read on futures, where it is actually valid."""
    if buildup is Buildup.LONG_BUILDUP:
        return Bias.BULLISH
    if buildup is Buildup.SHORT_BUILDUP:
        return Bias.BEARISH
    if buildup is Buildup.SHORT_COVERING:
        return Bias.BULLISH
    if buildup is Buildup.LONG_UNWINDING:
        return Bias.BEARISH
    return Bias.NEUTRAL


def _with_previous_spot(
    session: SessionData, prev_session: Optional[SessionData]
) -> Dict[str, UnderlyingContext]:
    """Fill each context's ``prev_spot`` from the prior session, when we have it."""
    if prev_session is None:
        return session.contexts
    for symbol, context in session.contexts.items():
        previous = prev_session.contexts.get(symbol)
        if previous is not None:
            context.prev_spot = previous.spot
    return session.contexts


def _reasons(row: OptionRow, tier: Tier, share: float, is_new: bool) -> List[str]:
    reasons: List[str] = []
    if is_new:
        reasons.append(f"new series, {row.oi_lots:,.0f} lots appeared")
    else:
        reasons.append(
            f"OI {row.oi_pct_change:+,.0f}% ({row.prev_oi_lots:,.0f}→{row.oi_lots:,.0f} lots)"
        )
    reasons.append(f"{share * 100:.1f}% of the symbol's OI book")
    reasons.append(f"₹{row.notional / CRORE:,.1f} Cr traded")
    if tier is Tier.EXTREME:
        reasons.append("extreme tier")
    return reasons


def _rank_and_cap(hits: List[OIAlert], settings: OISettings) -> List[OIAlert]:
    """Best-first, then thinned so one busy symbol cannot fill the message.

    Without the per-symbol cap a single stock in the middle of a rollover
    routinely supplies every slot in the alert, hiding the other twelve names
    that also fired.
    """
    hits.sort(key=lambda alert: (alert.score, alert.symbol), reverse=True)

    if settings.max_per_symbol > 0:
        seen: Dict[str, int] = defaultdict(int)
        thinned: List[OIAlert] = []
        for alert in hits:
            if seen[alert.symbol] >= settings.max_per_symbol:
                continue
            seen[alert.symbol] += 1
            thinned.append(alert)
        hits = thinned

    if settings.max_alerts > 0:
        hits = hits[: settings.max_alerts]
    return hits
