"""
bloom_india/data/process/adjust.py
====================================
Applies corporate action adjustments to raw NSE price data.

Supported actions:
    splits  : e.g. 2:1 split → multiply all prior prices by 0.5
    bonuses : e.g. 1:1 bonus → multiply all prior prices by 0.5
    dividends: subtract dividend amount from all prior prices (optional)

Raw prices from NSE bhavcopy are unadjusted.
Use this module to get backward-adjusted prices for charting
and return calculations.

Public API
----------
    fetch_corp_actions(symbol)         → pd.DataFrame of actions
    adjust_prices(prices_df, actions)  → adjusted prices DataFrame
    build_adjusted_panel(panel, actions_db) → full panel adjusted
"""

import time
import random
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Fetch corporate actions ───────────────────────────────────────────────────

NSE_CORP_ACTIONS_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&symbol={symbol}"
)
NSE_HOMEPAGE = "https://www.nseindia.com"


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": CONFIG.fetch.user_agent,
        "Referer":    NSE_HOMEPAGE,
        "Accept":     "application/json",
    })
    try:
        s.get(NSE_HOMEPAGE, timeout=10)
        time.sleep(random.uniform(0.3, 0.7))
    except Exception:
        pass
    return s


def fetch_corp_actions(
    symbol:  str,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """
    Fetch corporate actions for a symbol from NSE.

    Returns DataFrame with columns:
        symbol, ex_date, action_type, ratio, factor
        where factor is the price adjustment multiplier.

    action_type: 'split' | 'bonus' | 'dividend'
    factor:
        split/bonus : new_shares / (new_shares + old_shares) or ratio-based
        dividend    : amount in Rs
    """
    sess = session or _make_session()

    try:
        url  = NSE_CORP_ACTIONS_URL.format(symbol=symbol.upper())
        resp = sess.get(url, timeout=CONFIG.fetch.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
    except Exception as e:
        print(f"  [corp_actions] {symbol}: {e}")
        return pd.DataFrame()

    rows = []
    for item in data:
        ex_date_str = item.get("exDate", "")
        subject     = item.get("subject", "").lower()
        series      = item.get("series", "EQ")

        if series != "EQ":
            continue

        try:
            ex_date = pd.to_datetime(ex_date_str, dayfirst=True)
        except Exception:
            continue

        # ── Split ─────────────────────────────────────────────────────────────
        if "split" in subject or "sub-division" in subject:
            # e.g. "Face Value split from Rs 10/- to Rs 2/-" → factor = 0.2
            import re
            nums = re.findall(r"[\d\.]+", subject)
            if len(nums) >= 2:
                try:
                    old_fv = float(nums[0])
                    new_fv = float(nums[1])
                    factor = new_fv / old_fv  # < 1 means prices go down
                except Exception:
                    factor = 0.5   # assume 2:1 split as fallback
            else:
                factor = 0.5
            rows.append({
                "symbol":      symbol.upper(),
                "ex_date":     ex_date,
                "action_type": "split",
                "subject":     item.get("subject", ""),
                "factor":      factor,
            })

        # ── Bonus ─────────────────────────────────────────────────────────────
        elif "bonus" in subject:
            import re
            # e.g. "Bonus 1:1" → factor = 0.5 (prices halve)
            nums = re.findall(r"(\d+)\s*:\s*(\d+)", subject)
            if nums:
                try:
                    new_sh, old_sh = int(nums[0][0]), int(nums[0][1])
                    factor = old_sh / (old_sh + new_sh)
                except Exception:
                    factor = 0.5
            else:
                factor = 0.5
            rows.append({
                "symbol":      symbol.upper(),
                "ex_date":     ex_date,
                "action_type": "bonus",
                "subject":     item.get("subject", ""),
                "factor":      factor,
            })

        # ── Dividend ──────────────────────────────────────────────────────────
        elif "dividend" in subject and "interim" not in subject:
            import re
            nums = re.findall(r"[\d\.]+", subject)
            amount = float(nums[0]) if nums else 0.0
            rows.append({
                "symbol":      symbol.upper(),
                "ex_date":     ex_date,
                "action_type": "dividend",
                "subject":     item.get("subject", ""),
                "factor":      amount,   # Rs per share
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("ex_date").reset_index(drop=True)
    return df


# ── Apply adjustments ─────────────────────────────────────────────────────────

def adjust_prices(
    prices:   pd.DataFrame,
    actions:  pd.DataFrame,
    adjust_dividends: bool = False,
) -> pd.DataFrame:
    """
    Apply backward adjustment to a price series.

    Backward adjustment: multiplies all prices BEFORE each ex_date
    by the cumulative adjustment factor, keeping the latest price
    as the anchor (most recent prices unchanged).

    Args:
        prices          : DataFrame with columns Date, Close (and Open, High, Low)
                          sorted by Date ascending.
        actions         : DataFrame from fetch_corp_actions() for one symbol.
        adjust_dividends: whether to subtract dividend amounts (default False)

    Returns:
        Adjusted prices DataFrame with same schema.
        Adds column 'adj_factor' showing cumulative adjustment applied.
    """
    if prices.empty or actions.empty:
        prices = prices.copy()
        prices["adj_factor"] = 1.0
        return prices

    df      = prices.copy().sort_values("Date").reset_index(drop=True)
    df["adj_factor"] = 1.0

    price_cols = [c for c in ["Open","High","Low","Close","PrevClose"] if c in df.columns]

    # Process actions from most recent to oldest (backward adjustment)
    act = actions.sort_values("ex_date", ascending=False)

    for _, action in act.iterrows():
        ex_date     = pd.Timestamp(action["ex_date"])
        action_type = action["action_type"]
        factor      = action["factor"]

        mask = df["Date"] < ex_date

        if action_type in ("split", "bonus"):
            df.loc[mask, price_cols]  = df.loc[mask, price_cols] * factor
            df.loc[mask, "adj_factor"] *= factor

        elif action_type == "dividend" and adjust_dividends:
            df.loc[mask, price_cols]  = df.loc[mask, price_cols] - factor
            # Keep prices positive
            for col in price_cols:
                df[col] = df[col].clip(lower=0.01)

    return df


# ── Build adjusted panel ──────────────────────────────────────────────────────

def build_adjusted_panel(
    panel:   pd.DataFrame,
    symbols: Optional[list] = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Build a fully adjusted price panel for all symbols.

    Args:
        panel   : raw price panel with columns Date, Symbol, Close, ...
        symbols : subset of symbols (default: all in panel)
        verbose : print progress

    Returns:
        Adjusted panel with same schema + adj_factor column.
    """
    if symbols is None:
        symbols = sorted(panel["Symbol"].unique())

    sess   = _make_session()
    frames = []

    for i, sym in enumerate(symbols, 1):
        sym_prices = panel[panel["Symbol"] == sym].copy()
        if sym_prices.empty:
            continue

        if verbose:
            print(f"  [{i:2d}/{len(symbols)}] {sym:<15}", end="  ")

        actions = fetch_corp_actions(sym, session=sess)
        adj     = adjust_prices(sym_prices, actions)
        frames.append(adj)

        n_actions = len(actions) if not actions.empty else 0
        if verbose:
            print(f"{n_actions} corporate actions")

        time.sleep(CONFIG.fetch.request_delay_sec)

    if not frames:
        return pd.DataFrame()

    return (pd.concat(frames, ignore_index=True)
              .sort_values(["Date","Symbol"])
              .reset_index(drop=True))


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch and apply corporate action adjustments")
    parser.add_argument("--symbol", required=True, help="NSE symbol")
    parser.add_argument("--panel",  default="",    help="Raw price panel parquet")
    parser.add_argument("--out",    default="",    help="Output parquet path")
    args = parser.parse_args()

    print(f"Fetching corporate actions for {args.symbol}...")
    actions = fetch_corp_actions(args.symbol.upper())
    print(f"Found {len(actions)} actions:")
    print(actions.to_string(index=False) if not actions.empty else "  None")

    if args.panel and args.out:
        panel = pd.read_parquet(args.panel)
        sym_panel = panel[panel["Symbol"] == args.symbol.upper()]
        adj = adjust_prices(sym_panel, actions)
        adj.to_parquet(args.out, index=False)
        print(f"\nSaved adjusted prices: {args.out}")
