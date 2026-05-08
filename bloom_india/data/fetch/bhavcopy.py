"""
bloom_india/data/fetch/bhavcopy.py
===================================
Downloads NSE end-of-day OHLCV data from the official NSE bhavcopy archive.

Two URL formats are handled transparently:
  >= 2024-07-08  UDiFF format:
      nsearchives.nseindia.com/content/cm/
      BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip

  2010-01-01 to 2024-07-07  Legacy format:
      nsearchives.nseindia.com/content/historical/EQUITIES/
      {YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip

Public API
----------
    fetch_bhavcopy(date_str)          → pd.DataFrame | None
    fetch_bhavcopy_range(from, to)    → pd.DataFrame
    save_bhavcopy(date_str, out_dir)  → Path | None

Output columns (all dates):
    Date, Symbol, Series, Open, High, Low, Close,
    PrevClose, Volume, Value, ISIN

Date floor: 2010-01-01
Raises:
    DateFloorError  — date before 2010-01-01
    ValueError      — bad date string
"""

import io
import time
import random
import zipfile
import datetime
import requests
import pandas as pd
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Constants ─────────────────────────────────────────────────────────────────

DATE_FLOOR      = datetime.date(2010, 1, 1)
UDIFF_CUTOVER   = datetime.date(2024, 7, 8)

NSE_ARCHIVE     = "https://nsearchives.nseindia.com/content/cm"
NSE_HIST        = "https://nsearchives.nseindia.com/content/historical/EQUITIES"
NSE_HOMEPAGE    = "https://www.nseindia.com"

EQUITY_SERIES   = {"EQ", "BE", "BZ", "SM", "ST"}

# ── Exceptions ────────────────────────────────────────────────────────────────

class DateFloorError(Exception):
    """Date is before the hard floor of 2010-01-01."""
    pass


class BhavcopydataError(Exception):
    """Data was fetched but failed quality checks."""
    pass


# ── Session ───────────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": CONFIG.fetch.user_agent,
        "Referer":    NSE_HOMEPAGE,
        "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    try:
        s.get(NSE_HOMEPAGE, timeout=10)
        time.sleep(random.uniform(0.3, 0.7))
    except Exception:
        pass
    return s


# ── URL builders ──────────────────────────────────────────────────────────────

def _url_udiff(d: datetime.date) -> str:
    return (
        f"{NSE_ARCHIVE}/"
        f"BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
    )


def _url_legacy(d: datetime.date) -> str:
    mon = d.strftime("%b").upper()
    return (
        f"{NSE_HIST}/{d.year}/{mon}/"
        f"cm{d.strftime('%d')}{mon}{d.year}bhav.csv.zip"
    )


# ── Column normalisation ──────────────────────────────────────────────────────

_UDIFF_RENAME = {
    "TradDt":        "Date",
    "TckrSymb":      "Symbol",
    "SctySrs":       "Series",
    "OpnPric":       "Open",
    "HghPric":       "High",
    "LwPric":        "Low",
    "ClsPric":       "Close",
    "PrvsClsgPric":  "PrevClose",
    "TtlTradgVol":   "Volume",
    "TtlTrfVal":     "Value",
    "ISIN":          "ISIN",
}

_LEGACY_RENAME = {
    "SYMBOL":    "Symbol",
    "SERIES":    "Series",
    "OPEN":      "Open",
    "HIGH":      "High",
    "LOW":       "Low",
    "CLOSE":     "Close",
    "PREVCLOSE": "PrevClose",
    "TOTTRDQTY": "Volume",
    "TOTTRDVAL": "Value",
    "ISIN":      "ISIN",
    "TIMESTAMP": "Date",
}

OUTPUT_COLS = ["Date","Symbol","Series","Open","High","Low",
               "Close","PrevClose","Volume","Value","ISIN"]


def _normalise(df: pd.DataFrame, date_str: str, is_udiff: bool) -> pd.DataFrame:
    """Rename columns, set types, filter to equity series."""
    rename = _UDIFF_RENAME if is_udiff else _LEGACY_RENAME
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    # Date
    if "Date" not in df.columns:
        df["Date"] = pd.to_datetime(date_str)
    else:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")

    # Numeric
    for col in ["Open","High","Low","Close","PrevClose","Value"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").astype("Int64")

    # Filter series
    if "Series" in df.columns:
        df = df[df["Series"].isin(EQUITY_SERIES)]

    # Select output columns
    cols = [c for c in OUTPUT_COLS if c in df.columns]
    return df[cols].reset_index(drop=True)


# ── Core fetch ────────────────────────────────────────────────────────────────

def fetch_bhavcopy(
    date_str:   str,
    session:    Optional[requests.Session] = None,
    series:     Optional[set] = None,
) -> Optional[pd.DataFrame]:
    """
    Download NSE bhavcopy for a single trading date.

    Args:
        date_str : ISO date string 'YYYY-MM-DD'
        session  : optional reusable requests.Session (creates one if None)
        series   : equity series to keep (default: EQ, BE, BZ, SM, ST)

    Returns:
        DataFrame with columns Date, Symbol, Series, Open, High, Low,
        Close, PrevClose, Volume, Value, ISIN.
        None if the date is a holiday / weekend (no file on NSE).

    Raises:
        DateFloorError : date before 2010-01-01
        ValueError     : bad date_str format
    """
    # ── Parse date ────────────────────────────────────────────────────────────
    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        raise ValueError(f"Bad date format: '{date_str}'. Expected 'YYYY-MM-DD'.")

    if d < DATE_FLOOR:
        raise DateFloorError(
            f"Date {date_str} is before the floor {DATE_FLOOR}. "
            "NSE bhavcopy is not reliably available before 2010-01-01."
        )

    # Weekend — skip immediately
    if d.weekday() >= 5:
        return None

    # ── Build URL ─────────────────────────────────────────────────────────────
    is_udiff = d >= UDIFF_CUTOVER
    url      = _url_udiff(d) if is_udiff else _url_legacy(d)

    # ── Download with retry ───────────────────────────────────────────────────
    sess         = session or _make_session()
    max_retries  = CONFIG.fetch.max_retries
    timeout      = CONFIG.fetch.timeout_sec
    last_err     = ""

    for attempt in range(max_retries):
        try:
            resp = sess.get(url, timeout=timeout)

            if resp.status_code == 404:
                return None   # holiday or no file

            resp.raise_for_status()

            # ── Parse ZIP ─────────────────────────────────────────────────────
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                csv_name = next(
                    (n for n in z.namelist() if n.lower().endswith(".csv")),
                    None
                )
                if not csv_name:
                    return None
                with z.open(csv_name) as f:
                    df = pd.read_csv(f, low_memory=False)

            if df.empty:
                return None

            df = _normalise(df, date_str, is_udiff)

            # Apply series filter
            keep = series or EQUITY_SERIES
            if "Series" in df.columns:
                df = df[df["Series"].isin(keep)]

            return df if not df.empty else None

        except (requests.RequestException, zipfile.BadZipFile) as e:
            last_err = str(e)
            time.sleep((2 ** attempt) + random.uniform(0.5, 1.5))

    print(f"  [bhavcopy] {date_str} failed after {max_retries} attempts: {last_err}")
    return None


# ── Range fetch (sequential) ──────────────────────────────────────────────────

def fetch_bhavcopy_range(
    from_date:  str,
    to_date:    str,
    symbols:    Optional[list] = None,
    series:     Optional[set]  = None,
    verbose:    bool           = True,
    workers:    int            = 1,
) -> pd.DataFrame:
    """
    Download NSE bhavcopy for a date range.
    Set workers > 1 for parallel fetching (recommended: 4-6).
    """
    if workers > 1:
        return fetch_bhavcopy_range_parallel(
            from_date, to_date,
            symbols=symbols, series=series,
            verbose=verbose, workers=workers,
        )

    start  = datetime.date.fromisoformat(from_date)
    end    = datetime.date.fromisoformat(to_date)
    dates  = pd.date_range(start, end, freq="B")

    if verbose:
        print(f"Fetching bhavcopy: {from_date} → {to_date}  ({len(dates)} business days)")

    sess   = _make_session()
    frames = []
    errors = 0

    for i, dt in enumerate(dates):
        ds = dt.strftime("%Y-%m-%d")
        try:
            df = fetch_bhavcopy(ds, session=sess, series=series)
            if df is not None and not df.empty:
                if symbols:
                    df = df[df["Symbol"].isin(symbols)]
                if not df.empty:
                    frames.append(df)
        except DateFloorError:
            break
        except Exception as e:
            errors += 1
            if verbose:
                print(f"  [error] {ds}: {e}")

        if verbose and (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(dates)} days  frames={len(frames)}  errors={errors}")

        time.sleep(CONFIG.fetch.request_delay_sec)

    if not frames:
        return pd.DataFrame()

    out = (pd.concat(frames, ignore_index=True)
             .sort_values(["Date","Symbol"])
             .reset_index(drop=True))

    if verbose:
        print(f"Done: {len(out)} rows, {out['Symbol'].nunique()} symbols, "
              f"{out['Date'].nunique()} dates, {errors} errors")
    return out


# ── Range fetch (parallel) ────────────────────────────────────────────────────

def fetch_bhavcopy_range_parallel(
    from_date:  str,
    to_date:    str,
    symbols:    Optional[list] = None,
    series:     Optional[set]  = None,
    verbose:    bool           = True,
    workers:    int            = 5,
) -> pd.DataFrame:
    """
    Download NSE bhavcopy for a date range using parallel workers.

    Each worker gets its own NSE session. Dates are split across workers.
    NSE is generally okay with 4-6 parallel connections — don't go above 8.

    Args:
        from_date : 'YYYY-MM-DD'
        to_date   : 'YYYY-MM-DD'
        symbols   : filter to these symbols (default: all)
        series    : equity series filter (default: EQ,BE,BZ,SM,ST)
        verbose   : print progress
        workers   : parallel threads (default 5, max recommended 8)

    Returns:
        Combined DataFrame sorted by Date, Symbol.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start  = datetime.date.fromisoformat(from_date)
    end    = datetime.date.fromisoformat(to_date)
    dates  = [dt.strftime("%Y-%m-%d")
              for dt in pd.date_range(start, end, freq="B")]

    if verbose:
        print(f"Fetching bhavcopy (parallel, {workers} workers): "
              f"{from_date} → {to_date}  ({len(dates)} business days)")

    # Thread-local sessions — one per worker
    _local = threading.local()

    def _get_sess() -> requests.Session:
        if not hasattr(_local, "sess"):
            _local.sess = _make_session()
        return _local.sess

    _lock   = threading.Lock()
    counter = {"done": 0, "frames": 0, "errors": 0}
    frames  = []

    def _fetch_day(ds: str):
        time.sleep(random.uniform(0.05, 0.3))  # small jitter
        try:
            df = fetch_bhavcopy(ds, session=_get_sess(), series=series)
            if df is not None and not df.empty:
                if symbols:
                    df = df[df["Symbol"].isin(symbols)]
                if not df.empty:
                    return ds, df, None
            return ds, None, None
        except Exception as e:
            err = str(e)
            # On rate limit — back off and retry with fresh session
            if any(x in err for x in ["429","403","rate","blocked"]):
                time.sleep(random.uniform(5, 15))
                try:
                    _local.sess = _make_session()
                    df = fetch_bhavcopy(ds, session=_get_sess(), series=series)
                    if df is not None and not df.empty:
                        if symbols:
                            df = df[df["Symbol"].isin(symbols)]
                        return ds, df if not df.empty else None, None
                except Exception as e2:
                    return ds, None, str(e2)
            return ds, None, err

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_day, ds): ds for ds in dates}
        for future in as_completed(futures):
            ds, df, err = future.result()
            with _lock:
                counter["done"] += 1
                if df is not None:
                    frames.append(df)
                    counter["frames"] += 1
                if err:
                    counter["errors"] += 1
                if verbose and counter["done"] % 100 == 0:
                    pct = counter["done"] / len(dates) * 100
                    print(f"  {counter['done']:4d}/{len(dates)}  "
                          f"({pct:5.1f}%)  "
                          f"frames={counter['frames']}  "
                          f"errors={counter['errors']}")

    if not frames:
        return pd.DataFrame()

    out = (pd.concat(frames, ignore_index=True)
             .sort_values(["Date","Symbol"])
             .reset_index(drop=True))

    if verbose:
        print(f"Done: {len(out):,} rows  "
              f"{out['Symbol'].nunique()} symbols  "
              f"{out['Date'].nunique()} dates  "
              f"{counter['errors']} errors")
    return out


# ── Save single day ───────────────────────────────────────────────────────────

def save_bhavcopy(
    date_str: str,
    out_dir:  Optional[str] = None,
) -> Optional[Path]:
    """
    Download and save a single day's bhavcopy as parquet.

    Args:
        date_str : 'YYYY-MM-DD'
        out_dir  : directory to save to (default: CONFIG.storage.bhavcopy_dir)

    Returns:
        Path to saved file, or None if no data (holiday).
    """
    save_dir = Path(out_dir) if out_dir else Path(CONFIG.storage.bhavcopy_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    out_path = save_dir / f"{date_str}.parquet"
    if out_path.exists():
        return out_path   # already saved

    df = fetch_bhavcopy(date_str)
    if df is None or df.empty:
        return None

    df.to_parquet(out_path, index=False)
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch NSE bhavcopy data")
    parser.add_argument("--date",  help="Single date YYYY-MM-DD")
    parser.add_argument("--from",  dest="from_date", help="Start date YYYY-MM-DD")
    parser.add_argument("--to",    dest="to_date",   help="End date YYYY-MM-DD")
    parser.add_argument("--save",  action="store_true", help="Save to bhavcopy_dir")
    args = parser.parse_args()

    if args.date:
        df = fetch_bhavcopy(args.date)
        if df is not None:
            print(f"{args.date}: {len(df)} rows")
            print(df.head())
            if args.save:
                p = save_bhavcopy(args.date)
                print(f"Saved: {p}")
        else:
            print(f"{args.date}: no data (holiday or weekend)")

    elif args.from_date and args.to_date:
        df = fetch_bhavcopy_range(args.from_date, args.to_date)
        print(df.head())
        if args.save:
            out = Path(CONFIG.storage.bhavcopy_dir)
            out.mkdir(parents=True, exist_ok=True)
            p = out / f"{args.from_date}_{args.to_date}.parquet"
            df.to_parquet(p, index=False)
            print(f"Saved: {p}")

    else:
        parser.print_help()