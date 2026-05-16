"""
bloom_india/mf/db/nav_db.py
============================
SQLite-backed NAV database for mutual funds.

Design principles
-----------------
- NEVER overwrite rows that already exist (INSERT OR IGNORE).
- Schema is append-only: existing data is immutable.
- A cron job calls update_new_navs() to fetch and store only new dates.
- All reads return pandas DataFrames.
- Thread-safe via connection-per-call pattern.
- Graceful degradation: every public function has try/except — app never crashes.

Tables
------
  schemes (scheme_code PK, scheme_name, fund_house, scheme_type,
           scheme_category, category_clean, inserted_at)

  navs    (scheme_code, nav_date, nav  ← PRIMARY KEY (scheme_code, nav_date))
           INSERT OR IGNORE — existing rows are NEVER touched.

DB location
-----------
  ~/bloom_india_data/mf_nav.db   (or wherever config points)
  Override: set MF_NAV_DB env var to an absolute path.
"""

from __future__ import annotations

import os
import math
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime, date
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# ── DB path ───────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    env = os.environ.get("MF_NAV_DB")
    if env:
        return Path(env).expanduser()
    try:
        from bloom_india.config import CONFIG
        base = Path(CONFIG.storage.base_dir).expanduser()
    except Exception:
        base = Path.home() / "bloom_india_data"
    base.mkdir(parents=True, exist_ok=True)
    return base / "mf_nav.db"


# ── Connection factory ────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    path = _db_path()
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads + writes
    conn.execute("PRAGMA synchronous=NORMAL") # safe but faster than FULL
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schemes (
    scheme_code     INTEGER PRIMARY KEY,
    scheme_name     TEXT    NOT NULL,
    fund_house      TEXT,
    scheme_type     TEXT,
    scheme_category TEXT,
    category_clean  TEXT,
    inserted_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS navs (
    scheme_code INTEGER NOT NULL,
    nav_date    TEXT    NOT NULL,   -- ISO format YYYY-MM-DD
    nav         REAL    NOT NULL,
    PRIMARY KEY (scheme_code, nav_date),
    FOREIGN KEY (scheme_code) REFERENCES schemes(scheme_code)
);

CREATE INDEX IF NOT EXISTS idx_navs_date ON navs(nav_date);
CREATE INDEX IF NOT EXISTS idx_navs_scheme ON navs(scheme_code);
"""

_schema_lock = threading.Lock()
_schema_created = False


def ensure_schema() -> None:
    """Create tables if they don't exist. Idempotent."""
    global _schema_created
    with _schema_lock:
        if _schema_created:
            return
        try:
            conn = _connect()
            conn.executescript(_SCHEMA)
            conn.commit()
            conn.close()
            _schema_created = True
            log.info(f"[nav_db] Schema ready at {_db_path()}")
        except Exception as e:
            log.error(f"[nav_db] Schema creation failed: {e}")
            raise


# ── Write operations ──────────────────────────────────────────────────────────

def upsert_scheme(
    scheme_code    : int,
    scheme_name    : str,
    fund_house     : str  = "",
    scheme_type    : str  = "",
    scheme_category: str  = "",
    category_clean : str  = "",
) -> None:
    """
    Insert scheme metadata. Does NOT update if already present.
    (scheme_code is stable — no need to overwrite)
    """
    ensure_schema()
    try:
        conn = _connect()
        conn.execute(
            """INSERT OR IGNORE INTO schemes
               (scheme_code, scheme_name, fund_house, scheme_type,
                scheme_category, category_clean)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (scheme_code, scheme_name, fund_house,
             scheme_type, scheme_category, category_clean),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[nav_db] upsert_scheme({scheme_code}): {e}")


def insert_navs(scheme_code: int, nav_df: pd.DataFrame) -> int:
    """
    Bulk-insert NAV rows. Existing (scheme_code, nav_date) pairs are IGNORED.

    Args:
        scheme_code : integer scheme code
        nav_df      : DataFrame with columns [date, nav]

    Returns:
        Number of NEW rows inserted (0 if all already existed).
    """
    ensure_schema()
    if nav_df is None or nav_df.empty:
        return 0

    rows = []
    for _, row in nav_df.iterrows():
        try:
            nav_val = float(row["nav"])
            if not math.isfinite(nav_val) or nav_val <= 0:
                continue
            d = row["date"]
            if isinstance(d, (datetime, date)):
                date_str = d.strftime("%Y-%m-%d")
            else:
                date_str = str(d)[:10]
            rows.append((scheme_code, date_str, round(nav_val, 6)))
        except Exception:
            continue

    if not rows:
        return 0

    try:
        conn = _connect()
        cur  = conn.cursor()

        # Count before
        before = cur.execute(
            "SELECT COUNT(*) FROM navs WHERE scheme_code = ?", (scheme_code,)
        ).fetchone()[0]

        cur.executemany(
            "INSERT OR IGNORE INTO navs (scheme_code, nav_date, nav) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()

        after   = cur.execute(
            "SELECT COUNT(*) FROM navs WHERE scheme_code = ?", (scheme_code,)
        ).fetchone()[0]
        conn.close()

        inserted = after - before
        if inserted > 0:
            log.debug(f"[nav_db] {scheme_code}: +{inserted} new rows (of {len(rows)} offered)")
        return inserted

    except Exception as e:
        log.error(f"[nav_db] insert_navs({scheme_code}): {e}")
        return 0


# ── Read operations ───────────────────────────────────────────────────────────

def get_nav_history(
    scheme_code: int,
    from_date  : Optional[str] = None,
    to_date    : Optional[str] = None,
) -> pd.DataFrame:
    """
    Retrieve NAV history from DB for a scheme.

    Args:
        scheme_code : integer scheme code
        from_date   : 'YYYY-MM-DD' optional start (inclusive)
        to_date     : 'YYYY-MM-DD' optional end (inclusive)

    Returns:
        DataFrame [date (datetime64), nav (float)], sorted ascending.
        Empty DataFrame if not found — never raises.
    """
    ensure_schema()
    try:
        conn   = _connect()
        params = [scheme_code]
        where  = "WHERE scheme_code = ?"

        if from_date:
            where += " AND nav_date >= ?"
            params.append(from_date)
        if to_date:
            where += " AND nav_date <= ?"
            params.append(to_date)

        query = f"SELECT nav_date, nav FROM navs {where} ORDER BY nav_date ASC"
        df    = pd.read_sql_query(query, conn, params=params)
        conn.close()

        if df.empty:
            return pd.DataFrame(columns=["date", "nav"])

        df = df.rename(columns={"nav_date": "date"})
        df["date"] = pd.to_datetime(df["date"])
        df["nav"]  = df["nav"].astype(float)
        return df

    except Exception as e:
        log.error(f"[nav_db] get_nav_history({scheme_code}): {e}")
        return pd.DataFrame(columns=["date", "nav"])


def get_latest_nav_date(scheme_code: int) -> Optional[str]:
    """
    Return the most recent nav_date stored for a scheme ('YYYY-MM-DD'), or None.
    Used by the cron job to decide what to fetch.
    """
    ensure_schema()
    try:
        conn = _connect()
        row  = conn.execute(
            "SELECT MAX(nav_date) FROM navs WHERE scheme_code = ?", (scheme_code,)
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception as e:
        log.error(f"[nav_db] get_latest_nav_date({scheme_code}): {e}")
        return None


def scheme_exists(scheme_code: int) -> bool:
    """Return True if this scheme has any NAV rows in the DB."""
    ensure_schema()
    try:
        conn = _connect()
        row  = conn.execute(
            "SELECT 1 FROM navs WHERE scheme_code = ? LIMIT 1", (scheme_code,)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def list_schemes_in_db() -> pd.DataFrame:
    """Return all schemes that have at least one NAV row."""
    ensure_schema()
    try:
        conn = _connect()
        df   = pd.read_sql_query(
            """SELECT s.scheme_code, s.scheme_name, s.fund_house,
                      s.category_clean, COUNT(n.nav_date) AS nav_rows,
                      MIN(n.nav_date) AS earliest_date,
                      MAX(n.nav_date) AS latest_date
               FROM schemes s
               JOIN navs n ON s.scheme_code = n.scheme_code
               GROUP BY s.scheme_code
               ORDER BY s.scheme_name""",
            conn,
        )
        conn.close()
        return df
    except Exception as e:
        log.error(f"[nav_db] list_schemes_in_db: {e}")
        return pd.DataFrame()


def db_stats() -> dict:
    """Return quick stats about the database."""
    ensure_schema()
    try:
        conn        = _connect()
        schemes     = conn.execute("SELECT COUNT(*) FROM schemes").fetchone()[0]
        navs        = conn.execute("SELECT COUNT(*) FROM navs").fetchone()[0]
        earliest    = conn.execute("SELECT MIN(nav_date) FROM navs").fetchone()[0]
        latest      = conn.execute("SELECT MAX(nav_date) FROM navs").fetchone()[0]
        conn.close()
        size_mb = round(_db_path().stat().st_size / 1_048_576, 2) if _db_path().exists() else 0
        return {
            "db_path"     : str(_db_path()),
            "schemes"     : schemes,
            "nav_rows"    : navs,
            "earliest_nav": earliest,
            "latest_nav"  : latest,
            "size_mb"     : size_mb,
        }
    except Exception as e:
        log.error(f"[nav_db] db_stats: {e}")
        return {"error": str(e)}
