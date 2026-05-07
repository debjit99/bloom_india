"""
bloom_india/data/process/xbrl_parse.py
=======================================
Parses raw XBRL JSON files (from data/fetch/xbrl.py) into a clean DataFrame.

Handles both banking and non-banking XBRL tag schemas.
Filters out cumulative (YTD) periods — keeps only quarterly filings.
Deduplicates — consolidated preferred over standalone.

Public API
----------
    parse_xbrl_cache(xbrl_dir)    → pd.DataFrame
    parse_one(json_path)          → dict | None
"""

import json
import datetime
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Numeric helpers ───────────────────────────────────────────────────────────

def _to_cr(v) -> Optional[float]:
    """Absolute rupees → Crores. If already small (< 1M) return as-is."""
    if v is None:
        return None
    try:
        f = float(str(v).replace(",", "").strip())
        return round(f / 1e7, 2) if abs(f) > 1e6 else f
    except Exception:
        return None


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def _get_first(raw: dict, *keys) -> Optional[float]:
    """Return first non-None, non-zero numeric value from tag list."""
    for k in keys:
        v = raw.get(k)
        if v is not None:
            try:
                f = float(str(v).replace(",", "").strip())
                if f != 0.0:
                    return f
            except Exception:
                pass
    return None


# ── Period helpers ────────────────────────────────────────────────────────────

def _period_days(raw: dict) -> int:
    """Number of calendar days covered by this XBRL filing."""
    try:
        start = pd.to_datetime(raw.get("startDate", ""))
        end   = pd.to_datetime(
            raw.get("DateOfEndOfReportingPeriod", "") or raw.get("endDate", "")
        )
        return int((end - start).days)
    except Exception:
        return 0


def _parse_period(period_end: str) -> tuple[str, int]:
    """
    '2024-12-31' → ('Q3', 2025)
    '2024-03-31' → ('Q4', 2024)
    """
    try:
        d     = pd.to_datetime(period_end, dayfirst=True)
        month = d.month
        year  = d.year
        fy    = year if month < 4 else year + 1
        q_map = {3: "Q4", 6: "Q1", 9: "Q2", 12: "Q3"}
        q     = q_map.get(month, "Q?")
        return q, fy
    except Exception:
        return "Q?", 0


# ── Single file parser ────────────────────────────────────────────────────────

def parse_one(json_path: str) -> Optional[dict]:
    """
    Parse one XBRL JSON file into a flat dict of metrics.
    Returns None if the file should be skipped.
    """
    with open(json_path) as f:
        rec = json.load(f)

    raw = rec.get("raw_xbrl", {})
    if not raw or "_download_error" in raw or "_parse_error" in raw:
        return None

    # ── Skip non-quarterly periods ────────────────────────────────────────────
    days = _period_days(raw)
    if days > 105 or (0 < days < 60):
        return None

    # ── Period metadata ───────────────────────────────────────────────────────
    period_end = (
        raw.get("DateOfEndOfReportingPeriod")
        or rec.get("period_end", "")
    )
    if not period_end:
        return None

    quarter, fy = _parse_period(period_end)
    if not fy:
        return None

    nature       = raw.get("NatureOfReportStandaloneConsolidated", "").lower()
    consolidated = "consolidated" in nature

    symbol = rec.get("symbol", "")

    return {
        "symbol":        symbol,
        "period_end":    period_end,
        "period_days":   days,
        "quarter":       quarter,
        "fy":            fy,
        "quarter_label": f"{quarter}_FY{fy}",
        "consolidated":  consolidated,

        # ── Revenue ───────────────────────────────────────────────────────────
        "revenue": _to_cr(_get_first(raw,
            "InterestEarned",           # banking
            "RevenueFromOperations",    # non-banking IndAS
            "IncomeFromOperations",     # non-banking old GAAP
            "NetSales",
            "TotalRevenue",
        )),

        # ── PAT ───────────────────────────────────────────────────────────────
        "pat": _to_cr(_get_first(raw,
            "ProfitLossForThePeriod",
            "ProfitLossForPeriod",
            "ProfitLossFromOrdinaryActivitiesAfterTax",
            "ProfitOrLossAttributableToOwnersOfParent",
            "ProfitLossForPeriodFromContinuingOperations",
        )),

        # ── EPS ───────────────────────────────────────────────────────────────
        "eps_basic": _to_float(_get_first(raw,
            "BasicEarningsPerShareBeforeExtraordinaryItems",
            "BasicEarningsPerShareAfterExtraordinaryItems",
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "BasicEarningsLossPerShareFromContinuingOperations",
            "BasicEarningsPerShare",
        )),
        "eps_diluted": _to_float(_get_first(raw,
            "DilutedEarningsPerShareBeforeExtraordinaryItems",
            "DilutedEarningsPerShareAfterExtraordinaryItems",
            "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "DilutedEarningsLossPerShareFromContinuingOperations",
            "DilutedEarningsPerShare",
        )),

        # ── Detailed P&L ──────────────────────────────────────────────────────
        "other_income":      _to_cr(_get_first(raw, "OtherIncome")),
        "total_income":      _to_cr(_get_first(raw, "Income", "TotalIncome")),
        "interest_expended": _to_cr(_get_first(raw, "InterestExpended", "FinanceCosts")),
        "operating_profit":  _to_cr(_get_first(raw,
            "OperatingProfitBeforeProvisionAndContingencies",
            "ProfitBeforeExceptionalItemsAndTax",
        )),
        "provisions":    _to_cr(_get_first(raw, "ProvisionsOtherThanTaxAndContingencies")),
        "tax":           _to_cr(_get_first(raw, "TaxExpense")),
        "employee_cost": _to_cr(_get_first(raw, "EmployeesCost", "EmployeeBenefitExpense")),
        "depreciation":  _to_cr(_get_first(raw,
            "DepreciationDepletionAndAmortisationExpense")),

        # ── Bank-specific ─────────────────────────────────────────────────────
        "npa_gross_cr":  _to_cr(_get_first(raw, "GrossNonPerformingAssets")),
        "npa_net_cr":    _to_cr(_get_first(raw, "NonPerformingAssets")),
        "npa_pct_gross": _to_float(_get_first(raw, "PercentageOfGrossNpa")),
        "npa_pct_net":   _to_float(_get_first(raw, "PercentageOfNpa")),
        "roa":           _to_float(_get_first(raw, "ReturnOnAssets")),
        "car":           _to_float(_get_first(raw, "CET1Ratio", "CapitalAdequacyRatio")),
        "equity_capital":_to_cr(_get_first(raw, "PaidUpValueOfEquityShareCapital")),
    }


# ── Full cache parser ─────────────────────────────────────────────────────────

def parse_xbrl_cache(
    xbrl_dir: Optional[str] = None,
    symbols:  Optional[list] = None,
    verbose:  bool = True,
) -> pd.DataFrame:
    """
    Parse all cached XBRL JSON files into a clean DataFrame.

    Args:
        xbrl_dir : root directory of XBRL cache
                   (default: CONFIG.storage.xbrl_dir)
        symbols  : optional list to filter to specific symbols
        verbose  : print per-symbol progress

    Returns:
        DataFrame with one row per quarterly filing.
        Consolidated preferred over standalone where both exist.
        Columns: symbol, period_end, quarter, fy, quarter_label,
                 consolidated, revenue, pat, eps_basic, ... (all metrics)
    """
    base = Path(xbrl_dir) if xbrl_dir else Path(CONFIG.storage.xbrl_dir)

    if not base.exists():
        raise FileNotFoundError(
            f"XBRL cache directory not found: {base}\n"
            "Run data.fetch.xbrl.fetch_and_save_all() first."
        )

    rows = []

    sym_dirs = sorted([d for d in base.iterdir() if d.is_dir()])
    if symbols:
        sym_dirs = [d for d in sym_dirs if d.name.upper() in
                    [s.upper() for s in symbols]]

    for sym_dir in sym_dirs:
        symbol = sym_dir.name
        files  = sorted(sym_dir.glob("*.json"))

        if verbose:
            print(f"  {symbol:<15} {len(files):>3} files")

        for jf in files:
            row = parse_one(str(jf))
            if row is not None:
                rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce", dayfirst=True)

    # ── Deduplicate ───────────────────────────────────────────────────────────
    # Per symbol+quarter: consolidated > standalone, then most complete
    df["_nulls"] = df[["revenue", "pat", "eps_basic"]].isnull().sum(axis=1)
    df = df.sort_values(
        ["symbol", "quarter_label", "consolidated", "_nulls"],
        ascending=[True, True, False, True],
    )
    df = df.drop_duplicates(subset=["symbol", "quarter_label"], keep="first")
    df = df.drop(columns=["_nulls"])
    df = df.sort_values(["symbol", "period_end"]).reset_index(drop=True)

    if verbose:
        print(f"\n  Parsed: {len(df)} rows, {df['symbol'].nunique()} symbols")
        print(f"  Dates : {df['period_end'].min().date()} → "
              f"{df['period_end'].max().date()}")
        nulls = df[["revenue","pat","eps_basic"]].isnull().sum()
        print(f"  Nulls : revenue={nulls['revenue']} "
              f"pat={nulls['pat']} eps={nulls['eps_basic']}")

    return df


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Parse XBRL cache into DataFrame")
    parser.add_argument("--xbrl-dir", default="", help="XBRL cache directory")
    parser.add_argument("--symbol",   default="", help="Single symbol")
    parser.add_argument("--out",      default="", help="Output parquet path")
    args = parser.parse_args()

    df = parse_xbrl_cache(
        xbrl_dir = args.xbrl_dir or None,
        symbols  = [args.symbol.upper()] if args.symbol else None,
        verbose  = True,
    )

    if args.out:
        df.to_parquet(args.out, index=False)
        print(f"\nSaved: {args.out}")
    else:
        print(df.head(10).to_string())
