"""
bloom_india/data/process/factors.py
=====================================
Computes all backtest factors from a clean quarterly DataFrame.

All factors are point-in-time safe — they only use data from
prior periods (no look-ahead).

Factors computed:
    pat_margin          PAT / Revenue × 100
    revenue_yoy         Revenue growth vs same quarter last year (%)
    pat_yoy             PAT growth vs same quarter last year (%)
    eps_basic_yoy       EPS growth vs same quarter last year (%)
    sue                 Standardised Unexpected Earnings
                        = (EPS - EPS_4Q_ago) / rolling_std(EPS, 8Q)
    revenue_accel       Revenue YoY growth vs trailing 4Q avg YoY
    margin_change_qoq   PAT margin change vs last quarter (pp)
    margin_change_yoy   PAT margin change vs same quarter last year (pp)
    npa_change          NPA % change QoQ (banks only)
    nii                 Net Interest Income = revenue - interest_expended
    nii_yoy             NII growth vs same quarter last year (%)

Public API
----------
    compute_factors(df)    → pd.DataFrame with factor columns added
"""

import numpy as np
import pandas as pd


def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add factor columns to a clean quarterly fundamental DataFrame.

    Args:
        df : DataFrame from xbrl_parse + cumulative_fix pipeline.
             Must have: symbol, period_end, revenue, pat, eps_basic.

    Returns:
        DataFrame with factor columns added. Original columns unchanged.
    """
    df = df.copy().sort_values(["symbol", "period_end"]).reset_index(drop=True)

    # ── PAT margin ────────────────────────────────────────────────────────────
    df["pat_margin"] = (
        df["pat"] / df["revenue"].replace(0, np.nan)
    ) * 100

    # ── YoY growth — 4 quarters back = same quarter prior year ───────────────
    for col in ["revenue", "pat", "eps_basic"]:
        df[f"{col}_yoy"] = df.groupby("symbol")[col].pct_change(4) * 100

    # ── SUE — Standardised Unexpected Earnings ────────────────────────────────
    # SUE = (EPS_Q - EPS_{Q-4}) / rolling_std(EPS, 8Q)
    # Positive = beat naive expectation, Negative = missed
    df["sue"] = df.groupby("symbol", group_keys=False)["eps_basic"].transform(
        lambda x: (x - x.shift(4)) / x.rolling(8, min_periods=4).std()
    )

    # ── Revenue acceleration ──────────────────────────────────────────────────
    # How much faster/slower is growth vs trailing 4Q average?
    df["revenue_accel"] = df.groupby(
        "symbol", group_keys=False
    )["revenue_yoy"].transform(
        lambda x: x - x.rolling(4, min_periods=2).mean().shift(1)
    )

    # ── Margin changes ────────────────────────────────────────────────────────
    df["margin_change_qoq"] = df.groupby("symbol")["pat_margin"].diff(1)
    df["margin_change_yoy"] = df.groupby("symbol")["pat_margin"].diff(4)

    # ── Bank-specific ─────────────────────────────────────────────────────────
    if "npa_pct_gross" in df.columns:
        df["npa_change"] = df.groupby("symbol")["npa_pct_gross"].diff(1)

    if "interest_expended" in df.columns:
        df["nii"] = (
            df["revenue"].fillna(0) - df["interest_expended"].fillna(0)
        ).where(df["interest_expended"].notna())
        df["nii_yoy"] = df.groupby("symbol")["nii"].pct_change(4) * 100

    added = [c for c in [
        "pat_margin", "revenue_yoy", "pat_yoy", "eps_basic_yoy",
        "sue", "revenue_accel", "margin_change_qoq", "margin_change_yoy",
        "npa_change", "nii", "nii_yoy",
    ] if c in df.columns]

    print(f"  [factors] Added: {', '.join(added)}")
    return df.sort_values(["symbol", "period_end"]).reset_index(drop=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute fundamental factors")
    parser.add_argument("--input",  required=True, help="Input parquet")
    parser.add_argument("--output", required=True, help="Output parquet")
    args = parser.parse_args()

    df = pd.read_parquet(args.input)
    print(f"Loaded: {len(df)} rows")
    df = compute_factors(df)
    df.to_parquet(args.output, index=False)
    print(f"Saved:  {args.output}")
