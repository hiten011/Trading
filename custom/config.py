"""Configuration and secret loading for the custom alert runner.

Everything is driven by environment variables (set through ``.env`` +
docker-compose) except the Telegram credentials, which are read from the same
``.env.dev`` file PKScreener itself uses so there is only one secrets file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

LOGGER = logging.getLogger("custom.config")

# Locations checked for the PKScreener secrets file, in order.
ENV_DEV_CANDIDATES = (
    "/PKScreener-main/.env.dev",
    "./.env.dev",
    "./secrets/.env.dev",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "secrets", ".env.dev"),
)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_env_file(path: str) -> Dict[str, str]:
    """Parse a ``KEY='value'`` style env file.

    Uses python-dotenv when available (it ships with PKDevTools) and falls back
    to a small parser so the runner also works outside the container.
    """
    if not os.path.isfile(path):
        return {}
    try:
        from dotenv import dotenv_values

        return {k: (v or "") for k, v in dotenv_values(path).items()}
    except ImportError:
        pass

    values: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            values[key.strip()] = _strip_quotes(raw)
    return values


def load_secrets(explicit_path: Optional[str] = None) -> Dict[str, str]:
    """Load PKScreener's ``.env.dev``; real environment variables win."""
    candidates = [explicit_path] if explicit_path else list(ENV_DEV_CANDIDATES)
    secrets: Dict[str, str] = {}
    for candidate in candidates:
        if not candidate:
            continue
        found = read_env_file(candidate)
        if found:
            LOGGER.debug("Loaded %d secret(s) from %s", len(found), candidate)
            secrets = found
            break
    else:
        LOGGER.debug("No .env.dev found in %s", candidates)

    # Real environment variables override the file, which makes it easy to
    # override a single value for a one-off run.
    for key in ("TOKEN", "CHAT_ID", "chat_idADMIN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


def normalise_chat_id(raw: str) -> str:
    """Return a chat id Telegram will accept.

    PKScreener stores channel ids in ``CHAT_ID`` *without* the leading ``-`` and
    prepends it at send time. Users copying an id from ``@userinfobot`` paste it
    verbatim. Accept both:

    * already signed (``-1001785195297``, ``5058733760``) -> unchanged
    * unsigned but channel-shaped (starts with ``100`` and >= 13 digits) -> ``-`` added
    """
    chat_id = str(raw or "").strip()
    if not chat_id:
        return ""
    if chat_id.startswith("-"):
        return chat_id
    if chat_id.startswith("100") and len(chat_id) >= 13:
        return "-" + chat_id
    return chat_id


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env_str(key)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        LOGGER.warning("%s=%r is not a number, using %s", key, raw, default)
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env_str(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("%s=%r is not a number, using %s", key, raw, default)
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env_str(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _env_list(key: str) -> List[str]:
    raw = _env_str(key)
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Settings:
    """Everything the runner needs, resolved once at startup."""

    # --- universe ---------------------------------------------------------
    universe: str = "auto"
    universe_file: str = "/app/config/universe.txt"
    index_option: int = 12  # 12 = "Nifty (All Stocks)" in PKScreener's menu

    # --- data -------------------------------------------------------------
    # A normal PKScreener scan caches into results/Data; its download-only mode
    # (-d) writes to actions-data-download instead. Search both.
    data_dir: str = "/PKScreener-main/results/Data:/PKScreener-main/actions-data-download"
    lookback_days: int = 250

    # A single --schedule container refreshes its own cache periodically
    # (see custom/datarefresh.py) rather than needing a separate data-refresh
    # container or an external cron -- this is what keeps a container running
    # on a server for weeks from scanning ever-more-stale candles.
    auto_refresh_data: bool = True
    data_max_age_hours: float = 20.0

    # --- pre-filters applied before the indicator runs --------------------
    min_price: float = 20.0
    max_price: float = 100000.0
    min_avg_volume: float = 100000.0

    # --- alerting ---------------------------------------------------------
    strategy: str = "my_indicator"
    max_alerts: int = 40
    dry_run: bool = False
    notify_empty: bool = False
    attach_csv: bool = True

    # --- scheduling -------------------------------------------------------
    run_at: List[str] = field(default_factory=list)
    interval_minutes: int = 0
    trading_days_only: bool = True
    timezone: str = "Asia/Kolkata"

    # --- misc -------------------------------------------------------------
    log_level: str = "INFO"
    secrets: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        secrets = load_secrets()
        return cls(
            universe=_env_str("PKS_UNIVERSE", "auto").lower(),
            universe_file=_env_str("PKS_UNIVERSE_FILE", "/app/config/universe.txt"),
            index_option=_env_int("PKS_INDEX_OPTION", 12),
            data_dir=_env_str(
                "PKS_DATA_DIR",
                "/PKScreener-main/results/Data:/PKScreener-main/actions-data-download",
            ),
            lookback_days=_env_int("PKS_LOOKBACK_DAYS", 250),
            auto_refresh_data=_env_bool("PKS_AUTO_REFRESH_DATA", True),
            data_max_age_hours=_env_float("PKS_DATA_MAX_AGE_HOURS", 20.0),
            min_price=_env_float("PKS_MIN_PRICE", 20.0),
            max_price=_env_float("PKS_MAX_PRICE", 100000.0),
            min_avg_volume=_env_float("PKS_MIN_AVG_VOLUME", 100000.0),
            strategy=_env_str("PKS_STRATEGY", "my_indicator"),
            max_alerts=_env_int("PKS_MAX_ALERTS", 40),
            dry_run=_env_bool("PKS_DRY_RUN", False),
            notify_empty=_env_bool("PKS_NOTIFY_EMPTY", False),
            attach_csv=_env_bool("PKS_ATTACH_CSV", True),
            run_at=_env_list("PKS_RUN_AT"),
            interval_minutes=_env_int("PKS_INTERVAL_MINUTES", 0),
            trading_days_only=_env_bool("PKS_TRADING_DAYS_ONLY", True),
            timezone=_env_str("TZ", "Asia/Kolkata"),
            log_level=_env_str("PKS_LOG_LEVEL", "INFO").upper(),
            secrets=secrets,
        )

    # --- telegram ---------------------------------------------------------
    @property
    def telegram_token(self) -> str:
        return (self.secrets.get("TELEGRAM_BOT_TOKEN") or self.secrets.get("TOKEN") or "").strip()

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
    def data_dirs(self) -> List[str]:
        """Every directory searched for cached candles, in preference order."""
        return [part for part in self.data_dir.split(":") if part]

    @property
    def reports_dir(self) -> str:
        """Where result CSVs are written -- alongside the first data directory."""
        primary = self.data_dirs[0] if self.data_dirs else "/PKScreener-main/results/Data"
        parent = os.path.dirname(primary.rstrip("/")) or "."
        return os.path.join(parent, "Reports")

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def describe(self) -> str:
        token = self.telegram_token
        masked = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else ("<missing>" if not token else "<set>")
        return (
            f"universe={self.universe} strategy={self.strategy} "
            f"lookback={self.lookback_days}d min_price={self.min_price} "
            f"min_avg_volume={self.min_avg_volume:.0f} dry_run={self.dry_run} "
            f"telegram_token={masked} chat_id={self.telegram_chat_id or '<missing>'}"
        )


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
