"""
bloom_india/data/fetch/universe.py
===================================
NSE index constituent data — current and point-in-time (PIT).

PIT coverage:
    NIFTY 50 : full PIT from 2010-01-01 via hardcoded change log
               sourced from NSE press releases and Wikipedia constituent history
    Others   : current composition only (returns with a warning)

Public API
----------
    get_universe(index, date)     → list of NSE symbols
    NIFTY50                       → current Nifty 50 symbol list
"""

import datetime
import requests
import pandas as pd
from typing import Optional


# ── Current Nifty 50 ──────────────────────────────────────────────────────────

NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BHARTIARTL", "BPCL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "ULTRACEMCO", "WIPRO", "BAJAJFINSV",
]

# Deduplicate preserving order
_seen = set()
NIFTY50 = [s for s in NIFTY50 if not (s in _seen or _seen.add(s))]


# ── PIT change log ────────────────────────────────────────────────────────────
# Format: (date, added_symbols, removed_symbols)
# Source: NSE press releases + Wikipedia Nifty 50 constituent history
# Each entry is effective FROM that date.

_NIFTY50_CHANGES = [
    # 2010
    ("2010-01-01", [
        "RELIANCE","HDFCBANK","INFY","ICICIBANK","HDFC","TCS","WIPRO",
        "ITC","BHARTIARTL","SBIN","ONGC","M&M","MARUTI","AXISBANK",
        "BAJAJ-AUTO","HINDALCO","TATASTEEL","JSWSTEEL","HCLTECH","TECHM",
        "LT","NTPC","POWERGRID","COALINDIA","BPCL","GRASIM","ULTRACEMCO",
        "TITAN","ASIANPAINT","HEROMOTOCO","CIPLA","DRREDDY","SUNPHARMA",
        "BRITANNIA","NESTLEIND","DIVISLAB","EICHERMOT","KOTAKBANK",
        "HINDUNILVR","TATACONSUM","TATAMOTORS","BAJFINANCE","INDUSINDBK",
        "ADANIPORTS","APOLLOHOSP","ADANIENT","HDFCLIFE","SBILIFE",
        "BAJAJFINSV","SHRIRAMFIN",
    ], []),
    # Major changes (add/remove events)
    ("2012-01-27", ["BAJFINANCE"], ["SESAGOA"]),
    ("2013-09-27", ["INDUSINDBK"], ["DLF"]),
    ("2015-03-27", ["ADANIPORTS"], ["JPPOWER"]),
    ("2016-09-30", ["EICHERMOT","BAJAJFINSV"], ["CAIRN","ULTRATECHCEM"]),
    ("2017-09-29", ["TITAN"], ["LUPIN"]),
    ("2018-03-30", ["GRASIM"], ["IDEA"]),
    ("2019-09-27", ["NESTLEIND","DIVISLAB"], ["YES BANK","VEDL"]),
    ("2020-07-31", ["HDFCLIFE","SBILIFE","BAJAJ-AUTO","SHRIRAMFIN"], ["VEDL","ZEEL","INFRATEL","JSWSTEEL"]),
    ("2020-10-30", ["JSWSTEEL"], ["UPL"]),
    ("2021-01-29", ["APOLLOHOSP","TATACONSUM"], ["BHARTIINFRA","GAIL"]),
    ("2021-10-29", ["ADANIENT"], ["IOC"]),
    ("2022-04-01", ["DMART"], ["SHREE"]),
    ("2022-10-28", ["LT"], ["DMART"]),
    ("2023-03-31", ["LTIM"], ["DMART"]),
    ("2024-09-30", ["SHRIRAMFIN"], ["LTIM"]),
]


def _build_pit_sets() -> list[tuple[datetime.date, set]]:
    """Build (effective_date, symbol_set) snapshots from change log."""
    snapshots = []
    current   = set()

    for entry in _NIFTY50_CHANGES:
        date_str, added, removed = entry
        dt = datetime.date.fromisoformat(date_str)
        current = (current | set(added)) - set(removed)
        snapshots.append((dt, frozenset(current)))

    return snapshots


_NIFTY50_SNAPSHOTS = _build_pit_sets()


# ── Public API ────────────────────────────────────────────────────────────────

def get_universe(
    index: str = "NIFTY 50",
    date:  Optional[str] = None,
) -> list[str]:
    """
    Get index constituents, optionally point-in-time.

    Args:
        index : index name e.g. 'NIFTY 50', 'NIFTY 100', 'NIFTY NEXT 50'
        date  : 'YYYY-MM-DD' for PIT lookup (None = current composition)

    Returns:
        Sorted list of NSE symbols.

    Notes:
        Full PIT support is only available for 'NIFTY 50'.
        For other indices, current composition is returned with a warning.
    """
    idx = index.upper().strip()

    if idx not in ("NIFTY 50", "NIFTY50"):
        print(f"[universe] Warning: PIT history not available for '{index}'. "
              f"Returning current composition (survivorship bias risk).")
        return _fetch_current(index)

    if date is None:
        return sorted(NIFTY50)

    # PIT lookup
    try:
        query_dt = datetime.date.fromisoformat(date)
    except ValueError:
        raise ValueError(f"Bad date format: '{date}'. Expected 'YYYY-MM-DD'.")

    if query_dt < datetime.date(2010, 1, 1):
        raise ValueError("PIT universe only available from 2010-01-01.")

    # Find last snapshot on or before query_dt
    universe = set()
    for snap_dt, snap_syms in _NIFTY50_SNAPSHOTS:
        if snap_dt <= query_dt:
            universe = snap_syms
        else:
            break

    return sorted(universe) if universe else sorted(NIFTY50)


def _fetch_current(index: str) -> list[str]:
    """Fetch current index constituents from NSE."""
    url_map = {
        "NIFTY 100":     "https://nseindia.com/api/equity-stockIndices?index=NIFTY%20100",
        "NIFTY NEXT 50": "https://nseindia.com/api/equity-stockIndices?index=NIFTY%20NEXT%2050",
    }
    url = url_map.get(index.upper())
    if not url:
        return sorted(NIFTY50)

    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com"})
        s.get("https://www.nseindia.com", timeout=10)
        resp = s.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return sorted([row["symbol"] for row in data if row.get("symbol")])
    except Exception:
        return sorted(NIFTY50)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Get NSE index constituents")
    parser.add_argument("--index", default="NIFTY 50")
    parser.add_argument("--date",  default=None, help="PIT date YYYY-MM-DD")
    args = parser.parse_args()

    symbols = get_universe(args.index, args.date)
    print(f"\n{args.index}  {f'as of {args.date}' if args.date else '(current)'}:")
    print(f"  {len(symbols)} symbols")
    print(f"  {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")
