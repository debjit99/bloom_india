"""
bloom_india/scripts/build_price_db.py
=======================================
One-time script to build the price database from NSE bhavcopy archive.

Downloads all trading days from date_floor to today, filters to
requested symbols, saves as a single parquet file.

Run:
    python -m bloom_india.scripts.build_price_db
    python -m bloom_india.scripts.build_price_db --from 2020-01-01
    python -m bloom_india.scripts.build_price_db --symbols HDFCBANK TCS INFY
"""

import argparse
import datetime
import pandas as pd
from pathlib import Path

from bloom_india.config import CONFIG, ensure_dirs, print_config
from bloom_india.data.fetch.universe import NIFTY50
from bloom_india.data.fetch.bhavcopy import fetch_bhavcopy_range


def build(
    symbols:    list  = NIFTY50,
    from_date:  str   = None,
    to_date:    str   = None,
    verbose:    bool  = True,
) -> pd.DataFrame:
    """
    Download NSE bhavcopy for all trading days and save to price_db.

    If price_db already exists, loads it and only fetches missing dates.
    """
    ensure_dirs()
    out_path  = Path(CONFIG.storage.price_db)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    from_date = from_date or CONFIG.data.price_floor
    to_date   = to_date   or datetime.date.today().strftime("%Y-%m-%d")

    print("\n" + "="*60)
    print("  bloom_india — Build Price Database")
    print("="*60)
    print_config()
    print(f"\n  Symbols  : {len(symbols)}")
    print(f"  Range    : {from_date} → {to_date}")
    print(f"  Output   : {out_path}\n")

    # ── Load existing data ────────────────────────────────────────────────────
    existing = pd.DataFrame()
    if out_path.exists():
        existing = pd.read_parquet(out_path)
        existing["Date"] = pd.to_datetime(existing["Date"])
        last_date        = existing["Date"].max()
        # Only fetch dates after last known date
        from_date = (last_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"  Existing data up to {last_date.date()}. Fetching from {from_date}...")

        if from_date > to_date:
            print("  Already up to date.")
            return existing

    # ── Fetch new data ────────────────────────────────────────────────────────
    new_df = fetch_bhavcopy_range(
        from_date = from_date,
        to_date   = to_date,
        symbols   = symbols,
        verbose   = verbose,
    )

    if new_df.empty:
        print("  No new data fetched.")
        return existing

    # ── Combine and save ──────────────────────────────────────────────────────
    if not existing.empty:
        df = pd.concat([existing, new_df], ignore_index=True)
        df = df.drop_duplicates(subset=["Date","Symbol"]).sort_values(
            ["Date","Symbol"]
        ).reset_index(drop=True)
    else:
        df = new_df.sort_values(["Date","Symbol"]).reset_index(drop=True)

    df.to_parquet(out_path, index=False)

    print(f"\n{'='*60}")
    print(f"  Done.")
    print(f"  Rows      : {len(df)}")
    print(f"  Symbols   : {df['Symbol'].nunique()}")
    print(f"  Date range: {df['Date'].min().date()} → {df['Date'].max().date()}")
    print(f"  Saved     : {out_path}")
    print(f"{'='*60}\n")

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build price database from NSE bhavcopy archive"
    )
    parser.add_argument("--from",    dest="from_date", default=None,
                        help="Start date YYYY-MM-DD (default: 2010-01-01)")
    parser.add_argument("--to",      dest="to_date",   default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--symbols", nargs="+",        default=None,
                        help="Symbols to include (default: Nifty 50)")
    parser.add_argument("--quiet",   action="store_true")
    args = parser.parse_args()

    symbols = [s.upper() for s in args.symbols] if args.symbols else NIFTY50

    build(
        symbols   = symbols,
        from_date = args.from_date,
        to_date   = args.to_date,
        verbose   = not args.quiet,
    )
