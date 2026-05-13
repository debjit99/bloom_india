"""
bloom_india/scripts/verify_pdf_vs_xbrl.py
==========================================
Checks the latest PDF filing for each symbol against the latest
available XBRL filing. Flags discrepancies and prints a summary.

What it does:
  1. For each symbol in universe:
     a. Find the latest quarter in XBRL DB
     b. Find the latest PDF in filings dir
     c. If PDF quarter > XBRL quarter → extract and report (new data)
     d. If PDF quarter == XBRL quarter → extract and verify against XBRL
     e. Print match/mismatch table

  2. Save a JSON report: {base_dir}/verify_report.json

Usage:
    # Single symbol
    python -m bloom_india.scripts.verify_pdf_vs_xbrl --symbol HDFCBANK

    # Full universe
    python -m bloom_india.scripts.verify_pdf_vs_xbrl

    # Force re-extract (ignore Mistral cache)
    python -m bloom_india.scripts.verify_pdf_vs_xbrl --symbol HDFCBANK --force
"""

import os
import re
import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)s  %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Setup ──────────────────────────────────────────────────────────────────────

os.environ.setdefault(
    "BLOOM_INDIA_CONFIG",
    str(Path(__file__).parents[2] / "config.yaml"),
)

from bloom_india.config import CONFIG
from bloom_india.data.process.pdf_extract       import extract_symbol
from bloom_india.data.process.pdf_mistral_extractor import extract_pdf_fields


# ── Helpers ────────────────────────────────────────────────────────────────────

def _quarter_sort_key(ql: str) -> tuple:
    """Q3_FY2025 → (2025, 3) for sorting."""
    m = re.match(r"Q([1-4])_FY(\d{4})", ql or "")
    if not m:
        return (0, 0)
    q, fy = int(m.group(1)), int(m.group(2))
    # Convert to absolute order: FY2025 Q1=2024Apr, Q2=2024Jul, Q3=2024Oct, Q4=2025Jan
    abs_q = (fy - 2000) * 4 + q
    return (abs_q,)


def _latest_xbrl_quarter(db: pd.DataFrame, symbol: str) -> str:
    """Return the latest quarter_label in XBRL DB for this symbol."""
    rows = db[db["symbol"] == symbol]["quarter_label"].dropna().unique().tolist()
    if not rows:
        return ""
    return max(rows, key=lambda q: _quarter_sort_key(q))


def _latest_pdf(symbol: str) -> tuple:
    """
    Find the latest PDF for a symbol.
    Returns (pdf_stem, quarter_label, pdf_path) or (None, None, None).
    """
    filings_dir = Path(CONFIG.storage.raw_dir) / "filings" / symbol.upper()
    if not filings_dir.exists():
        return None, None, None

    pdfs = sorted(filings_dir.glob("*.pdf"))
    if not pdfs:
        return None, None, None

    # Sort by quarter
    def pdf_sort_key(p):
        m = re.search(r"(Q[1-4])_FY(\d{4})", p.stem)
        if not m:
            return (0,)
        return _quarter_sort_key(f"{m.group(1)}_FY{m.group(2)}")

    latest = max(pdfs, key=pdf_sort_key)
    m = re.search(r"(Q[1-4])_FY(\d{4})", latest.stem)
    quarter_label = f"{m.group(1)}_FY{m.group(2)}" if m else ""
    return latest.stem, quarter_label, latest


def _ensure_extracted(symbol: str, pdf_stem: str) -> dict:
    """
    Ensure raw_extractions.json exists for this symbol and contains pdf_stem.
    Runs Mistral vision extraction if needed.
    Returns page_results for pdf_stem or {}.
    """
    raw_path = (
        Path(CONFIG.storage.raw_dir) / "filings" / symbol.upper() / "raw_extractions.json"
    )

    # Load existing
    all_raw = {}
    if raw_path.exists():
        with open(raw_path) as f:
            all_raw = json.load(f)

    if pdf_stem in all_raw and all_raw[pdf_stem].get("page_results"):
        log.info(f"  [EXTRACT] {symbol}/{pdf_stem}: already extracted")
        return all_raw[pdf_stem].get("page_results", {})

    # Need to extract — run vision model
    log.info(f"  [EXTRACT] {symbol}/{pdf_stem}: running Mistral vision...")
    all_raw_new = extract_symbol(symbol, force=False, verbose=False)
    return all_raw_new.get(pdf_stem, {}).get("page_results", {})


# ── Per-symbol verifier ────────────────────────────────────────────────────────

def verify_symbol(
    symbol:   str,
    db:       pd.DataFrame,
    force:    bool = False,
    verbose:  bool = True,
) -> dict:
    """
    Verify latest PDF against latest XBRL for one symbol.

    Returns:
        {
          "symbol":           str,
          "xbrl_latest":      str,   # e.g. "Q3_FY2025"
          "pdf_latest":       str,   # e.g. "Q4_FY2025"
          "status":           str,   # "NEW_DATA" | "VERIFIED" | "NO_PDF" | "NO_XBRL" | "FAIL"
          "fields":           dict,
          "verdict":          str,
          "score":            int,
          "issues":           list,
          "is_new_quarter":   bool,
        }
    """
    result = {
        "symbol":         symbol,
        "xbrl_latest":    "",
        "pdf_latest":     "",
        "status":         "UNKNOWN",
        "fields":         {},
        "verdict":        "",
        "score":          0,
        "issues":         [],
        "is_new_quarter": False,
    }

    if verbose:
        print(f"\n{'─'*60}")
        print(f"  {symbol}")
        print(f"{'─'*60}")

    # ── Latest XBRL quarter ───────────────────────────────────────────────────
    xbrl_latest = _latest_xbrl_quarter(db, symbol)
    result["xbrl_latest"] = xbrl_latest

    if not xbrl_latest:
        log.warning(f"  {symbol}: no XBRL data")
        result["status"] = "NO_XBRL"
        return result

    if verbose:
        print(f"  Latest XBRL : {xbrl_latest}")

    # ── Latest PDF ────────────────────────────────────────────────────────────
    pdf_stem, pdf_quarter, pdf_path = _latest_pdf(symbol)
    result["pdf_latest"] = pdf_quarter or ""

    if not pdf_stem:
        log.info(f"  {symbol}: no PDFs downloaded")
        result["status"] = "NO_PDF"
        return result

    if verbose:
        print(f"  Latest PDF  : {pdf_quarter} ({pdf_stem})")

    # ── Decide mode ───────────────────────────────────────────────────────────
    xbrl_key = _quarter_sort_key(xbrl_latest)[0]
    pdf_key  = _quarter_sort_key(pdf_quarter)[0] if pdf_quarter else 0

    is_new = pdf_key > xbrl_key
    result["is_new_quarter"] = is_new

    if is_new:
        if verbose:
            print(f"  Mode        : NEW DATA (PDF has {pdf_quarter}, XBRL only has {xbrl_latest})")
        xbrl_row = None   # no XBRL to compare against
    else:
        if verbose:
            print(f"  Mode        : VERIFY (same quarter {pdf_quarter} vs XBRL)")
        rows = db[(db["symbol"] == symbol) & (db["quarter_label"] == pdf_quarter)]
        xbrl_row = rows.iloc[0].to_dict() if not rows.empty else None

    # ── Ensure extracted ──────────────────────────────────────────────────────
    page_results = _ensure_extracted(symbol, pdf_stem)
    if not page_results:
        log.warning(f"  {symbol}: extraction failed for {pdf_stem}")
        result["status"] = "EXTRACTION_FAILED"
        return result

    # ── Extract fields ────────────────────────────────────────────────────────
    extraction = extract_pdf_fields(
        symbol        = symbol,
        pdf_stem      = pdf_stem,
        page_results  = page_results,
        quarter_label = pdf_quarter or "",
        xbrl_row      = xbrl_row,
        force         = force,
        verbose       = verbose,
    )

    result["fields"]  = extraction["fields"]
    result["verdict"] = extraction["verdict"]
    result["score"]   = extraction["score"]
    result["issues"]  = extraction["issues"]
    result["status"]  = "NEW_DATA" if is_new else (
        "VERIFIED" if extraction["verdict"] == "GOOD" else "FAIL"
    )

    if verbose:
        n = len(extraction["fields"])
        print(f"\n  Result: {result['status']} | {extraction['verdict']} "
              f"(score={extraction['score']}) | {n} fields extracted")
        if extraction["issues"]:
            for issue in extraction["issues"]:
                print(f"    ! {issue}")

    return result


# ── Summary printer ────────────────────────────────────────────────────────────

def _print_summary(results: list):
    print(f"\n{'='*70}")
    print(f"  VERIFICATION SUMMARY  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}")
    print(f"  {'Symbol':<15} {'XBRL Latest':<14} {'PDF Latest':<14} {'Status':<15} {'Score':>5} {'Verdict'}")
    print(f"  {'─'*70}")

    by_status = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    # Print in order: NEW_DATA first (most interesting), then VERIFIED, FAIL, others
    for status in ["NEW_DATA","VERIFIED","FAIL","EXTRACTION_FAILED","NO_PDF","NO_XBRL","UNKNOWN"]:
        for r in by_status.get(status, []):
            score   = r["score"] or 0
            verdict = r["verdict"] or "—"
            sym_col = ("🆕" if status=="NEW_DATA" else
                       "✓"  if status=="VERIFIED" else
                       "✗"  if status=="FAIL"     else "—")
            print(f"  {sym_col} {r['symbol']:<14} {r['xbrl_latest']:<14} "
                  f"{r['pdf_latest']:<14} {status:<15} {score:>5} {verdict}")

    print(f"\n  Totals:")
    for status, items in sorted(by_status.items()):
        print(f"    {status:<20} : {len(items)}")
    print(f"{'='*70}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Verify latest PDF vs latest XBRL for bloom_india universe"
    )
    parser.add_argument("--symbol",  default="", help="Single symbol e.g. HDFCBANK")
    parser.add_argument("--force",   action="store_true", help="Re-extract even if cached")
    parser.add_argument("--quiet",   action="store_true", help="Only print summary")
    args = parser.parse_args()

    # Load fundamentals DB
    log.info("Loading fundamental DB...")
    db = pd.read_parquet(str(CONFIG.storage.fundamental_db))
    log.info(f"  {len(db)} rows, {db['symbol'].nunique()} symbols, "
             f"latest XBRL: {db['quarter_label'].max()}")

    # Load universe
    universe_path = Path(CONFIG.storage.base_dir) / "nifty500.json"
    if universe_path.exists():
        with open(universe_path) as f:
            symbols = json.load(f)
        if isinstance(symbols[0], dict):
            symbols = [s["symbol"] for s in symbols]
    else:
        symbols = db["symbol"].unique().tolist()

    # Filter to single symbol if requested
    if args.symbol:
        symbols = [args.symbol.upper()]

    log.info(f"Processing {len(symbols)} symbols...")

    # Run verification
    results = []
    for i, sym in enumerate(symbols, 1):
        try:
            r = verify_symbol(
                sym, db,
                force   = args.force,
                verbose = not args.quiet,
            )
            results.append(r)
        except Exception as e:
            log.error(f"  {sym}: {e}")
            results.append({
                "symbol": sym, "status": "ERROR",
                "xbrl_latest":"","pdf_latest":"",
                "fields":{},"verdict":"","score":0,
                "issues":[str(e)],"is_new_quarter":False,
            })

        # Progress
        if args.quiet and i % 10 == 0:
            print(f"  [{i}/{len(symbols)}] processed...")

    # Print summary
    _print_summary(results)

    # Save report
    report_path = Path(CONFIG.storage.base_dir) / "verify_report.json"
    with open(report_path, "w") as f:
        # Serialise — remove non-serialisable values
        clean = []
        for r in results:
            rc = {k: v for k, v in r.items() if k != "fields"}
            rc["fields_extracted"] = len(r.get("fields", {}))
            rc["key_fields"] = {
                k: r["fields"].get(k)
                for k in ["revenue","pat","eps_basic","eps_diluted"]
                if r["fields"].get(k) is not None
            }
            clean.append(rc)
        json.dump(clean, f, indent=2)

    log.info(f"Report saved: {report_path}")

    # Return exit code
    fails = sum(1 for r in results if r["status"] == "FAIL")
    return 1 if fails > 0 else 0


if __name__ == "__main__":
    sys.exit(main())