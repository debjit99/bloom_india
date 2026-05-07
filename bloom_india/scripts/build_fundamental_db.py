"""
bloom_india/scripts/build_fundamental_db.py
=============================================
One-time script to build the fundamental database from NSE XBRL filings.

Steps:
  1. Fetch XBRL filings from NSE  (skips already downloaded)
  2. Fetch announcement dates from BSE
  3. Parse XBRL JSONs → raw DataFrame
  4. Fix cumulative YTD values → standalone quarterly
  5. Fix cumulative EPS
  6. Merge announcement dates
  7. Compute factors (YoY, SUE, margins, NII...)
  8. Save to CONFIG.storage.fundamental_db

Run:
    python -m bloom_india.scripts.build_fundamental_db
    python -m bloom_india.scripts.build_fundamental_db --fetch   # re-download
    python -m bloom_india.scripts.build_fundamental_db --symbol HDFCBANK
"""

import argparse
import pandas as pd
from pathlib import Path

from bloom_india.config import CONFIG, ensure_dirs, print_config
from bloom_india.data.fetch.universe import NIFTY50
from bloom_india.data.fetch.xbrl import fetch_and_save_all as fetch_xbrl
from bloom_india.data.fetch.announcements import (
    fetch_and_save_all as fetch_dates,
    load_announce_dates,
)
from bloom_india.data.process.xbrl_parse import parse_xbrl_cache
from bloom_india.data.process.cumulative_fix import fix_cumulative, fix_eps
from bloom_india.data.process.factors import compute_factors


def merge_announce_dates(df: pd.DataFrame, dates: dict) -> pd.DataFrame:
    """Add announce_date column from BSE data. Estimate missing as period_end + 21d."""
    df = df.copy()

    def lookup(row):
        return dates.get(row["symbol"], {}).get(row["quarter_label"])

    df["announce_date"] = df.apply(lookup, axis=1)
    df["announce_date"] = pd.to_datetime(df["announce_date"], errors="coerce")

    filled = df["announce_date"].notna().sum()
    print(f"  Announce dates filled: {filled}/{len(df)} ({filled/len(df)*100:.1f}%)")

    # Estimate missing — period_end + 21 days (typical India reporting lag)
    mask = df["announce_date"].isna()
    df.loc[mask, "announce_date"]           = df.loc[mask, "period_end"] + pd.Timedelta(days=21)
    df.loc[mask, "announce_date_estimated"] = True
    df["announce_date_estimated"]           = df.get(
        "announce_date_estimated", False
    ).fillna(False)

    print(f"  Estimated (period_end+21d): {mask.sum()} rows")
    return df


def build(
    symbols:      list  = NIFTY50,
    fetch:        bool  = False,
    verbose:      bool  = True,
) -> pd.DataFrame:
    """Full fundamental DB build pipeline."""

    ensure_dirs()
    out_path = Path(CONFIG.storage.fundamental_db)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("\n" + "="*60)
    print("  bloom_india — Build Fundamental Database")
    print("="*60)
    print_config()

    # ── Step 1: Fetch XBRL ───────────────────────────────────────────────────
    print("\n[1/7] Fetching XBRL filings from NSE...")
    fetch_xbrl(
        symbols  = symbols,
        xbrl_dir = str(CONFIG.storage.xbrl_dir),
        force    = fetch,
        verbose  = verbose,
    )

    # ── Step 2: Fetch announcement dates ─────────────────────────────────────
    print("\n[2/7] Fetching announcement dates from BSE...")
    fetch_dates(
        symbols  = symbols,
        out_file = str(CONFIG.storage.announce_dates_file),
        force    = fetch,
        verbose  = verbose,
    )

    # ── Step 3: Parse XBRL cache ─────────────────────────────────────────────
    print("\n[3/7] Parsing XBRL cache...")
    df = parse_xbrl_cache(
        xbrl_dir = str(CONFIG.storage.xbrl_dir),
        symbols  = symbols,
        verbose  = verbose,
    )
    print(f"  Parsed: {len(df)} rows, {df['symbol'].nunique()} symbols")

    # ── Step 4: Fix cumulative values ─────────────────────────────────────────
    print("\n[4/7] Fixing cumulative YTD values...")
    df = fix_cumulative(df)

    # ── Step 5: Fix EPS ───────────────────────────────────────────────────────
    print("\n[5/7] Fixing cumulative EPS...")
    df = fix_eps(df)

    # ── Step 6: Merge announcement dates ──────────────────────────────────────
    print("\n[6/7] Merging announcement dates...")
    dates = load_announce_dates(str(CONFIG.storage.announce_dates_file))
    df    = merge_announce_dates(df, dates)

    # ── Step 7: Compute factors ───────────────────────────────────────────────
    print("\n[7/7] Computing factors...")
    df = compute_factors(df)

    # ── Save ──────────────────────────────────────────────────────────────────
    df.to_parquet(out_path, index=False)
    df.to_csv(str(out_path).replace(".parquet", ".csv"), index=False)

    print(f"\n{'='*60}")
    print(f"  Done.")
    print(f"  Rows      : {len(df)}")
    print(f"  Symbols   : {df['symbol'].nunique()}")
    print(f"  Date range: {df['period_end'].min().date()} → {df['period_end'].max().date()}")
    print(f"  Saved     : {out_path}")
    print(f"{'='*60}\n")

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build fundamental database from NSE XBRL filings"
    )
    parser.add_argument("--fetch",   action="store_true",
                        help="Re-download XBRL + announce dates even if cached")
    parser.add_argument("--symbol",  default="",
                        help="Single symbol (default: all Nifty 50)")
    parser.add_argument("--quiet",   action="store_true")
    args = parser.parse_args()

    symbols = [args.symbol.upper()] if args.symbol else NIFTY50

    build(
        symbols = symbols,
        fetch   = args.fetch,
        verbose = not args.quiet,
    )
