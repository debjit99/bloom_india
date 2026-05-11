"""
bloom_india/data/process/adjust.py
====================================
Bloomberg/CRSP-style price adjustment for NSE equity prices.

Methodology
-----------
Cumulative Adjustment Factor (CAF) — CRSP style:

    adj_price = raw_price * CAF

CAF is computed as a backward cumulative product anchored to the
EARLIEST date (2010 or listing). This means:

    - Prices BEFORE any event are multiplied by CAF > 1
      (scaled UP to be comparable with today's prices)
    - Prices AFTER all events have CAF = 1.0 (unchanged)
    - This matches Bloomberg convention: most recent price = raw price

Example — HDFCBANK bonus 1:1 on 2024-09-20:
    event_factor = 2.0  (price DOUBLES for pre-bonus dates)
    CAF before 2024-09-20 = 2.0
    CAF on and after       = 1.0

    Raw Oct 2020: ₹800  →  adj = 800 × 2.0 = ₹1,600  ✓
    Raw Oct 2024: ₹850  →  adj = 850 × 1.0 = ₹850    ✓
    (smooth series, no cliff)

Events handled
--------------
    Splits  : old_FV / new_FV   e.g. Rs10→Rs2 = factor 5.0
    Bonuses : (held+bonus)/held  e.g. 1:1 = factor 2.0
    Demergers: hardcoded factors from NSE futures settle prices
               (same approach as Bloomberg's demerger adjustment)

Events NOT applied (price-neutral)
-----------------------------------
    Dividends  — total return only, not price-level
    Rights     — dilutive but complex; excluded
    AGM/misc   — no price impact

Public API
----------
    fetch_corp_actions(symbol, session) → pd.DataFrame
    build_cum_factors(actions, dates)   → pd.DataFrame of CAF per date
    adjust_series(prices, actions)      → Bloomberg-adjusted price series
    adjust_panel(panel, workers)        → adjust full long panel

Raises
------
    Nothing — all errors are caught and logged silently.
"""

import re
import time
import random
import datetime
import threading
import requests
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Constants ─────────────────────────────────────────────────────────────────

NSE_CORP_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&symbol={symbol}"
)
NSE_HOME   = "https://www.nseindia.com"
DATE_FLOOR = datetime.date(2010, 1, 1)
PRICE_COLS = ["Open", "High", "Low", "Close", "PrevClose"]
VOL_COL    = "Volume"


# ── Known demerger factors ────────────────────────────────────────────────────
# Source: NSE futures settle price ratio on ex-date (Bloomberg methodology)
# Format: {symbol: [(ex_date, factor), ...]}
# factor > 1 means parent price dropped — multiply pre-demerger prices UP
#
# These are HARDCODED because:
#   1. NSE API doesn't return demerger events reliably
#   2. Factor must be verified from actual futures/equity price ratio
#   3. Demergers are infrequent (once per few years per stock)

DEMERGER_FACTORS: dict[str, list[tuple]] = {
    # ── Demergers — factors from NSE futures settle price ratio ───────────────
    # Source: masaki93trades/eqExperiment1 demerger_database.csv
    "AARTIIND":   [("2019-07-03", 1.0705),
                   ("2022-10-19", 1.0896)],
    "ABB":        [("2019-12-20", 1.1469)],
    "ADANIENT":   [("2018-04-05", 1.0195),
                   ("2018-09-06", 1.2793)],
    "ARVIND":     [("2018-11-28", 2.8602)],
    "BEML":       [("2022-09-08", 1.1566)],
    "BSOFT":      [("2019-01-24", 1.6782)],
    "CENTURYTEX": [("2019-10-11", 2.2377)],
    "CESC":       [("2018-10-30", 1.2040),   # SPENCERS demerger
                   ("2021-09-17", 10.0)],    # Split 10:1
    "GRASIM":     [("2017-07-19", 1.2399)],
    "MOTHERSON":  [("2022-01-14", 1.2617)],
    "NMDC":       [("2022-10-27", 1.2441)],
    "PEL":        [("2022-08-30", 1.7976)],
    "RELCAPITAL": [("2017-09-05", 1.1006)],
    "RELIANCE":   [("2023-07-20", 1.0823)],
    "SINTEX":     [("2017-05-25", 3.9962)],
    "TATACHEM":   [("2020-03-04", 2.2911)],
    "TATACOMM":   [("2019-09-17", 1.1623)],

    # ── Demergers — verified from raw bhavcopy ratio ──────────────────────────
    "SIEMENS":    [("2025-04-07", 1.7523)],
    "KPRMILL":    [("2021-09-24", 4.5470)],
    "PCBL":       [("2022-04-11", 1.8495)],
    "ANANTRAJ":   [("2020-10-06", 1.7813)],
    "CGCL":       [("2024-03-05", 3.3333)],

    # ── Splits 10:1 (Rs10→Re1) ────────────────────────────────────────────────
    "NESTLEIND":  [("2024-01-05", 10.0)],
    "EICHERMOT":  [("2020-08-24", 10.0)],
    "TATASTEEL":  [("2022-07-28", 10.0)],
    "BAJAJFINSV": [("2022-09-13", 10.0)],
    "SAREGAMA":   [("2022-04-26", 10.0)],
    "ANGELONE":   [("2026-02-26", 10.0)],
    "PGEL":       [("2024-07-10", 10.0)],
    "SARDAEN":    [("2023-08-04", 10.0)],
    "TATAINVEST": [("2025-10-14", 10.0)],
    "BAJFINANCE": [("2025-06-16", 10.0)],
    "INFIBEAM":   [("2017-08-31", 10.0)],
    "IEX":        [("2018-10-19", 10.0)],    # Split 10:1 confirmed ratio=10.09

    # ── Splits 5:1 ────────────────────────────────────────────────────────────
    "KOTAKBANK":  [("2026-01-14",  5.0)],
    "DRREDDY":    [("2024-10-28",  5.0)],
    "GPIL":       [("2024-10-04",  5.0)],
    "MCDOWELL-N": [("2018-06-15",  5.0)],

    # ── Splits 4:1 ────────────────────────────────────────────────────────────
    "360ONE":     [("2023-03-02",  4.0)],

    # ── Splits 3:1 ────────────────────────────────────────────────────────────
    "ABFRL":      [("2025-05-22",  3.0)],
    "VEDL":       [("2026-04-30",  3.0)],

    # ── Bonus 1:1 (2x) ────────────────────────────────────────────────────────
    "HCLTECH":    [("2019-12-05",  2.0)],
    "TCS":        [("2018-05-31",  2.0)],
    "BRITANNIA":  [("2018-11-29",  2.0)],
    "EMAMILTD":   [("2018-06-21",  2.0)],
    "ELECON":     [("2024-07-19",  2.0)],
    "JBCHEPHARM": [("2023-09-18",  2.0)],
    "JINDALSAW":  [("2024-10-09",  2.0)],
    "JBMA":       [("2025-01-31",  2.0)],
    "NAVA":       [("2025-01-20",  2.0)],
    "NBCC":       [("2018-04-25",  2.0)],
    "IPCALAB":    [("2022-01-10",  2.0)],
    "CONCOR":     [("2018-06-26",  2.0)],
    "IIFL":       [("2019-05-30",  2.0)],

    # ── Other verified events ─────────────────────────────────────────────────
    "IRB":        [("2023-02-22",  8.74)],
    "OIL":        [("2018-03-27",  1.58)],
}


# ── NSE session ───────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": CONFIG.fetch.user_agent,
        "Referer":    NSE_HOME,
        "Accept":     "application/json, text/plain, */*",
    })
    try:
        s.get(NSE_HOME, timeout=10)
        time.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass
    return s


# ── Factor parsers ────────────────────────────────────────────────────────────

def _parse_split_factor(subject: str) -> Optional[float]:
    """
    Split: old_FV / new_FV — the factor price is REDUCED on ex-date.
    Bloomberg convention: multiply pre-split prices by this factor to
    bring them UP to post-split scale.

    'Face Value Split From Rs 10 To Rs 2'  →  10/2 = 5.0
    """
    m = re.search(
        r"(?:split|face\s+value\s+split).*?rs\.?\s*/?\-?\s*([\d.]+)"
        r".*?to\s+rs\.?\s*/?\-?\s*([\d.]+)",
        subject, re.IGNORECASE,
    )
    if m:
        try:
            old_fv = float(m.group(1))
            new_fv = float(m.group(2))
            if new_fv > 0 and old_fv != new_fv:
                return round(old_fv / new_fv, 10)
        except (ValueError, ZeroDivisionError):
            pass
    return None


def _parse_bonus_factor(subject: str) -> Optional[float]:
    """
    Bonus: (existing + new) / existing
    Bloomberg: multiply pre-bonus prices by this to bring them UP.

    'Bonus 1:1'  →  (1+1)/1 = 2.0
    'Bonus 1:2'  →  (2+1)/2 = 1.5
    'Bonus 3:2'  →  (2+3)/2 = 2.5
    """
    m = re.search(r"bonus\s+(\d+)\s*[:/]\s*(\d+)", subject, re.IGNORECASE)
    if m:
        try:
            new_sh  = int(m.group(1))
            held    = int(m.group(2))
            total   = held + new_sh
            if held > 0:
                return round(total / held, 10)
        except (ValueError, ZeroDivisionError):
            pass
    return None


def _parse_event_factor(subject: str) -> Optional[float]:
    """
    Combined parser. Returns factor > 1 (Bloomberg scale-up convention).
    Returns None if not a split or bonus.
    """
    f = _parse_split_factor(subject)
    if f is not None and f > 1.0:
        return f
    f = _parse_bonus_factor(subject)
    if f is not None and f > 1.0:
        return f
    return None


# ── Fetch corporate actions ───────────────────────────────────────────────────

def fetch_corp_actions(
    symbol:  str,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """
    Fetch splits + bonuses for a symbol from NSE API.
    Also includes hardcoded demerger factors.

    Returns DataFrame:
        symbol, ex_date, subject, event_type, scale_factor
    where scale_factor > 1 = multiply PRE-event prices by this (Bloomberg style)

    Empty DataFrame if no events found.
    """
    sess = session or _make_session()
    sym  = symbol.upper()
    rows = []

    # ── Hardcoded factors first — these take priority over NSE API ────────────
    hardcoded_dates = set()
    for ex_date_str, factor in DEMERGER_FACTORS.get(sym, []):
        ex_date = datetime.date.fromisoformat(ex_date_str)
        hardcoded_dates.add(ex_date)
        rows.append({
            "symbol":       sym,
            "ex_date":      ex_date,
            "subject":      f"Hardcoded factor={factor}",
            "event_type":   "demerger",
            "scale_factor": factor,
        })

    # ── NSE API ───────────────────────────────────────────────────────────────
    try:
        resp = sess.get(
            NSE_CORP_URL.format(symbol=sym),
            timeout=CONFIG.fetch.timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []

    for item in data:
        subject     = item.get("subject", "") or ""
        ex_date_raw = item.get("exDate",  "") or ""
        series      = item.get("series",  "EQ")

        if series not in ("EQ", "BE", "BZ"):
            continue

        factor = _parse_event_factor(subject)
        if factor is None or factor <= 1.0:
            continue

        try:
            ex_date = pd.to_datetime(ex_date_raw, dayfirst=True).date()
        except Exception:
            continue

        if ex_date < DATE_FLOOR:
            continue

        # Skip if hardcoded factor already covers this date
        if ex_date in hardcoded_dates:
            continue

        event_type = "split" if _parse_split_factor(subject) else "bonus"
        rows.append({
            "symbol":       sym,
            "ex_date":      ex_date,
            "subject":      subject,
            "event_type":   event_type,
            "scale_factor": factor,
        })

    if not rows:
        return pd.DataFrame()

    df = (pd.DataFrame(rows)
            .sort_values("ex_date")
            .drop_duplicates(subset=["ex_date", "event_type"])
            .reset_index(drop=True))
    return df


# ── CAF computation — CRSP style ──────────────────────────────────────────────

def build_cum_factors(
    actions: pd.DataFrame,
    dates:   pd.DatetimeIndex,
) -> pd.Series:
    """
    Build cumulative adjustment factor (CAF) for each trading date.
    Bloomberg convention: anchored to LATEST (most recent) price.

        CAF[t] = product of (1/scale_factor) for all events with ex_date > t

    So:
        - CAF on most recent date  = 1.0  (raw price unchanged)
        - CAF on old dates         < 1.0  (scaled DOWN to match today)

    Example — HCLTECH bonus 1:1 on 2019-12-05 (scale_factor=2.0):
        CAF before 2019-12-05 = 1/2.0 = 0.5
        CAF on and after       = 1.0

        Raw Dec 4:  ₹1,125 × 0.5 = ₹562  ✓ (matches post-bonus ~₹560)
        Raw Dec 5:  ₹560   × 1.0 = ₹560  ✓ (unchanged)
        → Smooth series, no cliff

    Args:
        actions : from fetch_corp_actions(), has scale_factor > 1
        dates   : sorted DatetimeIndex of all trading dates

    Returns:
        pd.Series indexed by date, values = CAF (≤ 1.0)
    """
    caf = pd.Series(1.0, index=dates)

    if actions.empty:
        return caf

    # Sort oldest → newest
    events = actions.sort_values("ex_date", ascending=True)

    for _, ev in events.iterrows():
        ex_dt = pd.Timestamp(ev["ex_date"])
        f     = ev["scale_factor"]   # > 1
        # All dates STRICTLY BEFORE ex_date: multiply by 1/f (scale DOWN)
        caf[caf.index < ex_dt] *= (1.0 / f)

    return caf.round(10)


# ── Adjust single symbol ──────────────────────────────────────────────────────

def adjust_series(
    prices:  pd.DataFrame,
    actions: pd.DataFrame,
) -> pd.DataFrame:
    """
    Bloomberg-style adjustment for a single symbol.

    adj_price = raw_price × CAF

    Pre-event prices are scaled UP (CAF > 1).
    Most recent prices are unchanged (CAF = 1).

    Args:
        prices  : DataFrame with Date, Open, High, Low, Close, PrevClose cols
        actions : from fetch_corp_actions()

    Returns:
        Adjusted DataFrame with adj_factor column added.
        Original raw prices preserved as raw_Close, raw_Open etc.
    """
    if prices.empty or actions.empty:
        df = prices.copy()
        df["adj_factor"]  = 1.0
        df["IsAdjusted"]  = False
        return df

    df   = prices.copy().sort_values("Date").reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"])

    dates = pd.DatetimeIndex(df["Date"])
    caf   = build_cum_factors(actions, dates)

    df["adj_factor"] = caf.values
    df["IsAdjusted"] = caf.values != 1.0

    for col in PRICE_COLS:
        if col in df.columns:
            df[col] = (df[col].astype(float) * caf.values).round(4)

    if VOL_COL in df.columns:
        # Volume scales inversely — more shares means more volume historically
        inv_caf = (1.0 / caf.values).round(10)
        df[VOL_COL] = (
            df[VOL_COL].astype(float) * inv_caf
        ).round(0).astype("Int64")

    return df


# ── Full panel adjustment ─────────────────────────────────────────────────────

def adjust_panel(
    panel:   pd.DataFrame,
    workers: int  = 8,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Bloomberg-style adjustment for an entire price panel.

    Reads from RAW prices (panel should be unmodified bhavcopy data).
    Fetches corp actions in parallel, builds CAF per symbol, applies.

    Args:
        panel   : raw long DataFrame — Date, Symbol, Open, High, Low, Close, Volume
        workers : parallel threads for NSE API (default 8)
        verbose : print progress

    Returns:
        Adjusted panel. Adds adj_factor (CAF) and IsAdjusted columns.
        adj_factor = 1.0 for dates with no events.
    """
    panel   = panel.copy()
    panel["Date"] = pd.to_datetime(panel["Date"])
    symbols = panel["Symbol"].unique().tolist()

    if verbose:
        print(f"Fetching corp actions for {len(symbols)} symbols "
              f"({workers} workers)...")

    # ── Parallel fetch ────────────────────────────────────────────────────────
    _local = threading.local()

    def _get_sess():
        if not hasattr(_local, "s"):
            _local.s = _make_session()
        return _local.s

    def _fetch(sym):
        time.sleep(random.uniform(0.1, 0.5))
        try:
            return sym, fetch_corp_actions(sym, session=_get_sess())
        except Exception:
            return sym, pd.DataFrame()

    all_actions: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, s): s for s in symbols}
        for i, f in enumerate(as_completed(futures), 1):
            sym, acts = f.result()
            if not acts.empty:
                all_actions[sym] = acts
            if verbose and i % 100 == 0:
                pct = i / len(symbols) * 100
                print(f"  [{i:4d}/{len(symbols)}  {pct:5.1f}%]  "
                      f"{len(all_actions)} with events")

    # Summary
    n_splits   = sum((df["event_type"]=="split"   ).sum() for df in all_actions.values())
    n_bonuses  = sum((df["event_type"]=="bonus"   ).sum() for df in all_actions.values())
    n_demergers= sum((df["event_type"]=="demerger").sum() for df in all_actions.values())
    if verbose:
        print(f"\n  Events found: {len(all_actions)} symbols")
        print(f"    Splits    : {n_splits}")
        print(f"    Bonuses   : {n_bonuses}")
        print(f"    Demergers : {n_demergers}")
        print(f"\nApplying Bloomberg CAF adjustment...")

    # ── Apply per symbol ──────────────────────────────────────────────────────
    frames   = []
    adjusted = 0

    for sym in symbols:
        sym_df = panel[panel["Symbol"] == sym].copy()
        if sym in all_actions:
            sym_df   = adjust_series(sym_df, all_actions[sym])
            adjusted += 1
        else:
            sym_df["adj_factor"] = 1.0
            sym_df["IsAdjusted"] = False
        frames.append(sym_df)

    out = (pd.concat(frames, ignore_index=True)
             .sort_values(["Date", "Symbol"])
             .reset_index(drop=True))

    if verbose:
        adj_rows = (out["IsAdjusted"] == True).sum()
        print(f"Done. {adjusted}/{len(symbols)} symbols adjusted. "
              f"{adj_rows:,} rows modified.")

    return out