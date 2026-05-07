"""
bloom_india/data/api/fundamentals.py
=====================================
Public point-in-time fundamental data API.

Every row returned is tagged with announce_date — the date the market
first saw that filing. Queries are always filtered to announce_date <= date,
guaranteeing no look-ahead bias in backtests.

Public API
----------
    get_fundamentals(date_str)                        → pd.DataFrame
    get_fundamentals_history(symbols, from, to, freq) → pd.DataFrame
    refresh_cache()                                   → None

Date floor: 2018-04-01 (earliest reliable NSE XBRL data)

Raises
------
    DateFloorError  : date before 2018-04-01
    ValueError      : bad date string format
    FileNotFoundError : fundamental_db not built yet
"""

import datetime
import pandas as pd
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Constants ─────────────────────────────────────────────────────────────────

DATE_FLOOR = datetime.date(2018, 4, 1)


# ── Exceptions ────────────────────────────────────────────────────────────────

class DateFloorError(Exception):
    """Date is before the fundamental data floor of 2018-04-01."""
    pass


# ── In-memory cache ───────────────────────────────────────────────────────────

_db: Optional[pd.DataFrame] = None


def _load() -> pd.DataFrame:
    global _db
    if _db is not None:
        return _db

    path = Path(CONFIG.storage.fundamental_db)
    if not path.exists():
        raise FileNotFoundError(
            f"Fundamental database not found: {path}\n"
            "Run:  python -m bloom_india.scripts.build_fundamental_db"
        )

    _db = pd.read_parquet(path)
    _db["announce_date"] = pd.to_datetime(_db["announce_date"])
    _db["period_end"]    = pd.to_datetime(_db["period_end"], errors="coerce")
    return _db


def refresh_cache() -> None:
    """Reload the fundamental database from disk. Call after rebuilding."""
    global _db
    _db = None
    _load()
    print(f"[fundamentals] Cache refreshed — {len(_db)} rows.")


# ── Public API ────────────────────────────────────────────────────────────────

def get_fundamentals(
    date_str: str,
) -> pd.DataFrame:
    """
    Point-in-time fundamental snapshot for all available stocks as of date_str.

    Returns the most recent quarterly filing per symbol where
    announce_date <= date_str. This is exactly what was publicly
    known on that date — safe for backtesting.

    Args:
        date_str : ISO date 'YYYY-MM-DD'

    Returns:
        DataFrame — one row per symbol, sorted by symbol.
        Columns include: symbol, announce_date, period_end, quarter_label,
        revenue, pat, eps_basic, pat_margin, sue, revenue_yoy, ... and all
        other fundamental + factor columns.

        Empty DataFrame if no data is available before date_str.

    Raises:
        DateFloorError : date before 2018-04-01
        ValueError     : bad date_str format

    Example:
        df = get_fundamentals("2024-01-25")
        scores = df.set_index("symbol")["sue"]
    """
    try:
        dt = datetime.date.fromisoformat(date_str)
    except ValueError:
        raise ValueError(
            f"Bad date format: '{date_str}'. Expected 'YYYY-MM-DD'."
        )

    if dt < DATE_FLOOR:
        raise DateFloorError(
            f"Date {date_str} is before the data floor {DATE_FLOOR}. "
            "NSE XBRL data is not available before 2018-04-01."
        )

    db     = _load()
    cutoff = pd.Timestamp(date_str)

    pit = db[db["announce_date"] <= cutoff].copy()
    if pit.empty:
        return pd.DataFrame()

    # Latest filing per symbol
    pit = (
        pit.sort_values("announce_date")
           .groupby("symbol")
           .last()
           .reset_index()
    )

    return pit.sort_values("symbol").reset_index(drop=True)


def get_fundamentals_history(
    symbols:   list,
    from_date: str,
    to_date:   str,
    freq:      str = "QS",
) -> pd.DataFrame:
    """
    Fundamental panel across multiple dates for a list of symbols.

    Useful for building ML feature matrices or time-series analysis.

    Args:
        symbols   : list of NSE symbols e.g. ['HDFCBANK', 'TCS']
        from_date : 'YYYY-MM-DD'
        to_date   : 'YYYY-MM-DD'
        freq      : pandas date frequency for query dates
                    'QS' = quarter start, 'MS' = month start, 'W' = weekly

    Returns:
        Long DataFrame with columns: as_of_date, symbol, + all fundamental cols
        Sorted by symbol, as_of_date.

    Example:
        panel = get_fundamentals_history(
            ["HDFCBANK", "TCS"],
            from_date = "2020-01-01",
            to_date   = "2024-12-31",
            freq      = "MS",
        )
    """
    dates  = pd.date_range(from_date, to_date, freq=freq)
    frames = []

    for dt in dates:
        ds = dt.strftime("%Y-%m-%d")
        try:
            df = get_fundamentals(ds)
        except DateFloorError:
            continue
        if df.empty:
            continue
        df = df[df["symbol"].isin(symbols)].copy()
        if df.empty:
            continue
        df.insert(0, "as_of_date", pd.Timestamp(ds))
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    return (
        pd.concat(frames, ignore_index=True)
          .sort_values(["symbol", "as_of_date"])
          .reset_index(drop=True)
    )


# ── Warm up on import ─────────────────────────────────────────────────────────

try:
    _load()
except FileNotFoundError:
    pass  # DB not built yet — will load on first call
