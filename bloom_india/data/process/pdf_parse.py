"""
bloom_india/data/process/pdf_parse.py
=======================================
Converts raw Mistral vision extraction output (raw_extractions.json)
into the same 134-column schema used by xbrl_parse.py.

The output rows are identical in structure to XBRL rows — they can
be merged directly into the fundamental DB.

Usage:
    from bloom_india.data.process.pdf_parse import parse_symbol_extractions

    rows = parse_symbol_extractions("HDFCBANK")
    df   = pd.DataFrame(rows)
"""

import re
import json
import logging
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG

log = logging.getLogger(__name__)


# ── Number cleaning ───────────────────────────────────────────────────────────

def _clean_num(v) -> Optional[float]:
    """Parse '1,23,456.78' or '(1234)' or '1234.56' → float."""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "--", "—", "nil", "n.a.", "n/a", "na"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[(),\s₹rsinr]", "", s, flags=re.IGNORECASE)
    s = s.replace(",", "")
    try:
        f = float(s)
        return -f if negative else f
    except ValueError:
        return None


def _to_cr(v) -> Optional[float]:
    """Value (assumed in Crores already from PDF) → float."""
    f = _clean_num(v)
    if f is None:
        return None
    return round(f, 2)


def _to_float(v) -> Optional[float]:
    f = _clean_num(v)
    if f is None:
        return None
    return round(f, 4)


# ── Period parsing ────────────────────────────────────────────────────────────

def _parse_period(period_str: str) -> tuple:
    """
    Parse period string → (quarter, fy, period_end_date)
    e.g. '31.12.2024' → ('Q3', 2025, '2024-12-31')
         '31-Dec-2024' → ('Q3', 2025, '2024-12-31')
    """
    if not period_str:
        return "Q?", 0, ""

    # Normalise separators
    s = str(period_str).strip()

    try:
        d = pd.to_datetime(s, format="mixed", dayfirst=True)
        m = d.month
        y = d.year
        fy = y + 1 if m >= 4 else y
        q  = {3:"Q4",6:"Q1",9:"Q2",12:"Q3"}.get(m, "Q?")
        return q, fy, d.strftime("%Y-%m-%d")
    except Exception:
        pass

    return "Q?", 0, ""


# ── Row label matcher ─────────────────────────────────────────────────────────

# Maps row label fragments → our field names
# Longer/more specific patterns must come first
_LABEL_MAP = [
    # ── Banking P&L — must come before generic revenue ────────────────────────
    ("interest earned",                          "interest_earned"),
    ("interest/discount on advances",            "interest_on_advances"),
    ("income on investments",                    "income_on_investments"),
    ("interest on balances with rbi",            "interest_on_rbi"),
    ("other interest",                           "other_interest"),
    ("interest expended",                        "interest_expended"),
    ("operating expenses",                       "operating_expenses_bank"),
    ("operating profit before provision",        "operating_profit_bank"),
    ("provisions and contingencies",             "provisions_bank"),
    ("net interest income",                      "nii"),

    # ── P&L ───────────────────────────────────────────────────────────────────
    ("revenue from operations",                  "revenue"),
    ("net sales",                                "revenue"),
    ("income from operations",                   "revenue"),
    ("other income",                             "other_income"),
    ("total income",                             "total_income"),
    ("cost of materials consumed",               "cost_of_materials"),
    ("purchases of stock-in-trade",              "purchases_stock"),
    ("changes in inventories",                   "inventory_change"),
    ("employee benefit expense",                 "employee_cost"),
    ("employees cost",                           "employee_cost"),
    ("staff expenses",                           "employee_cost"),
    ("finance costs",                            "finance_costs"),
    ("depreciation and amortisation",            "depreciation"),
    ("depreciation",                             "depreciation"),
    ("other expenses",                           "other_expenses"),
    ("total expenses",                           "total_expenses"),
    ("profit before exceptional",                "ebitda"),
    ("exceptional items",                        "exceptional_items"),
    ("profit before tax",                        "profit_before_tax"),
    ("profit from ordinary activities before",   "profit_before_tax"),
    ("current tax",                              "current_tax"),
    ("deferred tax",                             "deferred_tax"),
    ("tax expense",                              "tax"),
    ("profit for the period from continuing",    "pat_continuing"),
    ("profit for the period",                    "pat"),
    ("profit after tax",                         "pat"),
    ("net profit for the period",                "pat"),
    ("net profit for the quarter",               "pat"),
    ("profit for the quarter",                   "pat"),
    ("other comprehensive income",               "other_comprehensive_income"),
    ("total comprehensive income",               "total_comprehensive_income"),
    ("profit attributable to owners",            "pat_owners"),
    ("profit attributable to equity",            "pat_owners"),
    ("non-controlling interest",                 "pat_minority"),
    ("minority interest",                        "pat_minority"),

    # ── EPS — specific patterns first ─────────────────────────────────────────
    ("(a) basic",                                "eps_basic"),
    ("(b) diluted",                              "eps_diluted"),
    ("basic eps",                                "eps_basic"),
    ("diluted eps",                              "eps_diluted"),
    ("basic earnings per share",                 "eps_basic"),
    ("diluted earnings per share",               "eps_diluted"),
    ("earnings per share - basic",               "eps_basic"),
    ("earnings per share - diluted",             "eps_diluted"),

    # ── Banking ratios — % patterns BEFORE gross/net ─────────────────────────
    ("% of gross npa",                           "npa_pct_gross"),
    ("% of net npa",                             "npa_pct_net"),
    ("gross npa %",                              "npa_pct_gross"),
    ("net npa %",                                "npa_pct_net"),
    ("percentage of gross",                      "npa_pct_gross"),
    ("percentage of net",                        "npa_pct_net"),
    ("gross non-performing assets",              "npa_gross_cr"),
    ("(a) gross npas",                           "npa_gross_cr"),
    ("gross npa",                                "npa_gross_cr"),
    ("net non-performing assets",                "npa_net_cr"),
    ("(b) net npas",                             "npa_net_cr"),
    ("net npa",                                  "npa_net_cr"),
    ("capital adequacy ratio",                   "car"),
    ("cet1 ratio",                               "car"),
    ("(car)",                                    "car"),
    ("return on assets",                         "roa"),
    ("return on average assets",                 "roa"),

    # ── Balance sheet ─────────────────────────────────────────────────────────
    ("equity share capital",                     "equity_capital"),
    ("paid-up equity",                           "equity_capital"),
    ("paid up share capital",                    "equity_capital"),
    ("reserves and surplus",                     "reserves_surplus"),
    ("other equity",                             "reserves_surplus"),
    ("total equity",                             "equity_total"),
    ("total assets",                             "total_assets"),
    ("inventories",                              "inventories"),
    ("trade receivables",                        "trade_receivables"),
    ("cash and cash equivalents",                "cash_equivalents"),
    ("borrowings",                               "borrowings_current"),
    ("deposits",                                 "deposits_bank"),

    # ── Cash flow ─────────────────────────────────────────────────────────────
    ("net cash from operating",                  "cfo"),
    ("cash flow from operating",                 "cfo"),
    ("net cash used in investing",               "cfi"),
    ("cash flow from investing",                 "cfi"),
    ("net cash from financing",                  "cff"),
    ("cash flow from financing",                 "cff"),
    ("purchase of property",                     "capex"),
    ("capital expenditure",                      "capex"),
]


def _match_label(label: str) -> Optional[str]:
    """Match a row label to our field name."""
    lower = label.lower().strip()

    # Remove only explicit list prefixes like "(i) ", "(a) ", "1. ", "ii) "
    # Pattern: optional bracket + letter/number + bracket/dot + space
    cleaned = re.sub(r"^[\(\[]+[ivxlcdm\d]+[\)\]\.]+\s*", "", lower).strip()
    # Also handle "a) ", "b) " style
    cleaned = re.sub(r"^[a-d]\)\s+", "", cleaned).strip()

    # Try both original and cleaned
    for text in [lower, cleaned]:
        for pattern, field in _LABEL_MAP:
            if pattern in text:
                return field
    return None


# ── Column selection ──────────────────────────────────────────────────────────

def _find_current_quarter_col(columns: list) -> int:
    """
    Find which column index is the current quarter (most recent).
    Usually the FIRST column is the current quarter.
    Returns 0 by default.
    """
    if not columns:
        return 0

    # Look for "unaudited" in the first column header
    for i, col in enumerate(columns):
        if "unaudited" in str(col).lower():
            return i

    return 0


# ── Page result parser ────────────────────────────────────────────────────────

def _parse_page(page_data: dict, col_idx: int = 0) -> dict:
    """
    Extract field→value dict from one page's Mistral output.
    Uses col_idx to select which column (quarter) to extract.
    """
    if not page_data.get("has_financial_table"):
        return {}

    data    = page_data.get("data", {})
    result  = {}

    for label, values in data.items():
        field = _match_label(label)
        if not field:
            continue
        if field in result:
            continue  # first match wins

        # Get value at col_idx
        if isinstance(values, list) and len(values) > col_idx:
            v = values[col_idx]
        elif isinstance(values, list) and values:
            v = values[0]
        else:
            v = values

        # EPS is a ratio — don't convert to Cr
        if field in ("eps_basic", "eps_diluted", "npa_pct_gross", "npa_pct_net",
                     "car", "roa", "tier1_additional"):
            result[field] = _to_float(v)
        else:
            result[field] = _to_cr(v)

    return result


# ── Full extraction parser ────────────────────────────────────────────────────

def parse_pdf_extraction(raw: dict) -> Optional[dict]:
    """
    Convert one PDF's raw Mistral extraction → single flat row
    matching the xbrl_parse schema.

    Args:
        raw: output of extract_pdf() — has 'page_results', 'symbol' etc.

    Returns:
        dict with same columns as xbrl_parse rows, or None if unusable.
    """
    symbol   = raw.get("symbol", "")
    pdf_stem = raw.get("pdf_stem", "")

    # Find the period_end from any page that has it
    period_end = ""
    col_idx    = 0
    consolidated = True

    for page_num, page_data in sorted(raw.get("page_results", {}).items(),
                                      key=lambda x: int(x[0])):
        if not page_data.get("has_financial_table"):
            continue

        pe = page_data.get("period_end", "")
        if pe and not period_end:
            period_end = pe
            # Find current quarter column
            cols = page_data.get("columns", [])
            col_idx = _find_current_quarter_col(cols)

        stype = page_data.get("statement_type", "")
        if "standalone" in stype:
            consolidated = False
        elif "consolidated" in stype:
            consolidated = True

    if not period_end:
        # Try to infer from PDF filename
        # e.g. Q3_FY2025_Consolidated → period_end=2024-12-31
        m = re.search(r"(Q[1-4])_FY(\d{4})", pdf_stem)
        if m:
            q, fy = m.group(1), int(m.group(2))
            q_to_month = {"Q1":"06","Q2":"09","Q3":"12","Q4":"03"}
            month = q_to_month[q]
            year  = fy - 1 if q == "Q1" else fy if q in ("Q2","Q3") else fy
            if q == "Q4": year = fy
            period_end = f"{year}-{month}-{'30' if month in ('06','09') else '31' if month=='12' else '31'}"

    if not period_end:
        log.warning(f"  [PDF PARSE] {symbol}/{pdf_stem}: no period_end found")
        return None

    quarter, fy, period_end_iso = _parse_period(period_end)
    if not fy:
        return None

    # Merge all pages into one row (later pages override earlier for same field)
    row = {
        "symbol":        symbol,
        "period_end":    period_end_iso,
        "period_days":   91,
        "quarter":       quarter,
        "fy":            fy,
        "quarter_label": f"{quarter}_FY{fy}",
        "consolidated":  consolidated,
        "source":        "pdf",
        "pdf_stem":      pdf_stem,
    }

    # Extract from each page, giving priority to consolidated > standalone
    # and P&L page > other pages
    priority_order = ["consolidated", "standalone", "other"]

    page_data_by_type = {}
    for page_num, page_data in raw.get("page_results", {}).items():
        if not page_data.get("has_financial_table"):
            continue
        stype = page_data.get("statement_type", "other")
        if stype not in page_data_by_type:
            page_data_by_type[stype] = []
        page_data_by_type[stype].append(page_data)

    # Extract in priority order
    for stype in priority_order:
        pages = page_data_by_type.get(stype, [])
        for page_data in pages:
            fields = _parse_page(page_data, col_idx=col_idx)
            for field, value in fields.items():
                if field not in row or row[field] is None:
                    row[field] = value

    # Must have at least revenue or pat to be useful
    if row.get("revenue") is None and row.get("pat") is None:
        log.warning(f"  [PDF PARSE] {symbol}/{pdf_stem}: no revenue or pat found")
        return None

    return row


# ── Symbol parser ─────────────────────────────────────────────────────────────

def parse_symbol_extractions(
    symbol:  str,
    verbose: bool = True,
) -> list:
    """
    Parse all raw extractions for a symbol into structured rows.
    Each row matches the xbrl_parse schema.

    Args:
        symbol  : NSE ticker
        verbose : print progress

    Returns:
        List of dicts, one per filing.
    """
    raw_path = Path(CONFIG.storage.raw_dir) / "filings" / symbol.upper() / "raw_extractions.json"

    if not raw_path.exists():
        log.warning(f"  [PDF PARSE] No raw_extractions.json for {symbol}")
        return []

    with open(raw_path) as f:
        all_raw = json.load(f)

    if verbose:
        print(f"\n  Parsing {symbol}: {len(all_raw)} PDFs")

    rows = []
    for pdf_stem, raw in all_raw.items():
        row = parse_pdf_extraction(raw)
        if row is not None:
            rows.append(row)
            if verbose:
                rev = row.get("revenue", "?")
                pat = row.get("pat", "?")
                print(f"  ✓ {pdf_stem:<45} "
                      f"rev={rev}  pat={pat}")
        else:
            if verbose:
                print(f"  ✗ {pdf_stem:<45} no data extracted")

    if verbose:
        print(f"\n  Parsed: {len(rows)} usable rows from {len(all_raw)} PDFs")

    return rows


# ── Merge with XBRL DB ────────────────────────────────────────────────────────

def merge_pdf_into_db(
    db:      pd.DataFrame,
    symbols: list,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Parse PDF extractions for all symbols and merge into the XBRL DB.

    PDF rows are used ONLY for quarters missing from XBRL.
    XBRL always takes priority.

    Args:
        db      : existing fundamental DB from xbrl_parse
        symbols : list of NSE tickers
        verbose : print progress

    Returns:
        Augmented DataFrame with PDF rows added for missing quarters.
    """
    if verbose:
        print(f"\nMerging PDF extractions into DB...")
        print(f"  XBRL DB: {len(db)} rows, {db['symbol'].nunique()} symbols")

    pdf_rows = []
    for sym in symbols:
        rows = parse_symbol_extractions(sym, verbose=verbose)
        pdf_rows.extend(rows)

    if not pdf_rows:
        if verbose:
            print("  No PDF rows to add.")
        return db

    pdf_df = pd.DataFrame(pdf_rows)
    pdf_df["period_end"] = pd.to_datetime(pdf_df["period_end"], errors="coerce")

    # Only keep PDF rows for quarters NOT already in XBRL
    existing_keys = set(zip(db["symbol"], db["quarter_label"]))
    pdf_df["_key"] = list(zip(pdf_df["symbol"], pdf_df["quarter_label"]))
    new_pdf = pdf_df[~pdf_df["_key"].isin(existing_keys)].drop(columns=["_key"])

    if verbose:
        print(f"  PDF rows total    : {len(pdf_df)}")
        print(f"  New (not in XBRL) : {len(new_pdf)}")

    if new_pdf.empty:
        return db

    # Align columns
    all_cols = list(db.columns)
    for col in all_cols:
        if col not in new_pdf.columns:
            new_pdf[col] = None

    merged = pd.concat(
        [db, new_pdf[all_cols]],
        ignore_index=True,
    ).sort_values(["symbol", "period_end"]).reset_index(drop=True)

    if verbose:
        print(f"  Final DB: {len(merged)} rows  "
              f"({len(merged) - len(db)} added from PDFs)")

    return merged