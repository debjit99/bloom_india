"""
bloom_india/data/process/pdf_docling_extractor.py
==================================================
Docling-based PDF table extractor — runs fully locally, no API calls, no rate limits.

Replaces the Pixtral vision pipeline for the page extraction step.
Produces the same page_results format as pdf_extract.py so it's a drop-in replacement.

Why Docling over Pixtral:
  - No rate limits (2 RPM → unlimited)
  - Runs locally — works offline, no API key needed
  - Advanced table structure understanding (merged cells, multi-column)
  - Native XBRL support
  - GraniteDocling VLM (258M params, document-optimized)

Output format matches pdf_extract.py:
    {
      "pdf_stem":     str,
      "symbol":       str,
      "pages":        int,
      "page_results": {
        "1": {
          "has_financial_table": True,
          "statement_type":      "consolidated",
          "columns":             ["Sep 2024", "Sep 2023"],
          "data":                {"Revenue": [81546.2, 75039.1], ...},
        },
        ...
      }
    }

Install:
    pip install docling

Usage:
    from bloom_india.data.process.pdf_docling_extractor import extract_pdf_docling

    result = extract_pdf_docling(
        pdf_path = "/path/to/Q3_FY2025_Results.pdf",
        symbol   = "HDFCBANK",
        log_cb   = print,
    )
    page_results = result["page_results"]
"""

import re
import json
import logging
from pathlib import Path
from typing import Optional, Callable

from bloom_india.config import CONFIG
from bloom_india.data.process.pdf_parse import _clean_num

log = logging.getLogger(__name__)

# ── Statement type classifier ─────────────────────────────────────────────────
# Maps table content keywords → statement type
_STYPE_KEYWORDS = {
    "consolidated": [
        "consolidated", "consl", "cons.",
    ],
    "standalone": [
        "standalone", "stdalone", "std.", "parent",
    ],
    "balance_sheet": [
        "balance sheet", "assets", "liabilities", "equity",
        "net worth", "fixed assets", "current assets",
    ],
    "cash_flow": [
        "cash flow", "cash from operations", "investing activities",
        "financing activities", "net cash",
    ],
    "segment": [
        "segment", "segmental", "business segment",
        "geographic segment", "treasury", "retail banking",
    ],
}

def _classify_statement(text: str) -> str:
    """Classify a table's statement type from surrounding text."""
    lower = text.lower()
    for stype, keywords in _STYPE_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return stype
    return "other"

def _is_financial_table(headers: list, rows: list) -> bool:
    """Heuristic: does this table look like a financial statement?"""
    if not rows or len(rows) < 3:
        return False
    # Check if any cell looks like a financial number
    n_numeric = 0
    for row in rows[:10]:
        for cell in row:
            val = _clean_num(str(cell))
            if val is not None and abs(val) > 0.01:
                n_numeric += 1
    return n_numeric >= 3

def _detect_columns(headers: list) -> list:
    """
    Extract quarter/year column labels from table headers.
    Looks for patterns like "Sep 2024", "Q3 FY2025", "3 months ended..."
    """
    cols = []
    for h in headers:
        h_str = str(h).strip()
        if not h_str or h_str.lower() in ("particulars","description","item","sr.no","no."):
            continue
        # Keep if looks like a period label
        if any(x in h_str for x in ["20", "FY", "Q1","Q2","Q3","Q4",
                                      "Jan","Feb","Mar","Apr","May","Jun",
                                      "Jul","Aug","Sep","Oct","Nov","Dec",
                                      "ended","ending","period"]):
            cols.append(h_str)
        elif h_str and not h_str.isdigit():
            cols.append(h_str)
    return cols[:4]  # max 4 columns (current + 3 comparatives)

def _table_to_data(table, col_idx_map: list) -> dict:
    """
    Convert a Docling table to {label: [values]} dict.
    col_idx_map: list of column indices to extract (skip label column).
    """
    data = {}
    try:
        # Docling TableItem has .data.grid (list of rows of cells)
        grid = table.data.grid
        if not grid:
            return data

        # Find header row (first row)
        header_row = grid[0] if grid else []

        for row in grid[1:]:  # skip header
            if not row:
                continue
            # First cell = label
            label = str(row[0].text).strip() if row[0] and row[0].text else ""
            if not label or label.lower() in ("","-","—"):
                continue

            vals = []
            for ci in col_idx_map:
                if ci < len(row) and row[ci] is not None:
                    raw = str(row[ci].text).strip() if row[ci].text else ""
                    num = _clean_num(raw)
                    vals.append(num)
                else:
                    vals.append(None)

            if any(v is not None for v in vals):
                data[label] = vals

    except Exception as e:
        log.warning(f"  [DOCLING] table_to_data error: {e}")

    return data


# ── Main extractor ─────────────────────────────────────────────────────────────

def extract_pdf_docling(
    pdf_path:  str,
    symbol:    str  = "",
    force:     bool = False,
    log_cb:    Optional[Callable] = None,
) -> dict:
    """
    Extract financial tables from a PDF using Docling (local, no API).

    Args:
        pdf_path : path to PDF
        symbol   : NSE ticker
        force    : re-extract even if cached
        log_cb   : callback(str) for progress streaming

    Returns:
        Same format as pdf_extract.py extract_pdf()
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
        "extractor":    "docling",
    }

    if not Path(pdf_path).exists():
        _log(f"  [DOCLING] PDF not found: {pdf_path}")
        return result

    # Check cache
    cache_dir  = Path(CONFIG.storage.raw_dir)/"filings"/symbol.upper()/".cache_docling"
    cache_file = cache_dir / f"{pdf_stem}.json"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not force and cache_file.exists():
        _log(f"  [DOCLING] Loading from cache: {cache_file.name}")
        with open(cache_file) as f:
            return json.load(f)

    # Import Docling
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import PdfFormatOption
    except ImportError:
        _log("  [DOCLING] Not installed — pip install docling")
        raise ImportError("pip install docling")

    _log(f"  [DOCLING] Converting {pdf_stem}...")

    # Configure pipeline — use table structure model
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_table_structure = True
    pipeline_options.do_ocr             = False   # PDFs are text-based

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options,
            )
        }
    )

    try:
        doc_result = converter.convert(pdf_path)
        doc        = doc_result.document
    except Exception as e:
        _log(f"  [DOCLING] Conversion error: {e}")
        return result

    _log(f"  [DOCLING] Converted — extracting tables...")

    # Extract tables per page
    page_tables = {}   # page_num → list of tables

    for table in doc.tables:
        # Get page number from table provenance
        try:
            prov = table.prov[0] if table.prov else None
            page_num = prov.page_no if prov else 1
        except Exception:
            page_num = 1
        page_tables.setdefault(page_num, []).append(table)

    n_pages = len(doc.pages) if hasattr(doc, 'pages') and doc.pages else 0
    result["pages"] = n_pages
    _log(f"  [DOCLING] {n_pages} pages, tables on {len(page_tables)} pages")

    for page_num in sorted(page_tables.keys()):
        tables = page_tables[page_num]
        _log(f"  [DOCLING] Page {page_num}: {len(tables)} table(s)")

        # Get surrounding text for statement type classification
        page_text = ""
        try:
            for item in doc.texts:
                if item.prov and item.prov[0].page_no == page_num:
                    page_text += " " + (item.text or "")
        except Exception:
            pass

        best_table    = None
        best_data     = {}
        best_cols     = []
        best_n_rows   = 0

        for table in tables:
            try:
                grid = table.data.grid
                if not grid or len(grid) < 2:
                    continue

                # Extract header row for column detection
                header_row = grid[0]
                headers    = [str(c.text).strip() if c and c.text else "" for c in header_row]
                cols       = _detect_columns(headers)

                # Build col_idx_map — indices of value columns (not label column)
                col_idx_map = []
                for i, h in enumerate(headers):
                    if i == 0:
                        continue  # label column
                    if _clean_num(h) is not None:
                        continue  # skip if header is a number (not a period)
                    col_idx_map.append(i)

                if not col_idx_map:
                    col_idx_map = list(range(1, min(5, len(headers))))

                data = _table_to_data(table, col_idx_map)

                if len(data) > best_n_rows:
                    best_n_rows = len(data)
                    best_data   = data
                    best_cols   = cols
                    best_table  = table

            except Exception as e:
                log.warning(f"  [DOCLING] Table parse error page {page_num}: {e}")
                continue

        if best_data:
            stype       = _classify_statement(page_text)
            is_fin      = _is_financial_table(best_cols, list(best_data.values()))
            result["page_results"][str(page_num)] = {
                "has_financial_table": is_fin,
                "statement_type":      stype,
                "columns":             best_cols,
                "data":                best_data,
                "_page":               page_num,
                "_pdf_stem":           pdf_stem,
                "_symbol":             symbol,
                "extractor":           "docling",
            }
            _log(f"  [DOCLING] Page {page_num} → [{stype}] {len(best_data)} rows"
                 + (" ✓" if is_fin else " (no financial table)"))
        else:
            result["page_results"][str(page_num)] = {
                "has_financial_table": False,
                "statement_type":      "other",
                "columns":             [],
                "data":                {},
                "_page":               page_num,
                "_pdf_stem":           pdf_stem,
                "_symbol":             symbol,
                "extractor":           "docling",
            }

    n_fin = sum(1 for p in result["page_results"].values() if p.get("has_financial_table"))
    _log(f"  [DOCLING] Done: {n_pages} pages, {n_fin} with financial tables")

    # Save to cache
    with open(cache_file, "w") as f:
        json.dump(result, f, indent=2)
    _log(f"  [DOCLING] Cached → {cache_file.name}")

    return result


# ── Symbol-level extractor ────────────────────────────────────────────────────

def extract_pdf_docling_symbol(
    symbol:   str,
    pdf_stem: str,
    force:    bool = False,
    log_cb:   Optional[Callable] = None,
) -> dict:
    """
    Extract a specific PDF for a symbol using Docling.
    Saves result to raw_extractions.json (same format as Pixtral pipeline).
    """
    def _log(msg):
        log.info(msg)
        if log_cb:
            try: log_cb(msg)
            except Exception: pass

    filings_dir = Path(CONFIG.storage.raw_dir)/"filings"/symbol.upper()
    pdf_path    = filings_dir / f"{pdf_stem}.pdf"

    if not pdf_path.exists():
        _log(f"  [DOCLING] PDF not found: {pdf_path}")
        return {}

    raw = extract_pdf_docling(
        pdf_path = str(pdf_path),
        symbol   = symbol,
        force    = force,
        log_cb   = log_cb,
    )

    # Save to raw_extractions.json
    raw_path = filings_dir / "raw_extractions.json"
    all_raw  = {}
    if raw_path.exists():
        try:
            with open(raw_path) as f:
                all_raw = json.load(f)
        except Exception:
            pass

    all_raw[pdf_stem] = raw
    with open(raw_path, "w") as f:
        json.dump(all_raw, f, indent=2)

    return raw