"""
bloom_india/scripts/update_daily.py
=====================================
Daily update script — fetches latest prices and any new quarterly filings.

Run as a cron job after market close (e.g. 6pm IST):
    0 18 * * 1-5 cd /path/to/bloom_india && python -m bloom_india.scripts.update_daily

Or manually:
    python -m bloom_india.scripts.update_daily
    python -m bloom_india.scripts.update_daily --dry-run   # show what would be fetched
"""

import argparse
import datetime
import pandas as pd
from pathlib import Path

from bloom_india.config import CONFIG, ensure_dirs
from bloom_india.data.fetch.universe import NIFTY50
from bloom_india.data.fetch.bhavcopy import fetch_bhavcopy
from bloom_india.data.fetch.xbrl import fetch_and_save_all as fetch_xbrl
from bloom_india.data.fetch.announcements import fetch_and_save_all as fetch_dates


def update_prices(dry_run: bool = False) -> int:
    """Fetch today's bhavcopy and append to price_db. Returns rows added."""
    today    = datetime.date.today()
    out_path = Path(CONFIG.storage.price_db)

    print(f"\n[prices] Fetching {today}...")

    if dry_run:
        print(f"  [dry-run] Would fetch bhavcopy for {today}")
        return 0

    df = fetch_bhavcopy(today.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        print(f"  No data for {today} (holiday or weekend)")
        return 0

    # Filter to Nifty 50
    df = df[df["Symbol"].isin(NIFTY50)]

    if out_path.exists():
        existing = pd.read_parquet(out_path)
        existing["Date"] = pd.to_datetime(existing["Date"])
        combined = pd.concat([existing, df], ignore_index=True)
        combined = combined.drop_duplicates(
            subset=["Date","Symbol"]
        ).sort_values(["Date","Symbol"]).reset_index(drop=True)
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        combined = df

    combined.to_parquet(out_path, index=False)
    print(f"  Added {len(df)} rows. DB now {len(combined)} rows.")
    return len(df)


def update_fundamentals(dry_run: bool = False) -> int:
    """
    Check for new XBRL filings and rebuild fundamental_db if any found.
    Returns number of new filings found.
    """
    print(f"\n[fundamentals] Checking for new filings...")

    if dry_run:
        print(f"  [dry-run] Would check NSE for new XBRL filings")
        return 0

    # Fetch new filings (skips already downloaded)
    results = fetch_xbrl(
        symbols  = NIFTY50,
        xbrl_dir = str(CONFIG.storage.xbrl_dir),
        force    = False,
        verbose  = False,
    )
    new_total = sum(results.values())

    if new_total == 0:
        print(f"  No new filings found.")
        return 0

    print(f"  {new_total} new filings downloaded. Rebuilding fundamental_db...")

    # Also update announce dates
    fetch_dates(
        symbols  = NIFTY50,
        out_file = str(CONFIG.storage.announce_dates_file),
        force    = False,
        verbose  = False,
    )

    # Rebuild the database
    from bloom_india.scripts.build_fundamental_db import build
    build(symbols=NIFTY50, fetch=False, verbose=False)

    print(f"  fundamental_db rebuilt.")
    return new_total


def run(dry_run: bool = False):
    ensure_dirs()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*55}")
    print(f"  bloom_india daily update  —  {now}")
    print(f"{'='*55}")

    price_rows = update_prices(dry_run)
    new_filings = update_fundamentals(dry_run)

    print(f"\n{'─'*55}")
    print(f"  Summary:")
    print(f"    Price rows added  : {price_rows}")
    print(f"    New XBRL filings  : {new_filings}")
    print(f"    DB rebuilt        : {'Yes' if new_filings > 0 else 'No'}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily bloom_india update")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without doing it")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
