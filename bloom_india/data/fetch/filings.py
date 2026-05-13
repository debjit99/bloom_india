"""
bloom_india/data/fetch/filings.py
===================================
Downloads quarterly result PDFs for NSE symbols.

Primary  : BSE announcements API  (structured, fast)
Fallback : DuckDuckGo search + URL pattern matching

Cache structure:
    {base_dir}/raw/filings/{SYMBOL}/{Q3_FY2025_Consolidated}.pdf
    {base_dir}/raw/filings/{SYMBOL}/missing.txt   ← quarters that failed

Usage:
    from bloom_india.data.fetch.filings import download_symbol, download_all

    # Download all quarters for HDFCBANK
    download_symbol("HDFCBANK")

    # Download all symbols in universe
    download_all(symbols)
"""

import os
import re
import json
import time
import random
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer":  "https://www.bseindia.com/",
    "Accept":   "application/pdf,*/*",
}

BSE_ATTACH_BUCKETS = ["AttachLive", "AttachHis"]


# ── Path helpers ──────────────────────────────────────────────────────────────

def filings_dir(symbol: str) -> Path:
    p = Path(CONFIG.storage.raw_dir) / "filings" / symbol.upper()
    p.mkdir(parents=True, exist_ok=True)
    return p


def missing_path(symbol: str) -> Path:
    return filings_dir(symbol) / "missing.txt"


def load_missing(symbol: str) -> set:
    p = missing_path(symbol)
    if not p.exists():
        return set()
    with open(p) as f:
        return {
            line.split("#")[0].strip()
            for line in f
            if line.strip() and not line.startswith("#")
        }


def mark_missing(symbol: str, quarter: str, reason: str = ""):
    p  = missing_path(symbol)
    ts = datetime.now().strftime("%Y-%m-%d")
    with open(p, "a") as f:
        f.write(f"{quarter}  # {ts}  {reason}\n")
    log.info(f"  [MISSING] {symbol} {quarter}")


def quarter_on_disk(symbol: str, quarter: str) -> bool:
    d = filings_dir(symbol)
    for suffix in ("Consolidated", "Standalone", "Results"):
        if (d / f"{quarter}_{suffix}.pdf").exists():
            return True
    return False


def expected_quarters(from_fy: int = 2019) -> list:
    """All quarter labels from from_fy up to and including the current quarter."""
    now   = datetime.today()
    month = now.month
    year  = now.year

    # Indian FY: Apr-Jun=Q1, Jul-Sep=Q2, Oct-Dec=Q3, Jan-Mar=Q4
    # FY = year+1 if month >= 4 else year
    if month in (4, 5, 6):
        cur_q, cur_fy = "Q1", year + 1
    elif month in (7, 8, 9):
        cur_q, cur_fy = "Q2", year + 1
    elif month in (10, 11, 12):
        cur_q, cur_fy = "Q3", year + 1
    else:  # Jan, Feb, Mar
        cur_q, cur_fy = "Q4", year

    out = []
    for fy in range(from_fy, cur_fy + 1):
        for q in ["Q1", "Q2", "Q3", "Q4"]:
            out.append(f"{q}_FY{fy}")
            if f"{q}_FY{fy}" == f"{cur_q}_FY{cur_fy}":
                return out
    return out


# ── Quarter label parsing ─────────────────────────────────────────────────────

def _parse_quarter_fy(headline: str, date_str: str) -> tuple:
    h = headline.lower()
    try:
        dt    = datetime.strptime(date_str[:10], "%Y-%m-%d")
        month = dt.month
        year  = dt.year
    except Exception:
        return "Q?", 0

    fy = year + 1 if month >= 4 else year

    # Default from announcement month
    q = {1:"Q3",2:"Q3",3:"Q3",4:"Q4",5:"Q4",6:"Q4",
         7:"Q1",8:"Q1",9:"Q1",10:"Q2",11:"Q2",12:"Q2"}[month]

    # Override from headline text
    if any(x in h for x in ["first quarter",  "june 30",     "jun 30",   " q1 "]):
        q = "Q1"
    elif any(x in h for x in ["second quarter","september 30","sep 30",   " q2 ","half year"]):
        q = "Q2"
    elif any(x in h for x in ["third quarter", "december 31", "dec 31",   " q3 ","nine months"]):
        q = "Q3"
    elif any(x in h for x in ["fourth quarter","march 31",    "mar 31",   " q4 ","year ended"]):
        q = "Q4"

    return q, fy


def _make_label(headline: str, date_str: str, used: set) -> tuple:
    """Returns (quarter_label, file_label) e.g. ('Q3_FY2025', 'Q3_FY2025_Consolidated')"""
    q, fy      = _parse_quarter_fy(headline, date_str)
    base       = f"{q}_FY{fy}"
    h          = headline.lower()
    suffix     = "Consolidated" if "consolidated" in h else \
                 "Standalone"   if "standalone"   in h else "Results"
    file_label = f"{base}_{suffix}"

    # Deduplicate
    n = 2
    label = file_label
    while label in used:
        label = f"{file_label}_v{n}"
        n += 1
    used.add(label)
    return base, label


# ── PDF download ──────────────────────────────────────────────────────────────

def _download_pdf(attach: str, symbol: str, file_label: str) -> Optional[Path]:
    """
    Download PDF from BSE attachment CDN.
    Tries AttachLive first, then AttachHis.
    Returns local Path if successful, None otherwise.
    """
    dest = filings_dir(symbol) / f"{file_label}.pdf"

    if dest.exists():
        return dest

    for bucket in BSE_ATTACH_BUCKETS:
        url = f"https://www.bseindia.com/xml-data/corpfiling/{bucket}/{attach}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, stream=True)
            if r.status_code == 404:
                continue
            r.raise_for_status()

            first_chunk = b""
            with open(dest, "wb") as f:
                for i, chunk in enumerate(r.iter_content(8192)):
                    if i == 0:
                        first_chunk = chunk
                    f.write(chunk)

            ct = r.headers.get("Content-Type", "")
            if first_chunk.startswith(b"%PDF") or "pdf" in ct.lower():
                kb = dest.stat().st_size // 1024
                log.info(f"  [PDF] {file_label}.pdf  ({kb} KB)")
                return dest

            dest.unlink()  # not a PDF — discard

        except Exception as e:
            if dest.exists():
                dest.unlink()
            log.warning(f"  [PDF] {bucket} {file_label}: {e}")

    return None


# ── BSE announcements ─────────────────────────────────────────────────────────

def _bse_download(symbol: str, from_fy: int = 2019) -> tuple:
    """
    Download all Result filings via BSE API.
    Returns (downloaded: list[Path], covered: set[str])
    """
    try:
        from bse import BSE
    except ImportError:
        log.error("pip install bse")
        return [], set()

    downloaded: list = []
    covered:    set  = set()
    used_labels: set = set()

    with BSE(download_folder=str(Path(CONFIG.storage.raw_dir) / "filings")) as bse:
        try:
            scrip = bse.getScripCode(symbol)
        except Exception as e:
            log.error(f"  [BSE] getScripCode {symbol}: {e}")
            return [], set()

        if not scrip:
            return [], set()

        # Paginate through all Result announcements
        all_rows = []
        page = 1
        while True:
            try:
                data  = bse.announcements(
                    scripcode = scrip,
                    category  = "Result",
                    from_date = datetime(from_fy, 1, 1),
                    to_date   = datetime.today(),
                    page_no   = page,
                )
            except Exception as e:
                log.error(f"  [BSE] announcements {symbol}: {e}")
                break

            rows  = data.get("Table", [])
            total = data.get("Table1", [{}])[0].get("ROWCNT", 0)
            if not rows:
                break
            all_rows.extend(rows)
            if len(all_rows) >= total:
                break
            page += 1
            time.sleep(0.3)

        log.info(f"  [BSE] {symbol}: {len(all_rows)} result filings")

        for row in all_rows:
            headline = row.get("HEADLINE", "")
            date_str = row.get("NEWS_DT",  "")
            attach   = row.get("ATTACHMENTNAME", "")

            base, file_label = _make_label(headline, date_str, used_labels)
            covered.add(base)

            if not attach:
                continue

            path = _download_pdf(attach, symbol, file_label)
            if path:
                downloaded.append(path)

            time.sleep(random.uniform(0.3, 0.8))

    return downloaded, covered


# ── DDG fallback search ───────────────────────────────────────────────────────

def _ddg_find_pdf(symbol: str, quarter: str) -> Optional[Path]:
    """
    Search DuckDuckGo for a missing quarter PDF and download it.
    Only used as fallback when BSE doesn't have the filing.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            log.warning("  [DDG] pip install ddgs")
            return None

    queries = [
        f"{symbol} {quarter} quarterly results PDF site:bseindia.com",
        f"{symbol} {quarter} financial results PDF investor relations",
    ]

    for query in queries:
        try:
            with DDGS() as ddg:
                results = list(ddg.text(query, max_results=5))

            for r in results:
                url = r.get("href", "")
                if not url.endswith(".pdf"):
                    continue

                # Try downloading directly
                try:
                    resp = requests.get(
                        url,
                        headers={**HEADERS, "Referer": "/".join(url.split("/")[:3]) + "/"},
                        timeout=30,
                        stream=True,
                    )
                    resp.raise_for_status()
                    first_chunk = b""
                    dest = filings_dir(symbol) / f"{quarter}_Results_ddg.pdf"
                    with open(dest, "wb") as f:
                        for i, chunk in enumerate(resp.iter_content(8192)):
                            if i == 0: first_chunk = chunk
                            f.write(chunk)

                    if first_chunk.startswith(b"%PDF"):
                        kb = dest.stat().st_size // 1024
                        log.info(f"  [DDG] {dest.name}  ({kb} KB)")
                        return dest
                    dest.unlink()

                except Exception:
                    continue

        except Exception as e:
            log.warning(f"  [DDG] search error: {e}")

        time.sleep(random.uniform(1.5, 3.0))

    return None


# ── Public API ────────────────────────────────────────────────────────────────

def download_symbol(
    symbol:        str,
    from_fy:       int       = 2019,
    force:         bool      = False,
    verbose:       bool      = True,
    xbrl_covered:  set       = None,
) -> dict:
    """
    Download all quarterly result PDFs for one symbol.

    Flow:
        1. BSE announcements  →  covers most quarters
        2. Find gaps          →  quarters not returned by BSE
                                 AND not already in XBRL DB
        3. DDG fallback       →  search for each remaining gap
        4. Mark missing       →  write to missing.txt, skip on next run

    Args:
        symbol        : NSE ticker
        from_fy       : download from this financial year (default 2019)
        force         : ignore missing.txt and retry everything
        verbose       : print progress
        xbrl_covered  : set of quarter_labels already in XBRL DB
                        e.g. {'Q1_FY2025', 'Q2_FY2025', ...}
                        DDG will NOT search for these quarters

    Returns:
        {symbol, downloaded, covered, gaps, missing}
    """
    if verbose:
        print(f"\n{'─'*55}")
        print(f"  {symbol}")
        print(f"{'─'*55}")

    if force:
        mp = missing_path(symbol)
        if mp.exists():
            mp.unlink()

    # Step 1: BSE direct download
    paths, covered = _bse_download(symbol, from_fy=from_fy)
    if verbose:
        print(f"  BSE: {len(paths)} PDFs downloaded  |  {len(covered)} quarters covered")

    # Step 2: Find genuine gaps
    # = expected quarters NOT covered by BSE AND NOT in XBRL DB AND NOT on disk
    all_expected  = expected_quarters(from_fy)
    known_missing = load_missing(symbol)
    xbrl_set      = xbrl_covered or set()

    gaps = [
        q for q in all_expected
        if q not in covered          # BSE didn't return it
        and q not in known_missing   # not previously marked unfindable
        and q not in xbrl_set        # not already in our XBRL database
        and not quarter_on_disk(symbol, q)  # not already downloaded
    ]

    if verbose:
        if gaps:
            print(f"  Gaps (need PDF): {gaps}")
        else:
            print(f"  No gaps — fully covered by BSE + XBRL")

    # Step 3: DDG fallback for each genuine gap
    ddg_found  = []
    ddg_failed = []

    for quarter in gaps:
        if verbose:
            print(f"  [DDG] searching {quarter}...", end=" ", flush=True)
        path = _ddg_find_pdf(symbol, quarter)
        if path:
            ddg_found.append(quarter)
            if verbose:
                print("✓")
        else:
            ddg_failed.append(quarter)
            mark_missing(symbol, quarter, "BSE empty + DDG failed")
            if verbose:
                print("✗ → missing.txt")

    if verbose:
        total = len(paths) + len(ddg_found)
        print(f"\n  ✓ {symbol}: {total} PDFs  "
              f"| {len(ddg_failed)} marked missing")

    return {
        "symbol":     symbol,
        "downloaded": len(paths) + len(ddg_found),
        "covered":    list(covered),
        "gaps":       gaps,
        "missing":    ddg_failed,
    }


def download_all(
    symbols:       list,
    from_fy:       int  = 2019,
    force:         bool = False,
    verbose:       bool = True,
    xbrl_covered:  dict = None,
) -> dict:
    """
    Download PDFs for all symbols.

    Args:
        xbrl_covered: {symbol: set of quarter_labels} already in XBRL DB
                      Build with:
                        import pandas as pd
                        db = pd.read_parquet(CONFIG.storage.fundamental_db)
                        xbrl_covered = db.groupby('symbol')['quarter_label'].apply(set).to_dict()

    Returns summary dict.
    """
    xbrl_covered = xbrl_covered or {}
    results = {}

    for i, sym in enumerate(symbols, 1):
        if verbose:
            print(f"\n[{i:3d}/{len(symbols)}]", end="")
        try:
            results[sym] = download_symbol(
                sym,
                from_fy      = from_fy,
                force        = force,
                verbose      = verbose,
                xbrl_covered = xbrl_covered.get(sym, set()),
            )
        except Exception as e:
            log.error(f"  [ERROR] {sym}: {e}")
            results[sym] = {"symbol": sym, "error": str(e)}
        time.sleep(random.uniform(2.0, 4.0))

    total_dl   = sum(r.get("downloaded", 0) for r in results.values())
    total_miss = sum(len(r.get("missing", [])) for r in results.values())
    if verbose:
        print(f"\n{'='*55}")
        print(f"  DONE: {total_dl} PDFs  |  {total_miss} missing")
        print(f"{'='*55}")

    return results