"""Remembering what has already been alerted on.

Without this the scanner re-sends every contract still sitting above the
threshold on every cycle, which on an interval schedule means the same twelve
lines arriving all afternoon. A contract alerts once per crossing; the same
contract only alerts again after a cooldown, or immediately if it has
*escalated* into a higher tier -- a move from 1000% to 2500% is new
information and should not be swallowed by the cooldown that the first alert
started.

SQLite rather than an in-memory set because the whole point is surviving a
container restart, and rather than a JSON file because two overlapping runs
writing the same file will lose alerts.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import date
from typing import Iterable, List, Optional, Tuple

from custom.oi.models import ContractKey, OIAlert, Tier

LOGGER = logging.getLogger("custom.oi.state")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fired_alerts (
    contract   TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    tier       TEXT NOT NULL,
    tier_rank  INTEGER NOT NULL,
    oi_pct     REAL,
    fired_at   REAL NOT NULL,
    PRIMARY KEY (contract, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_fired_at ON fired_alerts (fired_at);
"""


class AlertState:
    """Per-contract alert history backing the cooldown and de-duplication.

    Args:
        path: SQLite file. ``:memory:`` is honoured, which is what the tests
            and ``--dry-run`` use so a rehearsal never suppresses a real alert.
    """

    def __init__(self, path: str = "data/oi_state.sqlite") -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        # check_same_thread=False so a scheduled run and a manual one in the
        # same process do not trip over the connection's thread affinity.
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AlertState":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- queries -----------------------------------------------------------
    def last_alert(
        self, key: ContractKey, trade_date: date
    ) -> Optional[Tuple[int, float]]:
        """``(tier_rank, fired_at)`` of the most recent alert for this contract."""
        cursor = self._connection.execute(
            "SELECT tier_rank, fired_at FROM fired_alerts "
            "WHERE contract = ? ORDER BY fired_at DESC LIMIT 1",
            (key.slug,),
        )
        row = cursor.fetchone()
        return (int(row[0]), float(row[1])) if row else None

    def should_alert(
        self,
        key: ContractKey,
        trade_date: date,
        tier: Tier,
        cooldown_hours: float,
        now: Optional[float] = None,
    ) -> bool:
        """Whether this contract is allowed to alert right now."""
        previous = self.last_alert(key, trade_date)
        if previous is None:
            return True
        previous_rank, fired_at = previous
        if tier.rank > previous_rank:
            LOGGER.debug("%s escalated to %s; bypassing cooldown", key, tier.value)
            return True
        if cooldown_hours <= 0:
            return True
        elapsed_hours = ((now or time.time()) - fired_at) / 3600.0
        return elapsed_hours >= cooldown_hours

    # -- writes ------------------------------------------------------------
    def record(self, alert: OIAlert, now: Optional[float] = None) -> None:
        pct = alert.oi_pct_change
        self._connection.execute(
            "INSERT INTO fired_alerts (contract, trade_date, tier, tier_rank, oi_pct, fired_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(contract, trade_date) DO UPDATE SET "
            "tier=excluded.tier, tier_rank=excluded.tier_rank, "
            "oi_pct=excluded.oi_pct, fired_at=excluded.fired_at",
            (
                alert.key.slug,
                alert.row.trade_date.isoformat(),
                alert.tier.value,
                alert.tier.rank,
                None if pct != pct or pct in (float("inf"), float("-inf")) else pct,
                now or time.time(),
            ),
        )
        self._connection.commit()

    def record_all(self, alerts: Iterable[OIAlert], now: Optional[float] = None) -> None:
        for alert in alerts:
            self.record(alert, now=now)

    def filter_new(
        self, alerts: Iterable[OIAlert], cooldown_hours: float, now: Optional[float] = None
    ) -> List[OIAlert]:
        """Drop everything still inside its cooldown, keeping escalations."""
        fresh: List[OIAlert] = []
        suppressed = 0
        for alert in alerts:
            if self.should_alert(
                alert.key, alert.row.trade_date, alert.tier, cooldown_hours, now=now
            ):
                fresh.append(alert)
            else:
                suppressed += 1
        if suppressed:
            LOGGER.info("Cooldown suppressed %d already-sent alert(s)", suppressed)
        return fresh

    def purge_older_than(self, days: int = 90, now: Optional[float] = None) -> int:
        """Keep the state file from growing without bound."""
        cutoff = (now or time.time()) - days * 86400
        with closing(self._connection.execute(
            "DELETE FROM fired_alerts WHERE fired_at < ?", (cutoff,)
        )) as cursor:
            removed = cursor.rowcount or 0
        self._connection.commit()
        if removed:
            LOGGER.info("Purged %d alert record(s) older than %d days", removed, days)
        return removed
