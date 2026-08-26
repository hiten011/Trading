"""Every knob the OI scanner has, resolved from the environment once.

Naming follows the repo's existing convention (``PKS_*`` for the equity
screener); everything here is ``OI_*`` so the two can share one ``.env``
without colliding. Telegram credentials are *not* here -- they come from the
same loader the rest of the project uses, so there is exactly one secrets
file.

Defaults are set to the brief's ask (1000% on the strong tier) but with the
noise filters switched on, because a bare 1000% screen over the full F&O list
returns mostly deep-OTM lottery tickets that traded four times. See
``docs/OI_SCANNER.md`` for what each floor is actually protecting against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List

from custom.config import (
    _env_bool,
    _env_float,
    _env_int,
    _env_list,
    _env_str,
    load_secrets,
    normalise_chat_id,
)
from custom.oi.models import Tier

LOGGER = logging.getLogger("custom.oi.config")

# Indices behave differently from single stocks: far bigger OI, weekly
# expiries, and OI moves dominated by hedging rather than directional
# conviction. Kept separate so they can be excluded in one switch.
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


@dataclass
class OISettings:
    """Resolved configuration for one scanner run."""

    # --- data -------------------------------------------------------------
    source: str = "bhavcopy"
    cache_dir: str = "data/oi_cache"
    symbols: List[str] = field(default_factory=list)
    exclude_indices: bool = False

    # --- tiered thresholds (percent OI change vs the previous session) ----
    watch_pct: float = 300.0
    strong_pct: float = 1000.0
    extreme_pct: float = 2000.0
    min_tier: Tier = Tier.STRONG

    # --- absolute floors --------------------------------------------------
    # All in LOTS, never raw units: bhavcopy quotes OI in shares, so a raw
    # floor would compare IDEA (lot 71,475) against RELIANCE (lot 500) and
    # silently exclude the more liquid of the two.
    min_oi_lots: float = 500.0
    min_prev_oi_lots: float = 50.0
    min_delta_oi_lots: float = 250.0
    min_volume_lots: float = 100.0
    min_notional_cr: float = 1.0

    # --- near the money ---------------------------------------------------
    max_moneyness_pct: float = 10.0

    # --- expiry hygiene ---------------------------------------------------
    min_days_to_expiry: int = 2
    max_days_to_expiry: int = 45
    max_expiries: int = 2
    # Around a monthly expiry, open interest migrates wholesale from the
    # expiring series into the next one. That shows up as an enormous OI
    # jump in the far contract and means nothing directional, so far-month
    # contracts are dropped while the front month is inside its last few
    # sessions.
    suppress_rollover: bool = True
    rollover_window_days: int = 5

    # --- significance -----------------------------------------------------
    min_share_of_symbol_oi: float = 0.005
    min_z_score: float = 0.0
    z_lookback: int = 20
    include_new_contracts: bool = True
    require_futures_confirmation: bool = False

    # --- alerting ---------------------------------------------------------
    max_alerts: int = 25
    max_per_symbol: int = 3
    cooldown_hours: float = 12.0
    state_db: str = "data/oi_state.sqlite"
    dry_run: bool = False
    notify_empty: bool = False
    attach_csv: bool = True
    reports_dir: str = "data/oi_reports"

    # --- scheduling -------------------------------------------------------
    run_at: List[str] = field(default_factory=lambda: ["18:30"])
    interval_minutes: int = 0
    trading_days_only: bool = True
    timezone: str = "Asia/Kolkata"

    # --- misc -------------------------------------------------------------
    log_level: str = "INFO"
    secrets: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "OISettings":
        raw_tier = _env_str("OI_MIN_TIER", "STRONG").upper()
        try:
            min_tier = Tier(raw_tier)
        except ValueError:
            LOGGER.warning("OI_MIN_TIER=%r is not a tier; using STRONG", raw_tier)
            min_tier = Tier.STRONG

        run_at = _env_list("OI_RUN_AT") or ["18:30"]
        return cls(
            source=_env_str("OI_SOURCE", "bhavcopy").lower(),
            cache_dir=_env_str("OI_CACHE_DIR", "data/oi_cache"),
            symbols=[s.upper() for s in _env_list("OI_SYMBOLS")],
            exclude_indices=_env_bool("OI_EXCLUDE_INDICES", False),
            watch_pct=_env_float("OI_WATCH_PCT", 300.0),
            strong_pct=_env_float("OI_STRONG_PCT", 1000.0),
            extreme_pct=_env_float("OI_EXTREME_PCT", 2000.0),
            min_tier=min_tier,
            min_oi_lots=_env_float("OI_MIN_OI_LOTS", 500.0),
            min_prev_oi_lots=_env_float("OI_MIN_PREV_OI_LOTS", 50.0),
            min_delta_oi_lots=_env_float("OI_MIN_DELTA_OI_LOTS", 250.0),
            min_volume_lots=_env_float("OI_MIN_VOLUME_LOTS", 100.0),
            min_notional_cr=_env_float("OI_MIN_NOTIONAL_CR", 1.0),
            max_moneyness_pct=_env_float("OI_MAX_MONEYNESS_PCT", 10.0),
            min_days_to_expiry=_env_int("OI_MIN_DTE", 2),
            max_days_to_expiry=_env_int("OI_MAX_DTE", 45),
            max_expiries=_env_int("OI_MAX_EXPIRIES", 2),
            suppress_rollover=_env_bool("OI_SUPPRESS_ROLLOVER", True),
            rollover_window_days=_env_int("OI_ROLLOVER_WINDOW_DAYS", 5),
            min_share_of_symbol_oi=_env_float("OI_MIN_SHARE_OF_SYMBOL_OI", 0.005),
            min_z_score=_env_float("OI_MIN_Z_SCORE", 0.0),
            z_lookback=_env_int("OI_Z_LOOKBACK", 20),
            include_new_contracts=_env_bool("OI_INCLUDE_NEW_CONTRACTS", True),
            require_futures_confirmation=_env_bool("OI_REQUIRE_FUTURES_CONFIRM", False),
            max_alerts=_env_int("OI_MAX_ALERTS", 25),
            max_per_symbol=_env_int("OI_MAX_PER_SYMBOL", 3),
            cooldown_hours=_env_float("OI_COOLDOWN_HOURS", 12.0),
            state_db=_env_str("OI_STATE_DB", "data/oi_state.sqlite"),
            dry_run=_env_bool("OI_DRY_RUN", False),
            notify_empty=_env_bool("OI_NOTIFY_EMPTY", False),
            attach_csv=_env_bool("OI_ATTACH_CSV", True),
            reports_dir=_env_str("OI_REPORTS_DIR", "data/oi_reports"),
            run_at=run_at,
            interval_minutes=_env_int("OI_INTERVAL_MINUTES", 0),
            trading_days_only=_env_bool("OI_TRADING_DAYS_ONLY", True),
            timezone=_env_str("TZ", "Asia/Kolkata"),
            log_level=_env_str("OI_LOG_LEVEL", _env_str("PKS_LOG_LEVEL", "INFO")).upper(),
            secrets=load_secrets(),
        )

    # -- telegram ----------------------------------------------------------
    @property
    def telegram_token(self) -> str:
        return (
            self.secrets.get("TELEGRAM_BOT_TOKEN") or self.secrets.get("TOKEN") or ""
        ).strip()

    @property
    def telegram_chat_id(self) -> str:
        raw = (
            self.secrets.get("TELEGRAM_CHAT_ID")
            or self.secrets.get("chat_idADMIN")
            or self.secrets.get("CHAT_ID")
            or ""
        )
        return normalise_chat_id(raw)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def tier_for(self, oi_pct_change: float, is_new_contract: bool = False) -> Tier:
        """Which band a percentage change lands in.

        A contract with no prior open interest has an undefined percentage,
        not an infinite one. It is graded on the absolute size of the position
        that appeared instead -- the floors elsewhere decide whether it is big
        enough to care about at all.
        """
        if is_new_contract:
            return Tier.STRONG
        if oi_pct_change >= self.extreme_pct:
            return Tier.EXTREME
        if oi_pct_change >= self.strong_pct:
            return Tier.STRONG
        if oi_pct_change >= self.watch_pct:
            return Tier.WATCH
        return Tier.NONE

    def describe(self) -> str:
        token = self.telegram_token
        masked = (
            f"{token[:6]}...{token[-4:]}" if len(token) > 12
            else ("<missing>" if not token else "<set>")
        )
        return (
            f"source={self.source} tiers={self.watch_pct:g}/{self.strong_pct:g}/"
            f"{self.extreme_pct:g}% min_tier={self.min_tier.value} "
            f"floors(oi>={self.min_oi_lots:g}L, prev>={self.min_prev_oi_lots:g}L, "
            f"add>={self.min_delta_oi_lots:g}L, vol>={self.min_volume_lots:g}L, "
            f"notional>={self.min_notional_cr:g}Cr) "
            f"ntm<={self.max_moneyness_pct:g}% dte={self.min_days_to_expiry}-"
            f"{self.max_days_to_expiry} expiries<={self.max_expiries} "
            f"rollover_guard={'on' if self.suppress_rollover else 'off'} "
            f"z>={self.min_z_score:g} cooldown={self.cooldown_hours:g}h "
            f"dry_run={self.dry_run} telegram={masked}/{self.telegram_chat_id or '<missing>'}"
        )
