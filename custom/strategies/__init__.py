"""Strategy discovery.

A strategy is any module in this package that exposes an ``evaluate`` function::

    def evaluate(symbol: str, df: pandas.DataFrame) -> Signal | None

Select one with ``PKS_STRATEGY`` in ``.env`` (module name without ``.py``).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from types import ModuleType
from typing import List

LOGGER = logging.getLogger("custom.strategies")

RESERVED = {"base"}


class StrategyError(RuntimeError):
    """Raised when the configured strategy cannot be loaded or is malformed."""


def available() -> List[str]:
    """Names of every loadable strategy module in this package."""
    return sorted(
        module.name
        for module in pkgutil.iter_modules(__path__)
        if not module.name.startswith("_") and module.name not in RESERVED
    )


def load(name: str) -> ModuleType:
    """Import strategy ``name`` and check it satisfies the contract."""
    if not name:
        raise StrategyError("No strategy configured (set PKS_STRATEGY)")

    try:
        module = importlib.import_module(f"{__name__}.{name}")
    except ImportError as exc:
        raise StrategyError(
            f"Could not import strategy {name!r}: {exc}. Available: {', '.join(available()) or 'none'}"
        ) from exc

    if not callable(getattr(module, "evaluate", None)):
        raise StrategyError(f"Strategy {name!r} does not define an evaluate(symbol, df) function")

    LOGGER.info(
        "Loaded strategy %r: %s",
        getattr(module, "NAME", name),
        getattr(module, "DESCRIPTION", "no description"),
    )
    return module
