"""Resolving which stocks to scan.

"All the stocks in India" in practice means every symbol listed on the NSE.
Three ways to get that list, in decreasing order of self-sufficiency:

``auto``        every symbol inside PKScreener's cached candle file. No extra
                network call, and it is exactly the set we have data for.
``pkscreener``  ask PKScreener's own fetcher for an index constituent list
                (index option 12 = "Nifty (All Stocks)").
``file``        read ``config/universe.txt`` -- your own hand-picked list.
"""

from __future__ import annotations

import logging
import os
import pickle
from typing import List, Optional

from custom.data import find_cache_file

LOGGER = logging.getLogger("custom.universe")


def from_file(path: str) -> List[str]:
    """One symbol per line; ``#`` comments and blanks ignored."""
    if not os.path.isfile(path):
        LOGGER.warning("Universe file %s does not exist", path)
        return []
    symbols: List[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip().upper()
            if line:
                symbols.append(line.replace(".NS", ""))
    LOGGER.info("Universe file %s gave %d symbols", path, len(symbols))
    return sorted(set(symbols))


def from_cache(data_dirs) -> List[str]:
    """Every symbol present in PKScreener's cached candle file."""
    cache_path = find_cache_file(data_dirs)
    if cache_path is None:
        return []
    try:
        with open(cache_path, "rb") as handle:
            payload = pickle.load(handle)
    except (OSError, pickle.UnpicklingError, EOFError) as exc:
        LOGGER.warning("Could not read %s: %s", cache_path, exc)
        return []
    if not isinstance(payload, dict):
        return []
    symbols = sorted({str(key).upper().replace(".NS", "") for key in payload})
    LOGGER.info("Cache universe: %d symbols", len(symbols))
    return symbols


def from_pkscreener(index_option: int = 12) -> List[str]:
    """Ask PKScreener's fetcher for the constituents of one of its indexes.

    Index options mirror PKScreener's own menu: 12 = Nifty (All Stocks),
    5 = Nifty 500, 1 = Nifty 50, 14 = F&O stocks.
    """
    try:
        from pkscreener.classes.ConfigManager import parser, tools
        from pkscreener.classes.Fetcher import screenerStockDataFetcher
    except ImportError as exc:
        LOGGER.warning("PKScreener is not importable here (%s)", exc)
        return []

    try:
        config_manager = tools()
        config_manager.getConfig(parser)
        fetcher = screenerStockDataFetcher(config_manager)
        codes = fetcher.fetchStockCodes(index_option, stockCode=None)
    except Exception as exc:  # noqa: BLE001 - upstream raises a variety of errors
        LOGGER.warning("PKScreener fetchStockCodes(%s) failed: %s", index_option, exc)
        return []

    symbols = sorted({str(code).upper().replace(".NS", "") for code in (codes or []) if code})
    LOGGER.info("PKScreener index option %s gave %d symbols", index_option, len(symbols))
    return symbols


def resolve(
    mode: str = "auto",
    data_dirs=("/PKScreener-main/results/Data", "/PKScreener-main/actions-data-download"),
    universe_file: str = "/app/config/universe.txt",
    index_option: int = 12,
) -> Optional[List[str]]:
    """Resolve the configured universe.

    Returns ``None`` for "everything in the cache", which lets the data layer
    skip filtering entirely -- that is the fastest path for a full-market scan.
    """
    mode = (mode or "auto").lower()

    if mode == "file":
        symbols = from_file(universe_file)
        if symbols:
            return symbols
        LOGGER.warning("Universe file was empty; falling back to the cached universe")
        mode = "auto"

    if mode == "pkscreener":
        symbols = from_pkscreener(index_option)
        if symbols:
            return symbols
        LOGGER.warning("PKScreener fetcher returned nothing; falling back to the cached universe")
        mode = "auto"

    # auto: scan whatever we have candles for. No need to unpickle the cache
    # just to enumerate its keys -- the data layer is about to read it anyway,
    # and returning None tells it to keep every symbol it finds.
    if find_cache_file(data_dirs) is not None:
        LOGGER.info("Scanning every symbol in PKScreener's cache")
        return None

    LOGGER.warning("No cached universe available; trying PKScreener's fetcher")
    symbols = from_pkscreener(index_option)
    return symbols or None
