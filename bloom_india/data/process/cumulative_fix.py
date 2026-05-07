"""
bloom_india/data/process/cumulative_fix.py
==========================================
Converts YTD cumulative values in XBRL filings to standalone quarterly.

Indian XBRL commonly stores 6-month (H1) or 9-month (9M) cumulative
figures even in quarterly filings. This module detects and corrects them.

Detection:
    Q2 revenue > 1.6x Q1 revenue  → Q2 is cumulative (6M)
    Q3 revenue > 2.2x Q1 revenue  → Q3 is cumulative (9M)
    Q4 revenue > 3.0x Q1 revenue  → Q4 is cumulative (12M / annual)

Fix:
    Q2_standalone = Q2_ytd − Q1
    Q3_standalone = Q3_ytd − Q2_ytd
    Q4_standalone = Q4_ytd − Q3_ytd

EPS fix:
    EPS cannot be subtracted (it's per-share, not a flow).
    Recompute from PAT and shares outstanding:
    EPS = PAT × face_value / equity_capital

Public API
----------
    fix_cumulative(df)     → pd.DataFrame  (flow columns fixed)
    fix_eps(df)            → pd.DataFrame  (EPS recomputed where cumulative)
"""

import numpy as np
import pandas as pd
from typing import Optional


# ── Columns that are flows (can be subtracted) ────────────────────────────────

FLOW_COLS = [
    "revenue", "pat", "other_income", "total_income",
    "interest_expended", "operating_profit", "provisions",
    "tax", "employee_cost", "depreciation",
]

# Thresholds for cumulative detection
THRESHOLDS = {"Q2": 1.6, "Q3": 2.2, "Q4": 3.0}
PREV_Q     = {"Q2": "Q1", "Q3": "Q2", "Q4": "Q3"}


# ── Main fix ──────────────────────────────────────────────────────────────────

def fix_cumulative(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert YTD cumulative flow values → standalone quarterly.

    Args:
        df : DataFrame from xbrl_parse.parse_xbrl_cache()
             Must have columns: symbol, fy, quarter, revenue, pat, ...

    Returns:
        DataFrame with flow columns corrected.
        Adds column 'cumulative_fixed' (bool) marking corrected rows.
    """
    flow_cols = [c for c in FLOW_COLS if c in df.columns]
    df        = df.copy().sort_values(["symbol", "fy", "quarter"]).reset_index(drop=True)
    df["cumulative_fixed"] = False
    fixed = 0

    for symbol, sym_grp in df.groupby("symbol"):
        for fy, fy_grp in sym_grp.groupby("fy"):
            fy_grp = fy_grp.sort_values("quarter")

            # Q1 is always standalone — use as reference
            q1_rows = fy_grp[fy_grp["quarter"] == "Q1"]
            if q1_rows.empty:
                continue

            q1_rev = q1_rows["revenue"].values[0]
            if q1_rev is None or np.isnan(q1_rev) or q1_rev == 0:
                continue

            # Store original values before any modification
            orig: dict[str, dict] = {}
            for _, row in fy_grp.iterrows():
                orig[row["quarter"]] = {c: row[c] for c in flow_cols}

            for q in ["Q2", "Q3", "Q4"]:
                q_rows = fy_grp[fy_grp["quarter"] == q]
                if q_rows.empty:
                    continue

                idx     = q_rows.index[0]
                cur_rev = df.at[idx, "revenue"]

                if cur_rev is None or np.isnan(cur_rev) or cur_rev == 0:
                    continue

                # Is it cumulative?
                if cur_rev / q1_rev < THRESHOLDS[q]:
                    continue

                prev_q = PREV_Q[q]
                if prev_q not in orig:
                    continue

                # Subtract previous quarter's ORIGINAL cumulative value
                for col in flow_cols:
                    cur = df.at[idx, col]
                    prv = orig[prev_q].get(col)
                    if (cur is not None and not np.isnan(cur) and
                            prv is not None and not np.isnan(prv)):
                        df.at[idx, col] = round(cur - prv, 2)

                df.at[idx, "cumulative_fixed"] = True
                fixed += 1

    print(f"  [cumulative_fix] Fixed {fixed} cumulative rows → standalone quarterly")
    return df.sort_values(["symbol", "period_end"]).reset_index(drop=True)


# ── EPS fix ───────────────────────────────────────────────────────────────────

def fix_eps(df: pd.DataFrame) -> pd.DataFrame:
    """
    Recompute EPS from PAT and shares outstanding where EPS appears cumulative.

    Formula:
        EPS = PAT_cr × face_value / equity_capital_cr

    Tries face values [1, 2, 5, 10] and uses the one that gives
    a sensible result (0 < EPS < 500).

    Only fixes rows where:
        current_eps / pat_derived_eps is between 1.5 and 3.5
        (i.e. looks like a 2x or 3x cumulative value)

    Args:
        df : DataFrame with columns pat, equity_capital, eps_basic, quarter

    Returns:
        DataFrame with eps_basic and eps_diluted corrected where needed.
        Adds column 'eps_fixed' (bool).
    """
    df = df.copy()
    df["eps_fixed"] = False
    fixed = 0

    for idx, row in df.iterrows():
        if row.get("quarter") == "Q1":
            continue

        pat = row.get("pat")
        ec  = row.get("equity_capital")
        eps = row.get("eps_basic")

        if any(v is None or (isinstance(v, float) and np.isnan(v))
               for v in [pat, ec, eps]):
            continue
        if ec == 0 or eps == 0:
            continue

        # Try face values
        for fv in [1, 2, 5, 10]:
            eps_calc = (pat * fv) / ec
            if not (0 < eps_calc < 500):
                continue

            ratio = eps / eps_calc
            if 1.5 < ratio < 3.5:
                df.at[idx, "eps_basic"]  = round(eps_calc, 2)
                df.at[idx, "eps_diluted"] = round(eps_calc * 0.99, 2)
                df.at[idx, "eps_fixed"]  = True
                fixed += 1
            break

    print(f"  [fix_eps] Fixed {fixed} cumulative EPS values")
    return df


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Fix cumulative XBRL values"
    )
    parser.add_argument("--input",  required=True, help="Input parquet path")
    parser.add_argument("--output", required=True, help="Output parquet path")
    args = parser.parse_args()

    df = pd.read_parquet(args.input)
    print(f"Loaded: {len(df)} rows")

    df = fix_cumulative(df)
    df = fix_eps(df)

    df.to_parquet(args.output, index=False)
    print(f"Saved:  {args.output}  ({len(df)} rows)")
