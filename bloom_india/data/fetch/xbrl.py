"""
bloom_india/data/fetch/xbrl.py
===============================
Downloads NSE XBRL quarterly financial filings for all symbols.

Each filing is saved as a JSON file:
    xbrl_dir/SYMBOL/Q3_FY2025_consolidated.json

The JSON contains:
    {symbol, quarter, fy, quarter_label, consolidated,
     xbrl_url, cache_key, raw_xbrl: {tag: value, ...}}

Public API
----------
    fetch_xbrl_filings(symbol)         → list of filing metadata dicts
    download_xbrl(symbol, filings)     → saves JSON files, returns count
    fetch_and_save_all(symbols)        → fetch + save for all symbols
"""

import re
import time
import json
import random
import datetime
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG


# ── Constants ─────────────────────────────────────────────────────────────────

NSE_API       = "https://www.nseindia.com/api/corporates-financial-results"
NSE_HOMEPAGE  = "https://www.nseindia.com"
DATE_FLOOR    = datetime.date(2014, 1, 1)

QUARTER_MAP   = {
    "first":  "Q1",
    "second": "Q2",
    "third":  "Q3",
    "fourth": "Q4",
}


# ── NSE session ───────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": CONFIG.fetch.user_agent,
        "Referer":    NSE_HOMEPAGE,
        "Accept":     "application/json, text/plain, */*",
    })
    try:
        s.get(NSE_HOMEPAGE, timeout=10)
        time.sleep(random.uniform(0.5, 1.0))
    except Exception:
        pass
    return s


# ── XBRL XML parser ───────────────────────────────────────────────────────────

def _parse_xbrl_xml(content: bytes) -> dict:
    """
    Parse XBRL XML into a flat {tag: value} dict.
    Strips XML namespaces from tag names.
    """
    ns_strip = re.compile(r"\{[^}]+\}")
    result   = {}
    try:
        root = ET.fromstring(content)
        for elem in root.iter():
            tag = ns_strip.sub("", elem.tag)
            if tag and elem.text and elem.text.strip():
                result[tag] = elem.text.strip()
    except Exception as e:
        result["_parse_error"] = str(e)
    return result


# ── Quarter / FY helpers ──────────────────────────────────────────────────────

def _parse_quarter_fy(item: dict) -> tuple[str, int]:
    """Extract quarter (Q1-Q4) and FY from NSE API item."""
    relating = item.get("relatingTo", "").lower()
    fy_str   = item.get("financialYear", "")

    quarter = next(
        (v for k, v in QUARTER_MAP.items() if k in relating),
        "Q?"
    )

    try:
        fy = int(fy_str.split("To")[-1].strip().split("-")[-1].strip())
    except Exception:
        fy = 0

    return quarter, fy


# ── Filing list fetch ─────────────────────────────────────────────────────────

def fetch_xbrl_filings(
    symbol:  str,
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """
    Fetch list of all quarterly XBRL filings for a symbol from NSE.

    Returns list of dicts:
        {symbol, quarter, fy, quarter_label, consolidated, xbrl_url, cache_key}
    """
    sess = session or _make_session()

    try:
        resp = sess.get(
            NSE_API,
            params  = {"index": "equities", "period": "Quarterly",
                       "symbol": symbol.upper()},
            timeout = CONFIG.fetch.timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
    except Exception as e:
        print(f"  [xbrl] {symbol}: filing list error — {e}")
        return []

    filings = []
    for item in data:
        xbrl_url = item.get("xbrl", "")
        if not xbrl_url or not xbrl_url.endswith(".xml"):
            continue

        quarter, fy = _parse_quarter_fy(item)
        if not fy:
            continue

        cons_str   = item.get("consolidated", "")
        cons       = "non-consolidated" not in cons_str.lower()
        cons_label = "consolidated" if cons else "standalone"
        cache_key  = f"{quarter}_FY{fy}_{cons_label}"

        filings.append({
            "symbol":        symbol.upper(),
            "quarter":       quarter,
            "fy":            fy,
            "quarter_label": f"{quarter}_FY{fy}",
            "consolidated":  cons,
            "xbrl_url":      xbrl_url,
            "cache_key":     cache_key,
        })

    return filings


# ── Download + save ───────────────────────────────────────────────────────────

def download_xbrl(
    symbol:   str,
    filings:  list[dict],
    xbrl_dir: Optional[str] = None,
    session:  Optional[requests.Session] = None,
    force:    bool = False,
) -> int:
    """
    Download XBRL XML for each filing and save as JSON.

    Save path: xbrl_dir/SYMBOL/cache_key.json

    Args:
        symbol   : NSE symbol
        filings  : list from fetch_xbrl_filings()
        xbrl_dir : root dir (default: CONFIG.storage.xbrl_dir)
        session  : reusable requests session
        force    : re-download even if file exists

    Returns:
        Number of new files downloaded.
    """
    base     = Path(xbrl_dir) if xbrl_dir else Path(CONFIG.storage.xbrl_dir)
    sym_dir  = base / symbol.upper()
    sym_dir.mkdir(parents=True, exist_ok=True)

    sess = session or _make_session()
    new  = 0

    for filing in filings:
        cache_path = sym_dir / f"{filing['cache_key']}.json"

        if cache_path.exists() and not force:
            continue

        # Download XBRL XML
        raw_data = {}
        for attempt in range(CONFIG.fetch.max_retries):
            try:
                r = sess.get(
                    filing["xbrl_url"],
                    timeout = CONFIG.fetch.timeout_sec,
                    headers = {
                        "User-Agent": CONFIG.fetch.user_agent,
                        "Referer":    NSE_HOMEPAGE,
                    },
                )
                r.raise_for_status()
                raw_data = _parse_xbrl_xml(r.content)
                break
            except Exception as e:
                if attempt == CONFIG.fetch.max_retries - 1:
                    raw_data = {"_download_error": str(e)}
                time.sleep(2 ** attempt)

        record = {**filing, "raw_xbrl": raw_data}
        with open(cache_path, "w") as f:
            json.dump(record, f, indent=2)

        new += 1
        time.sleep(random.uniform(0.2, 0.5))

    return new


# ── Fetch all symbols ─────────────────────────────────────────────────────────

def fetch_and_save_all(
    symbols:  list[str],
    xbrl_dir: Optional[str] = None,
    force:    bool = False,
    verbose:  bool = True,
) -> dict[str, int]:
    """
    Fetch and save XBRL filings for all symbols.

    Returns:
        {symbol: n_new_files}
    """
    base    = Path(xbrl_dir) if xbrl_dir else Path(CONFIG.storage.xbrl_dir)
    base.mkdir(parents=True, exist_ok=True)

    sess    = _make_session()
    results = {}

    if verbose:
        print(f"Fetching XBRL filings for {len(symbols)} symbols")
        print(f"Output: {base}")
        print("-" * 50)

    for i, symbol in enumerate(symbols, 1):
        sym_dir  = base / symbol.upper()
        existing = len(list(sym_dir.glob("*.json"))) if sym_dir.exists() else 0

        print(f"  [{i:2d}/{len(symbols)}] {symbol:<15}", end="  ")

        filings = fetch_xbrl_filings(symbol, session=sess)

        if not filings:
            print(f"no filings found")
            results[symbol] = 0
            time.sleep(random.uniform(1.0, 2.0))
            continue

        new = download_xbrl(symbol, filings, xbrl_dir=str(base),
                            session=sess, force=force)
        total = len(list(sym_dir.glob("*.json")))
        print(f"{total} files  (+{new} new)")
        results[symbol] = new

        time.sleep(random.uniform(1.5, 2.5))

    total_new = sum(results.values())
    if verbose:
        print(f"\nDone. {total_new} new filings downloaded.")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch NSE XBRL filings")
    parser.add_argument("--symbol",  default="", help="Single NSE symbol")
    parser.add_argument("--symbols", nargs="+",  help="List of symbols")
    parser.add_argument("--out",     default="", help="Output directory")
    parser.add_argument("--force",   action="store_true")
    args = parser.parse_args()

    from bloom_india.config import ensure_dirs
    ensure_dirs()

    if args.symbol:
        symbols = [args.symbol.upper()]
    elif args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        from bloom_india.data.fetch.universe import NIFTY50
        symbols = NIFTY50

    fetch_and_save_all(
        symbols  = symbols,
        xbrl_dir = args.out or None,
        force    = args.force,
    )
