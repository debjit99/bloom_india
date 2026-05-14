"""
bloom_india/data/process/pdf_gemini_extractor.py
=================================================
Gemini-based PDF page extractor — drop-in replacement for Pixtral.

Why Gemini over Pixtral:
  - 15 RPM free tier (vs 2 RPM) → 7.5x faster
  - gemini-2.5-flash has excellent table understanding
  - Same JSON output format as pdf_extract.py

Cache:
    {base_dir}/raw/filings/{SYMBOL}/.cache_gemini/{PDF_STEM}/page_NNN.json
    (separate from Pixtral cache so both coexist for comparison)

Usage:
    from bloom_india.data.process.pdf_gemini_extractor import (
        extract_pdf_gemini, extract_pdf_gemini_symbol
    )

    result = extract_pdf_gemini(
        pdf_path = "/path/to/Q3_FY2025_Results.pdf",
        symbol   = "HDFCBANK",
        model    = "gemini-2.5-flash",
        log_cb   = print,
    )
"""

import os, re, json, time, logging
from pathlib import Path
from typing  import Optional, Callable

from bloom_india.config import CONFIG
from bloom_india.data.process.pdf_parse import _clean_num

log = logging.getLogger(__name__)

MODEL        = "gemini-2.5-flash"
RATE_LIMIT   = 14    # RPM — stay just under 15
DPI          = 200
MAX_PAGES    = 50
RETRY_WAITS  = [5, 15, 30, 60]   # seconds per retry attempt

PROMPT = """This is a page from an Indian company quarterly results PDF filing (BSE/NSE).
Extract ALL financial data from any tables on this page.

Return ONLY valid JSON, nothing else:
{
  "has_financial_table": true,
  "statement_type": "consolidated",
  "columns": ["Sep 2024", "Sep 2023"],
  "data": {
    "Interest earned (a)+(b)+(c)+(d)": [85040.17, 78008.17],
    "Net Profit before minority interest": [19854.84, 17718.00]
  }
}

Rules:
- has_financial_table: true only if page has a P&L / Balance Sheet / Cash Flow / NPA table
- statement_type: consolidated / standalone / balance_sheet / cash_flow / segment / other
- columns: period headers exactly as shown, current period first (e.g. "Dec 2024", "Sep 2024")
- data: EVERY row label → list of numeric values in same column order
- Numbers: raw numeric only — no commas, no currency symbols, no units
- Include ALL rows including sub-items, totals, ratios, EPS
- If no financial table on this page: {"has_financial_table": false, "statement_type": "other", "columns": [], "data": {}}
- Return raw JSON only, no markdown fences"""


def _get_client():
    """Get Gemini client."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set. Get free key at https://aistudio.google.com/apikey")
    try:
        from google import genai
        return genai.Client(api_key=api_key)
    except ImportError:
        raise ImportError("pip install google-genai")


def _call_gemini(client, img, model: str) -> dict:
    """Call Gemini with one page image. Returns parsed page_data dict."""
    for attempt, wait in enumerate([0] + RETRY_WAITS):
        if wait:
            log.info(f"  [GEMINI] Retry {attempt} — waiting {wait}s...")
            time.sleep(wait)
        try:
            response = client.models.generate_content(
                model    = model,
                contents = [PROMPT, img],
            )
            text = response.text or ""
            # Strip markdown fences if present
            text = re.sub(r"```(?:json)?", "", text).strip()
            m    = re.search(r"\{[\s\S]*\}", text)
            if not m:
                log.warning(f"  [GEMINI] No JSON in response: {text[:100]}")
                return {"has_financial_table": False, "statement_type": "other",
                        "columns": [], "data": {}, "_error": "no_json"}
            return json.loads(m.group(0))

        except Exception as e:
            err = str(e)
            is_rate = "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower()
            if is_rate and attempt < len(RETRY_WAITS):
                log.warning(f"  [GEMINI] Rate limit on attempt {attempt+1} — will retry")
                continue
            log.error(f"  [GEMINI] Error: {e}")
            return {"has_financial_table": False, "statement_type": "other",
                    "columns": [], "data": {}, "_error": err[:200]}

    return {"has_financial_table": False, "statement_type": "other",
            "columns": [], "data": {}, "_error": "max_retries"}


def _page_cache_path(symbol: str, pdf_stem: str, page_num: int, model: str) -> Path:
    # Sanitize model name for filesystem: gemini-2.5-flash → gemini-2.5-flash
    model_slug = re.sub(r"[^\w\-]", "_", model)
    p = (Path(CONFIG.storage.raw_dir)/"filings"/symbol.upper()/
         f".cache_{model_slug}"/pdf_stem/f"page_{page_num:03d}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def extract_pdf_gemini(
    pdf_path: str,
    symbol:   str            = "",
    model:    str            = MODEL,
    force:    bool           = False,
    log_cb:   Optional[Callable] = None,
) -> dict:
    """
    Extract financial tables from a PDF using Gemini vision.
    Same output format as pdf_extract.py extract_pdf().

    Args:
        pdf_path : path to PDF file
        symbol   : NSE ticker
        model    : Gemini model name (default: gemini-2.5-flash)
        force    : re-extract even if cached
        log_cb   : callback(str) for live progress streaming

    Returns:
        {
          "pdf_stem":     str,
          "symbol":       str,
          "pages":        int,
          "page_results": {page_num: page_data},
          "extractor":    "gemini",
        }
    """
    def _log(msg):
        log.info(msg)
        if log_cb:
            try: log_cb(msg)
            except Exception: pass

    pdf_path = str(pdf_path)
    pdf_stem = Path(pdf_path).stem
    if not symbol:
        symbol = Path(pdf_path).parent.name.upper()

    result = {
        "pdf_stem":     pdf_stem,
        "pdf_path":     pdf_path,
        "symbol":       symbol,
        "pages":        0,
        "page_results": {},
        "extractor":    "gemini",
        "model":        model,
    }

    if not Path(pdf_path).exists():
        _log(f"  [GEMINI] PDF not found: {pdf_path}")
        return result

    # Convert PDF to images
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise ImportError("pip install pdf2image pillow")

    _log(f"  [GEMINI] Converting {pdf_stem} to images (DPI={DPI})...")
    try:
        images = convert_from_path(pdf_path, dpi=DPI)
    except Exception as e:
        _log(f"  [GEMINI] pdf2image error: {e}")
        return result

    if len(images) > MAX_PAGES:
        _log(f"  [GEMINI] Skipping — {len(images)} pages > MAX_PAGES={MAX_PAGES}")
        return result

    result["pages"] = len(images)
    _log(f"  [GEMINI] {len(images)} pages — model={model}")

    # Get Gemini client
    try:
        client = _get_client()
    except Exception as e:
        _log(f"  [GEMINI] Client error: {e}")
        return result

    last_api_call = 0.0
    n_cached = n_new = 0
    min_gap  = 60.0 / RATE_LIMIT   # seconds between calls

    for i, img in enumerate(images):
        page_num   = i + 1
        cache_path = _page_cache_path(symbol, pdf_stem, page_num, model)

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
            _log(f"  [GEMINI] Page {page_num:2d}/{len(images)} [CACHE] {status}")
            continue

        # ── Rate limit ────────────────────────────────────────────────────────
        elapsed = time.time() - last_api_call
        wait    = min_gap - elapsed
        if wait > 0 and last_api_call > 0:
            _log(f"  [GEMINI] Page {page_num:2d}/{len(images)} waiting {wait:.1f}s...")
            time.sleep(wait)

        _log(f"  [GEMINI] Page {page_num:2d}/{len(images)} calling {model}...")

        # ── Call Gemini ───────────────────────────────────────────────────────
        page_raw      = _call_gemini(client, img, model)
        last_api_call = time.time()

        # Add metadata
        page_raw["_page"]     = page_num
        page_raw["_pdf_stem"] = pdf_stem
        page_raw["_symbol"]   = symbol
        page_raw["_model"]    = model

        # Save to cache — only save successes, never errors
        # 503/rate-limit errors will retry on next run
        if not page_raw.get("_error"):
            with open(cache_path, "w") as f:
                json.dump(page_raw, f, indent=2)

        result["page_results"][str(page_num)] = page_raw
        n_new += 1

        has   = page_raw.get("has_financial_table", False)
        err   = page_raw.get("_error", "")
        stype = page_raw.get("statement_type", "")
        nrows = len(page_raw.get("data", {}))

        if err:
            _log(f"  [GEMINI] Page {page_num:2d}/{len(images)} ERROR: {err[:60]}")
        elif has:
            _log(f"  [GEMINI] Page {page_num:2d}/{len(images)} ✓ [{stype}] {nrows} rows")
        else:
            _log(f"  [GEMINI] Page {page_num:2d}/{len(images)} no table")

    n_fin = sum(1 for p in result["page_results"].values() if p.get("has_financial_table"))
    _log(f"  [GEMINI] Done: {pdf_stem} — {len(images)} pages "
         f"[{n_cached} cached, {n_new} new, {n_fin} with tables]")

    return result


def extract_pdf_gemini_symbol(
    symbol:   str,
    pdf_stem: str,
    model:    str  = MODEL,
    force:    bool = False,
    log_cb:   Optional[Callable] = None,
) -> dict:
    """
    Extract a specific PDF for a symbol using Gemini.
    Saves to raw_extractions.json (same format as Pixtral pipeline).
    """
    filings_dir = Path(CONFIG.storage.raw_dir)/"filings"/symbol.upper()
    pdf_path    = filings_dir/f"{pdf_stem}.pdf"

    raw = extract_pdf_gemini(
        pdf_path = str(pdf_path),
        symbol   = symbol,
        model    = model,
        force    = force,
        log_cb   = log_cb,
    )

    # Save to raw_extractions.json
    raw_path = filings_dir/"raw_extractions.json"
    all_raw  = {}
    if raw_path.exists():
        try:
            with open(raw_path) as f: all_raw = json.load(f)
        except Exception: pass

    all_raw[pdf_stem] = raw
    with open(raw_path, "w") as f:
        json.dump(all_raw, f, indent=2)

    return raw
