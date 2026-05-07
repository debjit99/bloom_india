"""
bloom_india/data/fetch/announcements.py
========================================
Downloads result announcement dates from BSE for all symbols.
These dates are critical for point-in-time safety — they tell us
exactly when the market first saw each quarterly result.

Saves to: CONFIG.storage.announce_dates_file (JSON)
Format:   {symbol: {quarter_label: "YYYY-MM-DD", ...}, ...}

Public API
----------
    fetch_announce_dates(symbol)        → {quarter_label: date_str}
    fetch_and_save_all(symbols)         → saves JSON, returns dict
    load_announce_dates()               → load saved JSON
"""

import re
import time
import json
import random
import datetime
import pandas as pd
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Quarter label parser ──────────────────────────────────────────────────────

def _quarter_from_headline(headline: str, news_dt: str) -> str:
    """
    Parse 'Q3_FY2025' from a BSE announcement headline + date.

    BSE headlines look like:
      "Unaudited Financial Results for the third quarter ended December 31, 2024"
      "Audited Financial Results for the year ended March 31, 2024"
    """
    h = headline.lower()

    if any(x in h for x in ["first quarter", "june 30", "jun 30"]):
        q = "Q1"
    elif any(x in h for x in ["second quarter", "september 30", "sep 30",
                                "half year", "half-year"]):
        q = "Q2"
    elif any(x in h for x in ["third quarter", "december 31", "dec 31",
                                "nine months"]):
        q = "Q3"
    elif any(x in h for x in ["fourth quarter", "march 31", "mar 31",
                                "year ended", "annual"]):
        q = "Q4"
    else:
        # Fallback: infer quarter from announcement month
        try:
            m = pd.to_datetime(news_dt).month
            q = {
                4:"Q4", 5:"Q4", 6:"Q4",
                7:"Q1", 8:"Q1", 9:"Q1",
                10:"Q2", 11:"Q2", 12:"Q2",
                1:"Q3", 2:"Q3", 3:"Q3",
            }[m]
        except Exception:
            return ""

    try:
        dt = pd.to_datetime(news_dt)
        fy = dt.year + 1 if dt.month >= 4 else dt.year
        if q == "Q4" and dt.month >= 4:
            fy = dt.year
    except Exception:
        return ""

    return f"{q}_FY{fy}"


# ── Core fetch ────────────────────────────────────────────────────────────────

def fetch_announce_dates(symbol: str) -> dict[str, str]:
    """
    Fetch all result announcement dates for one symbol from BSE.

    Returns:
        {quarter_label: announce_date_str}
        e.g. {"Q3_FY2025": "2025-01-22", "Q2_FY2025": "2024-10-19", ...}

    Requires the `bse` package:
        pip install bse
    """
    try:
        from bse import BSE
    except ImportError:
        raise ImportError(
            "pip install bse\n"
            "The BSE package is required for fetching announcement dates."
        )

    dates = {}

    try:
        with BSE(download_folder=str(CONFIG.storage.raw_dir)) as bse:
            scrip = bse.getScripCode(symbol)
            if not scrip:
                return {}

            data = bse.announcements(
                scripcode = scrip,
                category  = "Result",
                from_date = pd.Timestamp("2014-01-01"),
                to_date   = pd.Timestamp("2025-12-31"),
            )
            rows = data.get("Table", [])

            for row in rows:
                headline = row.get("HEADLINE", "")
                news_dt  = row.get("NEWS_DT", "")[:10]
                ql       = _quarter_from_headline(headline, news_dt)

                if ql and news_dt:
                    # Keep earliest date for duplicates (first filing = announcement)
                    if ql not in dates or news_dt < dates[ql]:
                        dates[ql] = news_dt

    except Exception as e:
        print(f"  [announcements] {symbol}: {e}")

    return dates


# ── Fetch all symbols ─────────────────────────────────────────────────────────

def fetch_and_save_all(
    symbols:   list[str],
    out_file:  Optional[str] = None,
    force:     bool = False,
    verbose:   bool = True,
) -> dict[str, dict[str, str]]:
    """
    Fetch announcement dates for all symbols and save to JSON.

    Args:
        symbols  : list of NSE symbols
        out_file : path to save JSON (default: CONFIG.storage.announce_dates_file)
        force    : re-fetch even if already saved
        verbose  : print progress

    Returns:
        {symbol: {quarter_label: date_str}}
    """
    save_path = Path(out_file) if out_file else Path(CONFIG.storage.announce_dates_file)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing
    existing: dict = {}
    if save_path.exists() and not force:
        with open(save_path) as f:
            existing = json.load(f)
        if verbose:
            print(f"Loaded {len(existing)} cached symbols from {save_path}")

    if verbose:
        print(f"Fetching announcement dates for {len(symbols)} symbols")
        print("-" * 50)

    for i, symbol in enumerate(symbols, 1):
        if symbol in existing and not force:
            if verbose:
                print(f"  [{i:2d}/{len(symbols)}] {symbol:<15} [cached] "
                      f"{len(existing[symbol])} quarters")
            continue

        print(f"  [{i:2d}/{len(symbols)}] {symbol:<15}", end="  ")

        dates = fetch_announce_dates(symbol)
        existing[symbol] = dates
        print(f"{len(dates)} quarters")

        # Save after each symbol — never lose work
        with open(save_path, "w") as f:
            json.dump(existing, f, indent=2)

        time.sleep(random.uniform(1.0, 2.0))

    total = sum(len(v) for v in existing.values())
    if verbose:
        print(f"\nDone. {total} announcement dates across {len(existing)} symbols.")
        print(f"Saved: {save_path}")

    return existing


# ── Load saved dates ──────────────────────────────────────────────────────────

def load_announce_dates(
    file_path: Optional[str] = None,
) -> dict[str, dict[str, str]]:
    """
    Load announcement dates from saved JSON.

    Returns:
        {symbol: {quarter_label: date_str}}
    """
    path = Path(file_path) if file_path else Path(CONFIG.storage.announce_dates_file)

    if not path.exists():
        raise FileNotFoundError(
            f"Announcement dates file not found: {path}\n"
            "Run fetch_and_save_all() first."
        )

    with open(path) as f:
        return json.load(f)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch BSE announcement dates")
    parser.add_argument("--symbol",  default="", help="Single NSE symbol")
    parser.add_argument("--symbols", nargs="+",  help="List of symbols")
    parser.add_argument("--out",     default="", help="Output JSON file path")
    parser.add_argument("--force",   action="store_true")
    args = parser.parse_args()

    from bloom_india.config import ensure_dirs
    ensure_dirs()

    if args.symbol:
        dates = fetch_announce_dates(args.symbol.upper())
        print(f"\n{args.symbol} announcement dates:")
        for ql, dt in sorted(dates.items()):
            print(f"  {ql:<15} {dt}")

    else:
        if args.symbols:
            symbols = [s.upper() for s in args.symbols]
        else:
            from bloom_india.data.fetch.universe import NIFTY50
            symbols = NIFTY50

        fetch_and_save_all(
            symbols  = symbols,
            out_file = args.out or None,
            force    = args.force,
        )
