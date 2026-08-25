"""Turning a list of Signals into something worth reading on a phone."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import List, Optional, Sequence

import pandas as pd

from custom.notify import escape
from custom.strategies.base import Signal

LOGGER = logging.getLogger("custom.report")

# Telegram renders <pre> in a monospace font, which is what makes a table line
# up. Keep it narrow enough not to wrap on a phone.
MAX_TABLE_WIDTH = 46


def to_frame(signals: Sequence[Signal]) -> pd.DataFrame:
    """Build a table from the signals, columns ordered as the strategy set them."""
    if not signals:
        return pd.DataFrame()
    return pd.DataFrame([signal.as_row() for signal in signals])


def render_table(frame: pd.DataFrame, columns: Optional[List[str]] = None) -> str:
    """Render as a fixed-width table, dropping the wordy columns if too wide."""
    if frame.empty:
        return ""

    display = frame[columns] if columns else frame
    # "Why" repeats the strategy's reason on every row; it is the first thing
    # to sacrifice when the table would wrap.
    if "Why" in display.columns and _width(display) > MAX_TABLE_WIDTH:
        display = display.drop(columns=["Why"])

    try:
        from tabulate import tabulate

        return tabulate(display, headers="keys", tablefmt="simple", showindex=False, floatfmt=".2f")
    except ImportError:
        return display.to_string(index=False)


def _width(frame: pd.DataFrame) -> int:
    widths = []
    for column in frame.columns:
        longest = max([len(str(column))] + [len(str(value)) for value in frame[column]])
        widths.append(longest)
    return sum(widths) + 2 * len(widths)


def build_message(
    signals: Sequence[Signal],
    strategy_name: str,
    strategy_description: str = "",
    scanned: int = 0,
    as_of: Optional[datetime] = None,
    truncated_from: int = 0,
) -> str:
    """Compose the Telegram message body (HTML parse mode)."""
    stamp = (as_of or datetime.now()).strftime("%d %b %Y %H:%M")
    header = [f"<b>{escape(strategy_name)}</b>", f"<i>{escape(stamp)} IST</i>"]
    if strategy_description:
        header.append(escape(strategy_description))

    if not signals:
        header.append(f"\nNo matches out of {scanned} stocks scanned.")
        return "\n".join(header)

    count = f"<b>{len(signals)}</b> match{'es' if len(signals) != 1 else ''}"
    if truncated_from and truncated_from > len(signals):
        count += f" (top {len(signals)} of {truncated_from})"
    header.append(f"\n{count} out of {scanned} stocks scanned:")

    table = render_table(to_frame(signals))
    body = f"<pre>{escape(table)}</pre>"

    symbols = " ".join(f"#{signal.symbol}" for signal in signals[:20])
    footer = f"\n{escape(symbols)}" if symbols else ""

    return "\n".join(header) + "\n" + body + footer


def write_csv(signals: Sequence[Signal], directory: str, strategy_name: str) -> Optional[str]:
    """Write the full result set to CSV and return its path."""
    if not signals:
        return None
    frame = to_frame(signals)
    os.makedirs(directory, exist_ok=True)
    slug = "".join(char if char.isalnum() else "_" for char in strategy_name).strip("_").lower()
    path = os.path.join(directory, f"{slug}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
    frame.to_csv(path, index=False)
    LOGGER.info("Wrote %d rows to %s", len(frame), path)
    return path
