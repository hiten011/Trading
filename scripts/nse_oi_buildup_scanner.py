#!/usr/bin/env python3
"""
NSE F&O Open Interest Buildup Scanner
======================================
Scans every stock in NSE's F&O (Futures & Options) segment and flags
every strike price - call (CE) or put (PE) side - where today's
percentage change in Open Interest exceeds a threshold (default: 2000%).

Data source: NSE's own option-chain API, accessed through the
`jugaad-data` library (https://github.com/jugaad-py/jugaad-data), a
maintained open-source scraper that handles NSE's Akamai bot-protection
session/cookie dance for you. This script does not scrape HTML or
reverse-engineer anything itself - it just calls that library and
filters the results.

INSTALL
    pip install jugaad-data pandas --break-system-packages

RUN
    python nse_oi_buildup_scanner.py
    python nse_oi_buildup_scanner.py --threshold 1500
    python nse_oi_buildup_scanner.py --include-indices
    python nse_oi_buildup_scanner.py --symbols RELIANCE,TCS,INFY --threshold 20

IMPORTANT NOTES
  - NSE aggressively blocks/rate-limits requests from datacenter or
    cloud IPs (AWS, GCP, Azure, etc.) via Akamai bot management. This
    script works from a normal residential/office connection. If you
    later run it on a cloud box (e.g. your EC2 instance) and every
    symbol starts failing, that's why - you'll need a residential
    proxy or to run the scan from elsewhere and push results in.
  - "Change in OI" is versus the previous trading session's close, so
    this is only meaningful once today's session has started trading
    (from 09:15 IST onward). NSE's pchangeinOpenInterest field is used
    directly rather than recomputed, since it's NSE's own figure.
  - A full scan is ~210 stocks. With a polite delay between requests
    (to avoid tripping bot detection) it takes roughly 5-8 minutes.
"""

import argparse
import csv
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional, Set

try:
    from jugaad_data.nse import NSELive
except ImportError:
    sys.exit(
        "Missing dependency. Install with:\n"
        "  pip install jugaad-data pandas --break-system-packages"
    )

UNDERLYING_INFO_URL = "https://www.nseindia.com/api/underlying-information"
REQUEST_DELAY_SEC = 1.0   # politeness delay between symbols - don't lower this much
MAX_RETRIES = 2


@dataclass
class OIBuildup:
    symbol: str
    option_type: str        # "CE" or "PE"
    strike_price: float
    expiry_date: str
    open_interest: float
    change_in_oi: float
    pct_change_oi: float
    ltp: float
    volume: int
    underlying_value: float


def get_fno_universe(nse: NSELive):
    """Return (stock_symbols, index_symbols) currently listed in NSE's F&O segment."""
    resp = nse.s.get(UNDERLYING_INFO_URL, timeout=10)
    resp.raise_for_status()
    data = resp.json()["data"]
    stocks = [item["symbol"] for item in data["UnderlyingList"]]
    indices = [item["symbol"] for item in data["IndexList"]]
    return stocks, indices


def fetch_option_chain(nse: NSELive, symbol: str, is_index: bool) -> Optional[dict]:
    """Fetch raw option-chain JSON for one symbol, with a couple of retries."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            if is_index:
                return nse.index_option_chain(symbol)
            return nse.equities_option_chain(symbol)
        except Exception as exc:
            if attempt == MAX_RETRIES:
                print(f"failed after {MAX_RETRIES + 1} attempts ({exc})", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def scan_symbol(nse: NSELive, symbol: str, is_index: bool, threshold_pct: float) -> List[OIBuildup]:
    chain = fetch_option_chain(nse, symbol, is_index)
    if not chain:
        return []

    rows = chain.get("records", {}).get("data", [])
    hits = []
    for row in rows:
        for side in ("CE", "PE"):
            leg = row.get(side)
            if not leg:
                continue
            pct = leg.get("pchangeinOpenInterest")
            if pct is None:
                continue
            if abs(pct) > threshold_pct:
                hits.append(OIBuildup(
                    symbol=symbol,
                    option_type=side,
                    strike_price=leg.get("strikePrice", row.get("strikePrice")),
                    expiry_date=leg.get("expiryDate", row.get("expiryDates", "")),
                    open_interest=leg.get("openInterest", 0),
                    change_in_oi=leg.get("changeinOpenInterest", 0),
                    pct_change_oi=pct,
                    ltp=leg.get("lastPrice", 0),
                    volume=leg.get("totalTradedVolume", 0),
                    underlying_value=leg.get("underlyingValue", 0),
                ))
    return hits


def scan_all(threshold_pct: float = 2000.0, symbols: Optional[List[str]] = None,
             include_indices: bool = False) -> List[OIBuildup]:
    nse = NSELive()  # opens & warms up a session against nseindia.com

    stock_universe, index_universe = get_fno_universe(nse)
    index_set: Set[str] = set(index_universe)

    if symbols is None:
        symbols = stock_universe + (index_universe if include_indices else [])

    all_hits: List[OIBuildup] = []
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{total}] {sym:<12}", end=" ", flush=True)
        hits = scan_symbol(nse, sym, is_index=sym in index_set, threshold_pct=threshold_pct)
        print(f"{len(hits)} hit(s)" if hits else "-")
        all_hits.extend(hits)
        time.sleep(REQUEST_DELAY_SEC)

    all_hits.sort(key=lambda h: abs(h.pct_change_oi), reverse=True)
    return all_hits


def save_csv(hits: List[OIBuildup], path: str):
    if not hits:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(hits[0]).keys()))
        writer.writeheader()
        for h in hits:
            writer.writerow(asdict(h))


def print_table(hits: List[OIBuildup]):
    if not hits:
        print("\nNo strikes crossed the threshold.")
        return
    print(f"\n{'SYMBOL':<12}{'TYPE':<5}{'STRIKE':>10}{'EXPIRY':>14}{'OI':>10}{'CHG OI':>10}{'% CHG OI':>12}{'LTP':>10}")
    print("-" * 87)
    for h in hits:
        print(f"{h.symbol:<12}{h.option_type:<5}{h.strike_price:>10}{h.expiry_date:>14}"
              f"{int(h.open_interest):>10}{int(h.change_in_oi):>10}{h.pct_change_oi:>11.1f}%{h.ltp:>10}")


def send_telegram_alert(hits: List[OIBuildup], bot_token: str, chat_id: str):
    """Optional: push hits to a Telegram chat. Needs `requests`."""
    import requests
    if not hits:
        return
    lines = ["*NSE OI Buildup Alert*"]
    for h in hits[:20]:  # Telegram messages have a length cap
        lines.append(
            f"{h.symbol} {h.option_type} {h.strike_price} ({h.expiry_date}): "
            f"{h.pct_change_oi:+.0f}% OI change, OI={int(h.open_interest)}"
        )
    text = "\n".join(lines)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)


def main():
    parser = argparse.ArgumentParser(description="Scan NSE F&O stocks for extreme OI buildup.")
    parser.add_argument("--threshold", type=float, default=2000.0,
                         help="Minimum absolute %% change in OI to flag (default: 2000)")
    parser.add_argument("--symbols", type=str, default=None,
                         help="Comma-separated symbols to scan instead of the full F&O list")
    parser.add_argument("--include-indices", action="store_true",
                         help="Also scan NIFTY/BANKNIFTY/etc. index options")
    parser.add_argument("--out", type=str, default=None,
                         help="Path to save results as CSV (default: oi_buildup_<timestamp>.csv)")
    parser.add_argument("--telegram-bot-token", type=str, default=None)
    parser.add_argument("--telegram-chat-id", type=str, default=None)
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    print(f"Scanning NSE F&O {'symbols' if symbols else 'universe'} "
          f"for |OI change| > {args.threshold}%...\n")
    hits = scan_all(threshold_pct=args.threshold, symbols=symbols, include_indices=args.include_indices)

    print_table(hits)

    out_path = args.out or f"oi_buildup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    save_csv(hits, out_path)
    if hits:
        print(f"\nSaved {len(hits)} row(s) to {out_path}")

    if args.telegram_bot_token and args.telegram_chat_id:
        send_telegram_alert(hits, args.telegram_bot_token, args.telegram_chat_id)
        print("Sent Telegram alert.")


if __name__ == "__main__":
    main()
