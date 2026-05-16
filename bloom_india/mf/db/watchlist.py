"""
bloom_india/mf/db/watchlist.py
================================
Persistent watchlist (favourites) stored in the same SQLite DB.

Table: watchlist
  scheme_code  INTEGER PK
  scheme_name  TEXT
  fund_house   TEXT
  added_at     TEXT (ISO datetime)
  notes        TEXT (optional user note)

All functions are safe — never raise, always return something.
Survives restarts: data is in the DB, not in Streamlit session state.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Optional

import pandas as pd

from bloom_india.mf.db.nav_db import _connect, ensure_schema

log = logging.getLogger(__name__)

_WATCHLIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    scheme_code  INTEGER PRIMARY KEY,
    scheme_name  TEXT    NOT NULL,
    fund_house   TEXT    NOT NULL DEFAULT '',
    category     TEXT    NOT NULL DEFAULT '',
    added_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    notes        TEXT    NOT NULL DEFAULT ''
);
"""


def _ensure_watchlist_table() -> None:
    try:
        ensure_schema()   # ensure main schema exists first
        conn = _connect()
        conn.executescript(_WATCHLIST_SCHEMA)
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[watchlist] schema error: {e}")


def add_fund(scheme_code: int, scheme_name: str,
             fund_house: str = "", category: str = "",
             notes: str = "") -> bool:
    """
    Add a fund to the watchlist.
    Returns True if added, False if already present or error.
    """
    _ensure_watchlist_table()
    try:
        conn = _connect()
        cur  = conn.execute(
            """INSERT OR IGNORE INTO watchlist
               (scheme_code, scheme_name, fund_house, category, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (scheme_code, scheme_name, fund_house, category, notes),
        )
        added = cur.rowcount > 0
        conn.commit()
        conn.close()
        return added
    except Exception as e:
        log.error(f"[watchlist] add_fund({scheme_code}): {e}")
        return False


def remove_fund(scheme_code: int) -> bool:
    """Remove a fund from the watchlist. Returns True if removed."""
    _ensure_watchlist_table()
    try:
        conn = _connect()
        cur  = conn.execute(
            "DELETE FROM watchlist WHERE scheme_code = ?", (scheme_code,)
        )
        removed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return removed
    except Exception as e:
        log.error(f"[watchlist] remove_fund({scheme_code}): {e}")
        return False


def update_notes(scheme_code: int, notes: str) -> bool:
    """Update the notes field for a watchlist entry."""
    _ensure_watchlist_table()
    try:
        conn = _connect()
        conn.execute(
            "UPDATE watchlist SET notes = ? WHERE scheme_code = ?",
            (notes, scheme_code),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error(f"[watchlist] update_notes({scheme_code}): {e}")
        return False


def get_watchlist() -> pd.DataFrame:
    """
    Return all watchlist entries as a DataFrame.
    Columns: scheme_code, scheme_name, fund_house, category, added_at, notes
    Empty DataFrame if nothing saved yet.
    """
    _ensure_watchlist_table()
    try:
        conn = _connect()
        df   = pd.read_sql_query(
            "SELECT * FROM watchlist ORDER BY added_at DESC", conn
        )
        conn.close()
        return df
    except Exception as e:
        log.error(f"[watchlist] get_watchlist: {e}")
        return pd.DataFrame(columns=["scheme_code","scheme_name","fund_house",
                                     "category","added_at","notes"])


def is_in_watchlist(scheme_code: int) -> bool:
    """Return True if scheme_code is in the watchlist."""
    _ensure_watchlist_table()
    try:
        conn = _connect()
        row  = conn.execute(
            "SELECT 1 FROM watchlist WHERE scheme_code = ? LIMIT 1",
            (scheme_code,),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def watchlist_codes() -> list[int]:
    """Return list of scheme_codes currently in the watchlist."""
    df = get_watchlist()
    return df["scheme_code"].tolist() if not df.empty else []


def clear_watchlist() -> int:
    """Remove all entries. Returns count deleted."""
    _ensure_watchlist_table()
    try:
        conn = _connect()
        n    = conn.execute("DELETE FROM watchlist").rowcount
        conn.commit()
        conn.close()
        return n
    except Exception as e:
        log.error(f"[watchlist] clear: {e}")
        return 0