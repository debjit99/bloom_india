"""
bloom_india/data/process/adjust.py
====================================
Corporate action price adjustment for NSE equity prices.

Implements backward adjustment — all historical prices are scaled
to be comparable with the most recent prices (post all events).

Core concept:
    For a bonus 1:1 on 2024-10-28:
        Prices BEFORE 2024-10-28 are multiplied by 0.5
        Prices ON OR AFTER 2024-10-28 are unchanged
    This makes the entire series comparable on today's price scale.

For multiple events (e.g. split in 2017, bonus in 2024):
    Prices before 2017: multiplied by split_factor × bonus_factor
    Prices between 2017-2024: multiplied by bonus_factor only
    Prices after 2024: unchanged

Public API
----------
    fetch_corp_actions(symbol, session)   → pd.DataFrame of parsed events
    adjust_series(prices_df, actions_df) → backward-adjusted prices
    adjust_panel(panel)                   → adjust full long panel, all symbols
"""

import re
import time
import random
import datetime
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Constants ─────────────────────────────────────────────────────────────────

NSE_CORP_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol={symbol}"
NSE_HOME     = "https://www.nseindia.com"
DATE_FLOOR   = datetime.date(2010, 1, 1)
PRICE_COLS   = ["Open","High","Low","Close","PrevClose"]
VOLUME_COL   = "Volume"


# ── Session ───────────────────────────────────────────────────────────────────

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


# ── Factor parser — same logic as cryptobot adjustments.py ───────────────────

def _parse_factor(subject: str) -> Optional[float]:
    """
    Parse NSE corporate action subject string → price adjustment factor.
    Factor < 1 means price goes down (more shares).

    Bonus 1:1  → factor = 1/(1+1) = 0.5
    Bonus 1:2  → factor = 2/(2+1) = 0.667
    Split Rs10 → Rs2 → factor = 2/10 = 0.2
    Dividend   → None (not applied)
    """
    p = subject.upper().strip()

    # Bonus: BONUS N:M — N bonus shares per M held
    m = re.search(r"BONUS\s+(\d+)\s*[:/]\s*(\d+)", p)
    if m:
        try:
            bonus = int(m.group(1))
            held  = int(m.group(2))
            total = held + bonus
            if total > 0:
                return round(held / total, 10)
        except (ValueError, ZeroDivisionError):
            pass

    # Split: face value change Rs OLD to Rs NEW
    m = re.search(
        r"(?:SPLIT|FACE\s+VALUE\s+SPLIT).*?RS\.?\s*/?\s*([\d.]+).*?TO\s+RS\.?\s*/?\s*([\d.]+)",
        p,
    )
    if m:
        try:
            old_fv = float(m.group(1))
            new_fv = float(m.group(2))
            if old_fv > 0:
                return round(new_fv / old_fv, 10)
        except (ValueError, ZeroDivisionError):
            pass

    return None  # dividend, AGM, rights — not applied


# ── Fetch corporate actions ───────────────────────────────────────────────────

def fetch_corp_actions(
    symbol:  str,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """
    Fetch all corporate actions for a symbol from NSE API.

    Returns DataFrame columns:
        symbol, ex_date, subject, event_factor, scale_up_factor
    where:
        event_factor   < 1 — multiply PRE-event prices by this (backward)
        scale_up_factor > 1 — = 1/event_factor (for forward adjustment)

    Empty DataFrame if no splits/bonuses found.
    """
    sess = session or _make_session()

    try:
        resp = sess.get(
            NSE_CORP_URL.format(symbol=symbol.upper()),
            timeout=CONFIG.fetch.timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
        if not isinstance(data, list):
            return pd.DataFrame()
    except Exception as e:
        print(f"  [adjust] {symbol}: {e}")
        return pd.DataFrame()

    rows = []
    for item in data:
        subject     = item.get("subject","") or ""
        ex_date_raw = item.get("exDate","")  or ""
        series      = item.get("series","EQ")

        if series not in ("EQ","BE","BZ"):
            continue

        factor = _parse_factor(subject)
        if factor is None or factor <= 0 or factor == 1.0:
            continue

        try:
            ex_date = pd.to_datetime(ex_date_raw, dayfirst=True).date()
        except Exception:
            continue

        if ex_date < DATE_FLOOR:
            continue

        rows.append({
            "symbol":          symbol.upper(),
            "ex_date":         ex_date,
            "subject":         subject,
            "event_factor":    factor,
            "scale_up_factor": round(1.0 / factor, 10),
        })

    if not rows:
        return pd.DataFrame()

    return (pd.DataFrame(rows)
              .sort_values("ex_date")
              .drop_duplicates(subset=["ex_date","subject"])
              .reset_index(drop=True))


# ── Backward adjustment — single symbol ───────────────────────────────────────

def adjust_series(
    prices:  pd.DataFrame,
    actions: pd.DataFrame,
) -> pd.DataFrame:
    """
    Apply backward adjustment to a single symbol's price series.

    Backward = prices BEFORE each event are scaled DOWN by event_factor.
    This anchors the series to the current (most recent) price scale.

    Example — HDFCBANK bonus 1:1 on 2025-08-26:
        event_factor = 0.5
        All prices before 2025-08-26 are × 0.5
        Prices on and after are unchanged

    Args:
        prices  : DataFrame with Date, Close, Open, High, Low columns
        actions : from fetch_corp_actions()

    Returns:
        Adjusted DataFrame. Prices before each event scaled down.
    """
    if prices.empty or actions.empty:
        return prices.copy()

    df  = prices.copy().sort_values("Date").reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"])
    df["IsAdjusted"] = False

    # Process events oldest → newest
    # Each event scales prices BEFORE its ex_date
    for _, event in actions.sort_values("ex_date").iterrows():
        ex_date = pd.Timestamp(event["ex_date"])
        factor  = event["event_factor"]    # < 1
        vol_f   = event["scale_up_factor"] # > 1 — volume goes up inversely

        mask = df["Date"] < ex_date
        if not mask.any():
            continue

        for col in PRICE_COLS:
            if col in df.columns:
                df.loc[mask, col] = (df.loc[mask, col].astype(float) * factor).round(4)

        if VOLUME_COL in df.columns:
            df.loc[mask, VOLUME_COL] = (
                df.loc[mask, VOLUME_COL].astype(float) * vol_f
            ).round(0).astype("Int64")

        df.loc[mask, "IsAdjusted"] = True

    return df


# ── Full panel adjustment ─────────────────────────────────────────────────────

def adjust_panel(
    panel:   pd.DataFrame,
    workers: int = 8,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Backward-adjust an entire price panel for all symbols.

    Fetches corporate actions for every symbol in parallel,
    then applies backward adjustment per symbol.

    Args:
        panel   : long DataFrame with Date, Symbol, Close, Open, High, Low
        workers : parallel threads for fetching (default 8)
        verbose : print progress

    Returns:
        Adjusted panel with IsAdjusted column added.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    panel = panel.copy()
    panel["Date"] = pd.to_datetime(panel["Date"])

    symbols = panel["Symbol"].unique().tolist()
    if verbose:
        print(f"Fetching corporate actions for {len(symbols)} symbols...")

    # Parallel fetch
    _local = threading.local()

    def _get_sess():
        if not hasattr(_local, "s"):
            _local.s = _make_session()
        return _local.s

    def _fetch(sym):
        time.sleep(random.uniform(0.1, 0.5))
        try:
            return sym, fetch_corp_actions(sym, session=_get_sess())
        except Exception as e:
            return sym, pd.DataFrame()

    all_actions = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, s): s for s in symbols}
        for i, f in enumerate(as_completed(futures), 1):
            sym, df_act = f.result()
            if not df_act.empty:
                all_actions[sym] = df_act
            if verbose and i % 100 == 0:
                print(f"  [{i}/{len(symbols)}] {len(all_actions)} with actions")

    n_splits  = sum((df["subject"].str.upper().str.contains("SPLIT")).sum()
                    for df in all_actions.values())
    n_bonuses = sum((df["subject"].str.upper().str.contains("BONUS")).sum()
                    for df in all_actions.values())
    if verbose:
        print(f"  Symbols with actions: {len(all_actions)}")
        print(f"  Splits: {n_splits}  Bonuses: {n_bonuses}")
        print(f"\nApplying adjustments...")

    # Apply per symbol
    frames    = []
    adjusted  = 0
    for i, sym in enumerate(symbols, 1):
        sym_df = panel[panel["Symbol"] == sym].copy()
        if sym in all_actions:
            sym_df   = adjust_series(sym_df, all_actions[sym])
            adjusted += 1
        frames.append(sym_df)
        if verbose and i % 100 == 0:
            print(f"  [{i}/{len(symbols)}] adjusted")

    out = pd.concat(frames, ignore_index=True).sort_values(["Date","Symbol"])
    if verbose:
        print(f"Done. {adjusted}/{len(symbols)} symbols adjusted.")
    return out.reset_index(drop=True)