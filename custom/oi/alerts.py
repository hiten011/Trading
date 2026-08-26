"""Turning scan results into a Telegram message worth reading on a phone.

The equity screener next door renders a fixed-width table, which works when
each hit is three short columns. An OI hit has a dozen numbers that matter --
the percentage, both OI levels, volume, notional, moneyness, expiry, buildup,
the futures leg -- and a table that wide wraps into unreadable mush on a
phone. So each hit gets a short block instead, with the one-line headline the
brief asked for as its first line.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import List, Optional, Sequence

import pandas as pd

from custom.oi.config import OISettings
from custom.oi.models import OIAlert, Tier
from custom.oi.scanner import CRORE, ScanResult
from custom.notify import TelegramError, TelegramNotifier, escape

LOGGER = logging.getLogger("custom.oi.alerts")


def format_alert(alert: OIAlert) -> str:
    """One hit as a compact HTML block."""
    row = alert.row
    lines = [
        f"{alert.tier.emoji} <b>{escape(alert.headline)}</b> {alert.bias.emoji} {alert.bias.value}"
    ]

    if row.is_new_contract:
        oi_line = f"OI 0 → {row.oi_lots:,.0f} lots (new position)"
    else:
        oi_line = (
            f"OI {alert.oi_pct_change:+,.0f}% "
            f"({row.prev_oi_lots:,.0f} → {row.oi_lots:,.0f} lots)"
        )
    lines.append(f"   {escape(oi_line)} · {alert.share_of_symbol_oi * 100:.1f}% of book")

    basis = "vs prev close" if row.price_basis == "prev_close" else "intraday"
    lines.append(
        f"   Px {row.price_pct_change:+.1f}% ({basis}) · "
        f"Vol {row.volume_lots:,.0f} lots · ₹{row.notional / CRORE:,.1f} Cr"
    )

    spot_move = (
        f" ({alert.context.spot_pct_change:+.1f}%)"
        if alert.context.spot_pct_change
        else ""
    )
    side = "above" if row.key.strike > row.underlying else "below"
    lines.append(
        f"   Spot {row.underlying:,.2f}{spot_move} · strike {row.moneyness_pct:.1f}% {side}"
    )

    futures = alert.context.futures_buildup
    tail = f"   {row.key.expiry:%d%b%y} · {row.days_to_expiry()}d"
    if futures.value != "UNKNOWN":
        tail += f" · futures: {futures.label}"
    if alert.context.pcr:
        tail += f" · PCR {alert.context.pcr:.2f}"
    lines.append(escape(tail))
    return "\n".join(lines)


def build_message(
    alerts: Sequence[OIAlert],
    result: ScanResult,
    settings: OISettings,
    as_of: Optional[datetime] = None,
    suppressed: int = 0,
) -> str:
    """Compose the whole Telegram body (HTML parse mode)."""
    stamp = (as_of or datetime.now()).strftime("%d %b %Y %H:%M")
    header = [
        "<b>F&amp;O OI BLAST</b>",
        f"<i>session {result.trade_date:%d %b %Y} · sent {escape(stamp)} IST</i>",
    ]

    if not alerts:
        header.append(
            f"\nNo contract cleared {settings.min_tier.value} "
            f"({settings.strong_pct:g}% OI) out of {result.scanned_contracts:,} "
            f"contracts across {result.scanned_symbols} underlyings."
        )
        return "\n".join(header)

    counts = {tier: 0 for tier in (Tier.EXTREME, Tier.STRONG, Tier.WATCH)}
    for alert in alerts:
        if alert.tier in counts:
            counts[alert.tier] += 1
    breakdown = " · ".join(
        f"{tier.emoji} {count} {tier.value.lower()}"
        for tier, count in counts.items()
        if count
    )

    shown = f"<b>{len(alerts)}</b> alert{'s' if len(alerts) != 1 else ''}"
    if result.total_hits > len(alerts):
        shown += f" (top {len(alerts)} of {result.total_hits})"
    header.append(
        f"\n{shown} from {result.scanned_contracts:,} contracts / "
        f"{result.scanned_symbols} underlyings"
    )
    if breakdown:
        header.append(breakdown)
    if suppressed:
        header.append(f"<i>{suppressed} repeat(s) held back by cooldown</i>")

    body = "\n\n".join(format_alert(alert) for alert in alerts)
    symbols = " ".join(f"#{name}" for name in dict.fromkeys(alert.symbol for alert in alerts))
    footer = f"\n\n{escape(symbols)}" if symbols else ""
    disclaimer = "\n<i>Screener output, not a trade recommendation.</i>"
    return "\n".join(header) + "\n\n" + body + footer + disclaimer


def to_frame(alerts: Sequence[OIAlert]) -> pd.DataFrame:
    return pd.DataFrame([alert.as_row() for alert in alerts]) if alerts else pd.DataFrame()


def write_csv(alerts: Sequence[OIAlert], directory: str, trade_date=None) -> Optional[str]:
    """Write the full hit list to CSV and return its path."""
    if not alerts:
        return None
    os.makedirs(directory, exist_ok=True)
    stamp = (trade_date or alerts[0].row.trade_date).strftime("%Y%m%d")
    path = os.path.join(
        directory, f"oi_blast_{stamp}_{datetime.now().strftime('%H%M%S')}.csv"
    )
    to_frame(alerts).to_csv(path, index=False)
    LOGGER.info("Wrote %d alert row(s) to %s", len(alerts), path)
    return path


def deliver(
    alerts: Sequence[OIAlert],
    result: ScanResult,
    settings: OISettings,
    notifier: TelegramNotifier,
    as_of: Optional[datetime] = None,
    suppressed: int = 0,
) -> bool:
    """Send the alert. Returns whether anything was actually sent."""
    if not alerts and not settings.notify_empty:
        LOGGER.info("Nothing to send (OI_NOTIFY_EMPTY=0)")
        return False

    message = build_message(alerts, result, settings, as_of=as_of, suppressed=suppressed)
    notifier.send_message(message)

    if settings.attach_csv and alerts:
        path = write_csv(alerts, settings.reports_dir, trade_date=result.trade_date)
        if path:
            try:
                notifier.send_document(
                    path, caption=f"OI blast {result.trade_date:%d %b %Y}: {result.total_hits} hit(s)"
                )
            except TelegramError as exc:
                # The message itself already went out; losing the attachment
                # is not worth failing the run over.
                LOGGER.warning("Could not attach the CSV: %s", exc)
    return True
