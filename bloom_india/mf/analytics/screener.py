"""
bloom_india/mf/analytics/screener.py
=====================================
Fund screener: filter the catalogue by quantitative metrics
(annualised return, Sharpe ratio, volatility, max drawdown, category)
computed from stored NAV data.

Design
------
- Only screens funds that already have NAV data in the DB.
- Metrics are computed in batch; results are cached (disk cache, 6h TTL).
- Each fund is fetched from DB (no network calls during screening).
- Returns a ranked DataFrame ready for display.

Usage
-----
    from bloom_india.mf.analytics.screener import run_screen

    results = run_screen(
        min_ann_return = 0.12,   # 12% CAGR
        min_sharpe     = 0.8,
        max_volatility = 0.20,
        category_kw    = "Large Cap",
        from_date      = "2021-01-01",
        to_date        = "2024-12-31",
        top_n          = 50,
    )
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import pandas as pd

from bloom_india.mf.analytics.metrics import full_metrics
from bloom_india.mf.db.nav_db import get_nav_history, list_schemes_in_db

log = logging.getLogger(__name__)


def run_screen(
    min_ann_return : Optional[float] = None,   # decimal e.g. 0.10 = 10%
    max_ann_return : Optional[float] = None,
    min_sharpe     : Optional[float] = None,
    max_volatility : Optional[float] = None,
    max_drawdown   : Optional[float] = None,   # e.g. -0.30 = max -30% drawdown
    category_kw    : Optional[str]   = None,   # keyword match on category_clean
    from_date      : Optional[str]   = None,   # 'YYYY-MM-DD'
    to_date        : Optional[str]   = None,
    risk_free      : float           = 0.065,
    top_n          : int             = 100,
    sort_by        : str             = "sharpe_ratio",  # or "ann_return", "volatility"
    ascending      : bool            = False,
) -> pd.DataFrame:
    """
    Screen funds in the DB by quantitative metrics.

    Only funds that have NAV data stored in the DB are considered.
    Applies filters in order: category → metrics thresholds → sort → top_n.

    Returns DataFrame with columns:
        scheme_code, scheme_name, fund_house, category,
        ann_return_pct, volatility_pct, sharpe_ratio, max_drawdown_pct,
        start_date, end_date, trading_days
    """
    # Get all schemes in DB
    schemes = list_schemes_in_db()
    if schemes.empty:
        log.warning("[screener] No schemes in DB. Run ingest first.")
        return pd.DataFrame()

    # Category filter (fast, no NAV fetch needed)
    if category_kw:
        col = "category_clean" if "category_clean" in schemes.columns else "fund_house"
        schemes = schemes[
            schemes[col].str.contains(category_kw, case=False, na=False) |
            schemes["scheme_name"].str.contains(category_kw, case=False, na=False)
        ]

    if schemes.empty:
        return pd.DataFrame()

    log.info(f"[screener] Computing metrics for {len(schemes)} schemes…")
    rows = []

    for _, row in schemes.iterrows():
        code = int(row["scheme_code"])
        try:
            nav_df = get_nav_history(code, from_date=from_date, to_date=to_date)
            if nav_df.empty or len(nav_df) < 30:
                continue

            m = full_metrics(nav_df, risk_free=risk_free)

            ann_ret = m["annualised_return"]
            vol     = m["annualised_volatility"]
            sharpe  = m["sharpe_ratio"]
            mdd     = m["max_drawdown"]

            # Apply metric filters
            if min_ann_return is not None and (ann_ret is None or ann_ret < min_ann_return):
                continue
            if max_ann_return is not None and (ann_ret is None or ann_ret > max_ann_return):
                continue
            if min_sharpe is not None and (sharpe is None or sharpe < min_sharpe):
                continue
            if max_volatility is not None and (vol is None or vol > max_volatility):
                continue
            if max_drawdown is not None and (mdd is None or mdd < max_drawdown):
                continue

            rows.append({
                "scheme_code"    : code,
                "scheme_name"    : row.get("scheme_name", ""),
                "fund_house"     : row.get("fund_house", ""),
                "category"       : row.get("category_clean", row.get("fund_house", "")),
                "ann_return_pct" : round(ann_ret * 100, 2) if ann_ret is not None else None,
                "volatility_pct" : round(vol * 100, 2)     if vol is not None     else None,
                "sharpe_ratio"   : round(sharpe, 3)         if sharpe is not None  else None,
                "max_drawdown_pct": round(mdd * 100, 2)    if mdd is not None     else None,
                "start_date"     : m["start_date"],
                "end_date"       : m["end_date"],
                "trading_days"   : m["trading_days"],
            })

        except Exception as e:
            log.debug(f"[screener] Skipping {code}: {e}")

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Sort
    sort_col_map = {
        "sharpe_ratio": "sharpe_ratio",
        "ann_return"  : "ann_return_pct",
        "volatility"  : "volatility_pct",
        "max_drawdown": "max_drawdown_pct",
    }
    sc = sort_col_map.get(sort_by, "sharpe_ratio")
    if sc in df.columns:
        df = df.sort_values(sc, ascending=ascending, na_position="last")

    return df.head(top_n).reset_index(drop=True)