"""
bloom_india/mf/api/funds.py
============================
Public API for mutual fund data.
"""

import pandas as pd
import logging
from typing import Optional

from bloom_india.mf.fetch.mfapi import fetch_all_schemes, fetch_scheme_nav
from bloom_india.mf.process.enrich import (
    build_scheme_catalogue,
    enrich_nav_history,
    compute_returns,
    _normalise_category,
)
from bloom_india.mf.cache.disk_cache import get_cache, set_cache, cache_stats

log = logging.getLogger(__name__)

_SNAPSHOT_CACHE_KEY = "mf:fund_snapshot:v1"


def list_funds(
    fund_house      : Optional[str]   = None,
    category        : Optional[str]   = None,
    scheme_type     : Optional[str]   = None,
    sort_by         : str             = "scheme_name",
    ascending       : bool            = True,
    limit           : Optional[int]   = None,
) -> pd.DataFrame:
    """
    List mutual funds from the lightweight scheme catalogue.

    Fast — uses only the scheme list endpoint (no per-fund NAV fetches).
    Returns: scheme_code, scheme_name, fund_house.
    For NAV + returns use get_fund(scheme_code) per fund.
    """
    df = build_scheme_catalogue()
    if df.empty:
        return df

    if fund_house:
        df = df[df["fund_house"].str.contains(fund_house, case=False, na=False)]
    if scheme_type:
        # scheme_type heuristic from name
        df = df[df["scheme_name"].str.contains(scheme_type, case=False, na=False)]
    if category:
        df = df[df["scheme_name"].str.contains(category, case=False, na=False)]

    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=ascending)
    if limit:
        df = df.head(limit)
    return df.reset_index(drop=True)


def get_fund_snapshot(
    scheme_codes: list[int],
) -> pd.DataFrame:
    """
    Build a NAV + returns snapshot for a specific list of scheme codes.

    Fetches NAV for each code individually (cached). Use for small sets (≤50).
    """
    from bloom_india.mf.process.enrich import build_fund_snapshot
    return build_fund_snapshot(scheme_codes=scheme_codes)


def get_fund(scheme_code: int) -> Optional[dict]:
    """Get full metadata + latest NAV + returns for one fund."""
    import math
    info = fetch_scheme_nav(scheme_code)
    if not info:
        return None
    meta    = info.get("meta", {})
    nav_df  = enrich_nav_history(info)
    returns = compute_returns(nav_df)
    # Sanitise returns — replace nan/inf with None
    returns = {
        k: (None if v is not None and isinstance(v, float) and not math.isfinite(v) else v)
        for k, v in returns.items()
    }
    latest_nav = float(nav_df["nav"].iloc[-1]) if not nav_df.empty else None
    if latest_nav is not None and not math.isfinite(latest_nav):
        latest_nav = None
    return {
        "scheme_code"    : scheme_code,
        "scheme_name"    : meta.get("scheme_name", ""),
        "fund_house"     : meta.get("fund_house", ""),
        "scheme_type"    : meta.get("scheme_type", ""),
        "scheme_category": meta.get("scheme_category", ""),
        "category_clean" : _normalise_category(meta.get("scheme_category", "")),
        "latest_nav"     : latest_nav,
        "nav_date"       : nav_df["date"].iloc[-1].strftime("%Y-%m-%d") if not nav_df.empty else None,
        "returns"        : returns,
    }


def get_nav_history(
    scheme_code: int,
    from_date  : Optional[str] = None,
    to_date    : Optional[str] = None,
) -> pd.DataFrame:
    """Get NAV history for a fund."""
    info = fetch_scheme_nav(scheme_code)
    if not info:
        return pd.DataFrame(columns=["date", "nav"])
    nav_df = enrich_nav_history(info)
    if from_date:
        nav_df = nav_df[nav_df["date"] >= pd.Timestamp(from_date)]
    if to_date:
        nav_df = nav_df[nav_df["date"] <= pd.Timestamp(to_date)]
    return nav_df.reset_index(drop=True)


def get_fund_returns(scheme_code: int) -> Optional[dict]:
    """Get trailing returns for a fund."""
    info = fetch_scheme_nav(scheme_code)
    if not info:
        return None
    return compute_returns(enrich_nav_history(info))


def search_funds(query: str, limit: int = 20) -> pd.DataFrame:
    """Search funds by name or fund house."""
    catalogue = build_scheme_catalogue()
    mask = (
        catalogue["scheme_name"].str.contains(query, case=False, na=False) |
        catalogue["fund_house"].str.contains(query, case=False, na=False)
    )
    return catalogue[mask].head(limit).reset_index(drop=True)


def get_cache_info() -> dict:
    """Return current cache statistics."""
    return cache_stats()