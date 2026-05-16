"""
bloom_india/mf/process/enrich.py
=================================
Transforms raw mfapi.in responses into clean, filter-friendly DataFrames.

Functions
---------
    build_scheme_catalogue()  → pd.DataFrame   (master list with metadata)
    enrich_nav_history(info)  → pd.DataFrame   (parsed NAV time series)
    compute_returns(nav_df)   → dict            (1d, 1w, 1m, 3m, 6m, 1y, 3y returns)
    build_fund_snapshot()     → pd.DataFrame   (full catalogue + latest NAV + returns)
"""

import pandas as pd
import numpy as np
import logging
from typing import Optional

from bloom_india.mf.fetch.mfapi import fetch_all_schemes, fetch_scheme_nav

log = logging.getLogger(__name__)


# ── Category normalisation map ─────────────────────────────────────────────────

CATEGORY_ALIASES = {
    "equity scheme - large cap fund"           : "Equity - Large Cap",
    "equity scheme - mid cap fund"             : "Equity - Mid Cap",
    "equity scheme - small cap fund"           : "Equity - Small Cap",
    "equity scheme - multi cap fund"           : "Equity - Multi Cap",
    "equity scheme - flexi cap fund"           : "Equity - Flexi Cap",
    "equity scheme - large & mid cap fund"     : "Equity - Large & Mid Cap",
    "equity scheme - elss"                     : "Equity - ELSS",
    "equity scheme - sectoral/thematic funds"  : "Equity - Sectoral/Thematic",
    "debt scheme - liquid fund"                : "Debt - Liquid",
    "debt scheme - overnight fund"             : "Debt - Overnight",
    "debt scheme - ultra short duration fund"  : "Debt - Ultra Short",
    "debt scheme - short duration fund"        : "Debt - Short Duration",
    "debt scheme - medium duration fund"       : "Debt - Medium Duration",
    "debt scheme - long duration fund"         : "Debt - Long Duration",
    "debt scheme - gilt fund"                  : "Debt - Gilt",
    "debt scheme - corporate bond fund"        : "Debt - Corporate Bond",
    "debt scheme - credit risk fund"           : "Debt - Credit Risk",
    "hybrid scheme - aggressive hybrid fund"   : "Hybrid - Aggressive",
    "hybrid scheme - conservative hybrid fund" : "Hybrid - Conservative",
    "hybrid scheme - balanced hybrid fund"     : "Hybrid - Balanced",
    "hybrid scheme - dynamic asset allocation" : "Hybrid - Dynamic AA",
    "other scheme - index funds"               : "Index Fund",
    "other scheme - etfs"                      : "ETF",
    "other scheme - fund of funds"             : "Fund of Funds",
}


def _normalise_category(raw: str) -> str:
    key = raw.strip().lower()
    return CATEGORY_ALIASES.get(key, raw.strip().title())


# ── Core processors ───────────────────────────────────────────────────────────

def build_scheme_catalogue() -> pd.DataFrame:
    """
    Fetch and clean the full list of MF schemes from mfapi.in.

    Returns:
        DataFrame with columns: scheme_code, scheme_name, fund_house (parsed)
    """
    raw = fetch_all_schemes()
    if not raw:
        return pd.DataFrame(columns=["scheme_code", "scheme_name", "fund_house"])

    df = pd.DataFrame(raw).rename(columns={
        "schemeCode" : "scheme_code",
        "schemeName" : "scheme_name",
    })
    df["scheme_code"] = df["scheme_code"].astype(int)

    # Crude fund house parse: take everything before " - " in the name
    df["fund_house"] = df["scheme_name"].str.split(" - ").str[0].str.strip()

    return df.drop_duplicates("scheme_code").reset_index(drop=True)


def enrich_nav_history(raw_info: dict) -> pd.DataFrame:
    """
    Parse mfapi.in scheme response into a clean NAV DataFrame.

    Args:
        raw_info: dict returned by fetch_scheme_nav()

    Returns:
        DataFrame with columns: date (DatetimeIndex), nav (float)
        Sorted ascending by date.
    """
    if not raw_info or not raw_info.get("data"):
        return pd.DataFrame(columns=["date", "nav"])

    rows = raw_info["data"]
    df   = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y", errors="coerce")
    df["nav"]  = pd.to_numeric(df["nav"], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)
    return df


def compute_returns(nav_df: pd.DataFrame) -> dict:
    """
    Compute trailing returns from a NAV DataFrame.

    Args:
        nav_df: output of enrich_nav_history()

    Returns:
        dict of { "1d": float|None, "1w": ..., "1m": ...,
                  "3m": ..., "6m": ..., "1y": ..., "3y": ... }
        Values are decimal returns (0.15 = +15%).
    """
    if nav_df.empty or len(nav_df) < 2:
        return {p: None for p in ["1d", "1w", "1m", "3m", "6m", "1y", "3y"]}

    nav   = nav_df.set_index("date")["nav"]
    today = nav.index[-1]
    cur   = nav.iloc[-1]

    periods = {
        "1d": 1,
        "1w": 7,
        "1m": 30,
        "3m": 91,
        "6m": 182,
        "1y": 365,
        "3y": 1095,
    }

    result = {}
    for label, days in periods.items():
        cutoff = today - pd.Timedelta(days=days)
        past   = nav[nav.index <= cutoff]
        if past.empty:
            result[label] = None
        else:
            old = past.iloc[-1]
            try:
                v = (cur - old) / old if old > 0 else None
                import math
                result[label] = round(v, 6) if v is not None and math.isfinite(v) else None
            except Exception:
                result[label] = None

    return result


def build_fund_snapshot(
    max_schemes: Optional[int] = None,
    include_categories: Optional[list] = None,
    scheme_codes: Optional[list] = None,
) -> pd.DataFrame:
    """
    Build a full snapshot DataFrame of all funds with NAV + returns.

    This is the main data pipeline entry point.

    Args:
        max_schemes        : cap number of schemes (for testing; None = all)
        include_categories : filter to specific raw category strings from mfapi.in

    Returns:
        DataFrame with columns:
            scheme_code, scheme_name, fund_house,
            scheme_type, scheme_category, category_clean,
            latest_nav, nav_date,
            return_1d, return_1w, return_1m, return_3m,
            return_6m, return_1y, return_3y
    """
    catalogue = build_scheme_catalogue()
    if scheme_codes:
        catalogue = catalogue[catalogue["scheme_code"].isin(scheme_codes)]
    if max_schemes:
        catalogue = catalogue.head(max_schemes)

    records = []
    total   = len(catalogue)

    for i, row in catalogue.iterrows():
        code = int(row["scheme_code"])
        try:
            info = fetch_scheme_nav(code)
            if not info:
                continue

            meta  = info.get("meta", {})
            cat   = meta.get("scheme_category", "")
            stype = meta.get("scheme_type", "")

            if include_categories and cat not in include_categories:
                continue

            nav_df  = enrich_nav_history(info)
            returns = compute_returns(nav_df)

            latest_nav  = nav_df["nav"].iloc[-1]  if not nav_df.empty else None
            latest_date = nav_df["date"].iloc[-1] if not nav_df.empty else None

            records.append({
                "scheme_code"    : code,
                "scheme_name"    : row["scheme_name"],
                "fund_house"     : meta.get("fund_house", row["fund_house"]),
                "scheme_type"    : stype,
                "scheme_category": cat,
                "category_clean" : _normalise_category(cat),
                "latest_nav"     : latest_nav,
                "nav_date"       : latest_date,
                "return_1d"      : returns["1d"],
                "return_1w"      : returns["1w"],
                "return_1m"      : returns["1m"],
                "return_3m"      : returns["3m"],
                "return_6m"      : returns["6m"],
                "return_1y"      : returns["1y"],
                "return_3y"      : returns["3y"],
            })

            if (i + 1) % 100 == 0:
                log.info(f"[build_fund_snapshot] {i+1}/{total} processed")

        except Exception as e:
            log.warning(f"[build_fund_snapshot] Skipping {code}: {e}")

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["nav_date"] = pd.to_datetime(df["nav_date"])
    return df.sort_values("scheme_name").reset_index(drop=True)