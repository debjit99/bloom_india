"""
bloom_india/data/api/prices.py
================================
Public price data API — single stock time series and cross-sectional panel.

Reads from the pre-built price database (prices.parquet).
Build it first with:  python -m bloom_india.scripts.build_price_db

Public API
----------
    get_ohlcv(symbol, from_date, to_date, adjusted) → pd.DataFrame
    get_price(symbol, date, field, adjusted)         → float | None
    get_panel(symbols, from_date, to_date, field)    → pd.DataFrame (wide)

Date floor: 2010-01-01

Raises
------
    DateFloorError    : date before 2010-01-01
    ValueError        : bad date format
    FileNotFoundError : price_db not built yet
"""

import datetime
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Constants ─────────────────────────────────────────────────────────────────

DATE_FLOOR = datetime.date(2010, 1, 1)


# ── Exceptions ────────────────────────────────────────────────────────────────

class DateFloorError(Exception):
    pass


# ── In-memory cache ───────────────────────────────────────────────────────────

_price_db: Optional[pd.DataFrame] = None


def _load() -> pd.DataFrame:
    global _price_db
    if _price_db is not None:
        return _price_db

    path = Path(CONFIG.storage.price_db)
    if not path.exists():
        raise FileNotFoundError(
            f"Price database not found: {path}\n"
            "Run:  python -m bloom_india.scripts.build_price_db"
        )

    _price_db = pd.read_parquet(path)
    _price_db["Date"] = pd.to_datetime(_price_db["Date"])
    return _price_db


def refresh_cache() -> None:
    """Reload price database from disk."""
    global _price_db
    _price_db = None
    _load()
    print(f"[prices] Cache refreshed — {len(_price_db)} rows.")


# ── Validation ────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime.date:
    try:
        dt = datetime.date.fromisoformat(date_str)
    except ValueError:
        raise ValueError(f"Bad date format: '{date_str}'. Expected 'YYYY-MM-DD'.")
    if dt < DATE_FLOOR:
        raise DateFloorError(
            f"Date {date_str} is before the floor {DATE_FLOOR}."
        )
    return dt


# ── Public API ────────────────────────────────────────────────────────────────

def get_ohlcv(
    symbol:    str,
    from_date: str,
    to_date:   str,
    adjusted:  bool = True,
    fields:    Optional[list] = None,
) -> pd.DataFrame:
    """
    OHLCV time series for a single stock.

    Args:
        symbol    : NSE symbol e.g. 'HDFCBANK'
        from_date : 'YYYY-MM-DD' start (inclusive)
        to_date   : 'YYYY-MM-DD' end (inclusive)
        adjusted  : use backward-adjusted prices (default True)
                    Adjusted prices account for splits and bonuses.
                    Use adjusted=False for raw NSE bhavcopy prices.
        fields    : subset of columns to return
                    default: ['Date','Open','High','Low','Close','Volume']

    Returns:
        DataFrame sorted by Date ascending.
        Empty if symbol not found or no data in range.

    Raises:
        DateFloorError : date before 2010-01-01
        ValueError     : bad date format

    Example:
        prices = get_ohlcv("HDFCBANK", "2023-01-01", "2024-12-31")
        print(prices.tail())
    """
    _parse_date(from_date)
    _parse_date(to_date)

    db  = _load()
    col = "AdjClose" if (adjusted and "AdjClose" in db.columns) else "Close"

    mask = (
        (db["Symbol"]   == symbol.upper()) &
        (db["Date"]     >= pd.Timestamp(from_date)) &
        (db["Date"]     <= pd.Timestamp(to_date))
    )
    df = db[mask].copy().sort_values("Date").reset_index(drop=True)

    if df.empty:
        return pd.DataFrame()

    default_fields = ["Date", "Open", "High", "Low", "Close", "Volume"]
    if adjusted and "AdjClose" in df.columns:
        default_fields = ["Date", "Open", "High", "Low", "AdjClose", "Volume"]
        df = df.rename(columns={"AdjClose": "Close"})

    keep = fields or default_fields
    keep = [c for c in keep if c in df.columns]
    return df[keep]


def get_price(
    symbol:   str,
    date_str: str,
    field:    str = "Close",
    adjusted: bool = True,
) -> Optional[float]:
    """
    Get a single price for a symbol on a specific date.

    If exact date is not a trading day, returns the most recent
    prior trading day's price.

    Args:
        symbol   : NSE symbol
        date_str : 'YYYY-MM-DD'
        field    : price field — 'Close', 'Open', 'High', 'Low' (default 'Close')
        adjusted : use adjusted prices (default True)

    Returns:
        Float price, or None if no data found.

    Example:
        price = get_price("HDFCBANK", "2024-01-15")
    """
    _parse_date(date_str)

    db = _load()
    col = "AdjClose" if (adjusted and "AdjClose" in db.columns and field == "Close") else field

    mask = (
        (db["Symbol"] == symbol.upper()) &
        (db["Date"]   <= pd.Timestamp(date_str))
    )
    df = db[mask]
    if df.empty or col not in df.columns:
        return None

    return float(df.iloc[-1][col])


def get_panel(
    symbols:   list,
    from_date: str,
    to_date:   str,
    field:     str = "Close",
    adjusted:  bool = True,
) -> pd.DataFrame:
    """
    Wide price panel — dates as index, symbols as columns.

    Useful for computing cross-sectional returns in a backtest.

    Args:
        symbols   : list of NSE symbols
        from_date : 'YYYY-MM-DD'
        to_date   : 'YYYY-MM-DD'
        field     : 'Close', 'Volume', etc. (default 'Close')
        adjusted  : use adjusted prices (default True)

    Returns:
        Wide DataFrame: index=Date, columns=symbols
        Missing values where symbol didn't trade on that date.

    Example:
        panel = get_panel(["HDFCBANK","TCS","INFY"], "2023-01-01", "2024-12-31")
        returns = panel.pct_change()
    """
    _parse_date(from_date)
    _parse_date(to_date)

    db  = _load()
    col = "AdjClose" if (adjusted and "AdjClose" in db.columns and field == "Close") else field

    mask = (
        db["Symbol"].isin([s.upper() for s in symbols]) &
        (db["Date"] >= pd.Timestamp(from_date)) &
        (db["Date"] <= pd.Timestamp(to_date))
    )
    df = db[mask].copy()
    if df.empty or col not in df.columns:
        return pd.DataFrame()

    wide = df.pivot_table(
        index   = "Date",
        columns = "Symbol",
        values  = col,
        aggfunc = "last",
    ).sort_index()

    if field == "Close":
        wide.columns.name = None
        wide.index.name   = "Date"

    return wide


# ── Warm up on import ─────────────────────────────────────────────────────────

try:
    _load()
except FileNotFoundError:
    pass
