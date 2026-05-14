"""
bloom_india/data/process/pdf_extract.py
=========================================
Extracts financial data from quarterly result PDFs using
Mistral pixtral-12b vision model.

Philosophy: Cache everything raw. Never re-hit the API for a cached page.
Process the cached output separately.

Cache structure:
    {base_dir}/raw/filings/{SYMBOL}/.cache/{PDF_STEM}/page_001.json
    {base_dir}/raw/filings/{SYMBOL}/raw_extractions.json   ← all pages all PDFs

Setup:
    pip install mistralai pdf2image pillow
    export MISTRAL_API_KEY="your_key_here"

Mistral free tier: 2 RPM, 1M tokens/month
pixtral-12b-2409 : vision model, handles tables well

Usage:
    from bloom_india.data.process.pdf_extract import extract_symbol, extract_pdf

    # Extract all PDFs for a symbol
    results = extract_symbol("HDFCBANK")

    # Extract single PDF
    result = extract_pdf("/path/to/file.pdf", symbol="HDFCBANK")
"""

import os
import re
import io
import json
import time
import base64
import random
import logging
from pathlib import Path
from typing import Optional

from bloom_india.config import CONFIG

log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

MODEL      = "pixtral-12b-2409"
DPI        = 200          # image resolution for PDF rasterisation
RATE_LIMIT = 2            # Mistral free tier = 2 RPM
MAX_PAGES  = 50           # skip PDFs longer than this (annual reports etc.)

PROMPT = """This is a page from an Indian company quarterly financial results PDF.
Extract ALL financial data from every table on this page — every single row without exception.

Return ONLY a JSON object, nothing else:
{
  "has_financial_table": true,
  "statement_type": "standalone" | "consolidated" | "segment" | "balance_sheet" | "cash_flow" | "other",
  "period_end": "31.12.2024",
  "period_label": "Quarter ended 31 December 2024",
  "columns": ["Quarter ended 31.12.2024 Unaudited", "Quarter ended 30.09.2024 Unaudited", "Year ended 31.03.2024 Audited"],
  "data": {
    "Row label exactly as in document": [value_col1, value_col2, value_col3],
    ...
  }
}

Rules:
- Include EVERY row — P&L lines, sub-totals, EPS, NPA, CAR, segment data, everything
- Use null for blank / dash / nil / N.A. cells
- Keep row labels exactly as they appear — do not rename or merge
- Return numeric values exactly as shown — do not convert or round
- If this page has NO financial table (cover page, auditor note, director report), return exactly:
  {"has_financial_table": false}
- Return ONLY the JSON — no explanation, no markdown, no preamble"""


# ── Path helpers ──────────────────────────────────────────────────────────────

def _filings_dir(symbol: str) -> Path:
    return Path(CONFIG.storage.raw_dir) / "filings" / symbol.upper()


def _page_cache_path(symbol: str, pdf_stem: str, page_num: int,
                     model: str = "pixtral-12b-2409") -> Path:
    model_slug = re.sub(r"[^\w\-]", "_", model)
    cache_dir  = _filings_dir(symbol) / f".cache_{model_slug}" / pdf_stem
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"page_{page_num:03d}.json"


def _raw_output_path(symbol: str) -> Path:
    return _filings_dir(symbol) / "raw_extractions.json"


def _load_raw_output(symbol: str) -> dict:
    p = _raw_output_path(symbol)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def _save_raw_output(symbol: str, data: dict):
    p = _raw_output_path(symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)


# ── Image helpers ─────────────────────────────────────────────────────────────

def _page_to_base64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _extract_json(text: str) -> Optional[dict]:
    """Pull JSON from model response — handles markdown fences."""
    # Try fenced JSON block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Try bare JSON object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


# ── Mistral API call ──────────────────────────────────────────────────────────

def _call_mistral(client, img) -> dict:
    """
    Send one page image to Mistral pixtral.
    Returns raw dict — saves model output alongside parsed JSON.
    On failure returns error dict.
    """
    try:
        resp = client.chat.complete(
            model    = MODEL,
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text",      "text": PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{_page_to_base64(img)}"
                    }},
                ],
            }],
        )
        raw_text = resp.choices[0].message.content or ""
        parsed   = _extract_json(raw_text)

        if parsed is not None:
            parsed["_raw_model_output"] = raw_text
            return parsed
        else:
            return {
                "has_financial_table": False,
                "_error":             "json_parse_failed",
                "_raw_model_output":  raw_text,
            }

    except Exception as e:
        return {
            "has_financial_table": False,
            "_error":             str(e),
            "_raw_model_output":  None,
        }


# ── Single PDF extractor ──────────────────────────────────────────────────────

def extract_pdf(
    pdf_path: str,
    symbol:   str  = "",
    force:    bool = False,
    verbose:  bool = True,
    log_cb          = None,   # callback(str) — called for every page event
) -> dict:
    """
    Extract raw financial data from every page of one PDF.

    Each page is cached individually as:
        .cache/{PDF_STEM}/page_NNN.json

    The API is NEVER called for a cached page (unless force=True).

    Args:
        pdf_path : path to PDF file
        symbol   : NSE ticker (inferred from parent dir if empty)
        force    : re-extract even if cached
        verbose  : print progress

    Returns:
        {
          "pdf_stem":     str,
          "symbol":       str,
          "pages":        int,
          "page_results": {page_num: raw_dict},
        }
    """
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "MISTRAL_API_KEY not set.\n"
            "export MISTRAL_API_KEY='your_key_here'\n"
            "Get a free key at: https://console.mistral.ai/"
        )

    try:
        from mistralai import Mistral
        from pdf2image import convert_from_path
    except ImportError:
        raise ImportError(
            "pip install mistralai pdf2image pillow"
        )

    client   = Mistral(api_key=api_key)
    pdf_stem = Path(pdf_path).stem

    if not symbol:
        symbol = Path(pdf_path).parent.name.upper()

    result = {
        "pdf_stem":     pdf_stem,
        "pdf_path":     str(pdf_path),
        "symbol":       symbol,
        "extracted_at": __import__("datetime").datetime.now().isoformat(),
        "pages":        0,
        "page_results": {},
    }

    if not Path(pdf_path).exists():
        log.error(f"  [PDF] Not found: {pdf_path}")
        return result

    # Convert PDF to images
    if verbose:
        print(f"  Converting {pdf_stem}...")
    try:
        images = convert_from_path(pdf_path, dpi=DPI)
    except Exception as e:
        log.error(f"  [PDF] pdf2image error: {e}")
        return result

    # Skip very long PDFs (annual reports)
    if len(images) > MAX_PAGES:
        if verbose:
            print(f"  [SKIP] {pdf_stem}: {len(images)} pages > MAX_PAGES={MAX_PAGES}")
        return result

    result["pages"] = len(images)
    last_api_call   = 0.0
    n_cached = n_new = 0

    def _log(msg):
        log.info(msg)
        if log_cb:
            try: log_cb(msg)
            except Exception: pass

    _log(f"  [VISION] {pdf_stem}: {len(images)} pages to process")

    for i, img in enumerate(images):
        page_num   = i + 1
        cache_path = _page_cache_path(symbol, pdf_stem, page_num)

        # ── Load from cache ───────────────────────────────────────────────────
        if not force and cache_path.exists():
            with open(cache_path) as f:
                page_raw = json.load(f)
            n_cached += 1
            result["page_results"][str(page_num)] = page_raw
            has   = page_raw.get("has_financial_table", False)
            stype = page_raw.get("statement_type", "")
            nrows = len(page_raw.get("data", {}))
            status = f"[{stype}] {nrows} rows" if has else "no table"
            _log(f"  [VISION] Page {page_num:2d}/{len(images)} [CACHE] {status}")
            if verbose:
                print(f"  Page {page_num:2d}/{result['pages']}  [CACHE]  {status}")
            continue

        # ── Rate limit ────────────────────────────────────────────────────────
        elapsed = time.time() - last_api_call
        wait    = (60.0 / RATE_LIMIT) - elapsed
        if wait > 0 and last_api_call > 0:
            _log(f"  [VISION] Page {page_num:2d}/{len(images)} waiting {wait:.0f}s (rate limit)...")
            if verbose:
                print(f"  [RATE LIMIT] waiting {wait:.1f}s...")
            time.sleep(wait)

        _log(f"  [VISION] Page {page_num:2d}/{len(images)} calling Mistral vision...")
        if verbose:
            print(f"  Page {page_num:2d}/{result['pages']} ...", end=" ", flush=True)

        # ── Call Mistral API with retry ───────────────────────────────────────
        page_raw = None
        for attempt in range(3):
            page_raw      = _call_mistral(client, img)
            last_api_call = time.time()

            if "_error" not in page_raw:
                break

            err = page_raw.get("_error", "")
            if "429" in err or "rate" in err.lower():
                wait = 60 + attempt * 30
                _log(f"  [VISION] Page {page_num} rate limited — waiting {wait}s (attempt {attempt+1})")
                if verbose:
                    print(f"\n  [RATE LIMIT] waiting {wait}s...")
                time.sleep(wait)
            elif "401" in err:
                raise EnvironmentError("Invalid MISTRAL_API_KEY")
            else:
                time.sleep(5)

        # Add metadata
        page_raw["_page"]     = page_num
        page_raw["_pdf_stem"] = pdf_stem
        page_raw["_symbol"]   = symbol

        # ── Save to cache — always, even errors ──────────────────────────────
        with open(cache_path, "w") as f:
            json.dump(page_raw, f, indent=2)

        result["page_results"][str(page_num)] = page_raw
        n_new += 1

        has   = page_raw.get("has_financial_table", False)
        err   = page_raw.get("_error", "")
        stype = page_raw.get("statement_type", "")
        nrows = len(page_raw.get("data", {}))
        if has:
            _log(f"  [VISION] Page {page_num:2d}/{len(images)} ✓ [{stype}] {nrows} rows")
            if verbose: print(f"[{stype}] {nrows} rows")
        elif err:
            _log(f"  [VISION] Page {page_num:2d}/{len(images)} ERROR: {err[:60]}")
            if verbose: print(f"ERROR: {err[:60]}")
        else:
            _log(f"  [VISION] Page {page_num:2d}/{len(images)} no table")
            if verbose: print("skip")

    _log(f"  [VISION] Done: {pdf_stem} — {len(images)} pages [{n_cached} cached, {n_new} new]")

    if verbose:
        print(f"  ✓ {pdf_stem}: {result['pages']} pages  "
              f"[cached={n_cached}  new={n_new}]")

    return result


# ── Symbol extractor ──────────────────────────────────────────────────────────

def extract_symbol(
    symbol:  str,
    force:   bool = False,
    verbose: bool = True,
    log_cb         = None,   # callback(str) — streamed to caller
) -> dict:
    """
    Extract all PDFs for a symbol.
    Saves combined raw output to raw_extractions.json after each PDF.

    Args:
        symbol  : NSE ticker
        force   : re-extract even if cached
        verbose : print progress

    Returns:
        {pdf_stem: extract_result, ...}
    """
    pdf_dir = _filings_dir(symbol)
    pdfs    = sorted(pdf_dir.glob("*.pdf"))

    if not pdfs:
        log.warning(f"  [EXTRACT] No PDFs for {symbol} in {pdf_dir}")
        return {}

    if verbose:
        print(f"\n{'='*55}")
        print(f"  {symbol}  ({len(pdfs)} PDFs)")
        print(f"{'='*55}")

    # Load existing raw extractions (resume-safe)
    all_raw = _load_raw_output(symbol) if not force else {}
    if verbose and all_raw:
        print(f"  Loaded {len(all_raw)} existing extractions")

    for pdf_path in pdfs:
        label = pdf_path.stem

        # Skip if already extracted (unless force)
        if label in all_raw and not force:
            cached_pages = len(all_raw[label].get("page_results", {}))
            if verbose:
                print(f"\n→ {label}  [CACHED {cached_pages} pages]")
            continue

        if verbose:
            print(f"\n→ {label}")

        raw = extract_pdf(
            str(pdf_path),
            symbol  = symbol,
            force   = force,
            verbose = verbose,
            log_cb  = log_cb,
        )

        all_raw[label] = raw

        # Save after every PDF — never lose work
        _save_raw_output(symbol, all_raw)
        if verbose:
            print(f"  Saved → {_raw_output_path(symbol)}")

        time.sleep(random.uniform(1.0, 2.0))

    return all_raw


# ── Status ────────────────────────────────────────────────────────────────────

def extraction_status(symbols: list) -> dict:
    """
    Show extraction status for all symbols.
    Returns {symbol: {pdfs, pages_cached, has_raw_json}}
    """
    status = {}
    for sym in symbols:
        pdf_dir    = _filings_dir(sym)
        pdfs       = list(pdf_dir.glob("*.pdf"))
        cache_root = pdf_dir / ".cache"
        n_pages    = 0
        if cache_root.exists():
            for sub in cache_root.iterdir():
                if sub.is_dir():
                    n_pages += len(list(sub.glob("page_*.json")))
        has_raw = _raw_output_path(sym).exists()
        status[sym] = {
            "pdfs":         len(pdfs),
            "pages_cached": n_pages,
            "has_raw_json": has_raw,
        }
    return status
