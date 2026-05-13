"""
bloom_india/data/process/pdf_mistral_extractor.py
==================================================
Direct field extraction from PDF page JSONs using Mistral.

Ask Mistral: "Here is my financials JSON. What is the revenue number?"
Cache: ONLY Mistral API calls (expensive). Everything else just logged.
Verify: math checks + Screener.in cross-check.

Cache structure (per symbol, per PDF):
    {base_dir}/raw/filings/{SYMBOL}/mistral_cache.json
    {
      "Q1_FY2025_Results": {
        "revenue": {"value": 81546.2, "label": "Interest earned (a)+(b)+(c)+(d)", "page": 7},
        ...
      }
    }

Usage:
    from bloom_india.data.process.pdf_mistral_extractor import extract_pdf_fields

    result = extract_pdf_fields(
        symbol        = "HDFCBANK",
        pdf_stem      = "Q1_FY2025_Results",
        page_results  = raw["Q1_FY2025_Results"]["page_results"],
        quarter_label = "Q1_FY2025",
        xbrl_row      = None,
    )
    print(result["fields"])   # {field: value}
    print(result["verdict"])  # GOOD / WARN / FAIL
"""

import os, re, json, time, logging
from pathlib import Path
from typing  import Optional

from bloom_india.config import CONFIG
from bloom_india.data.process.pdf_parse import _clean_num

log = logging.getLogger(__name__)
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)s  %(message)s",
    datefmt= "%H:%M:%S",
)

MISTRAL_MODEL    = "mistral-small-latest"
RATE_LIMIT_DELAY = 1.5   # seconds between Mistral calls

# ── Fields + questions ────────────────────────────────────────────────────────
# (question_to_mistral, is_ratio)
# is_ratio=True → small number (EPS, %, CAR), False → Crore value

FIELDS = {
    "revenue": (
        "What is the Interest earned (a)+(b)+(c)+(d) value? "
        "This is the FIRST major line in the P&L — the total interest income line. "
        "It is NOT 'Total income (1)+(2)' which includes other income too. "
        "For non-banks look for 'Revenue from operations' or 'Net sales'. "
        "Give the consolidated current quarter value in Crores.",
        False,
    ),
    "other_income": (
        "What is the other income or non-interest income? "
        "Look for 'Other income (a)+(b)' or 'Other income (Refer note...)'. "
        "Consolidated current quarter, Crores.",
        False,
    ),
    "total_income": (
        "What is the total income? "
        "'Total income (1)+(2)' = interest earned + other income. "
        "Consolidated current quarter, Crores.",
        False,
    ),
    "interest_expended": (
        "What is the total interest expended? "
        "'Interest expended' TOTAL line. Consolidated current quarter, Crores.",
        False,
    ),
    "employee_cost": (
        "What is the employee cost or staff expenses? "
        "Look for 'i) Employees cost' — a sub-item of operating expenses. "
        "Consolidated current quarter, Crores.",
        False,
    ),
    "operating_expenses_bank": (
        "What are the total operating expenses? "
        "'Operating expenses (i)+(ii)+(iii)' TOTAL. Consolidated, Crores.",
        False,
    ),
    "operating_profit_bank": (
        "What is the operating profit before provisions? "
        "'Operating profit before provisions and contingencies'. Consolidated, Crores.",
        False,
    ),
    "provisions_bank": (
        "What are the provisions and contingencies? "
        "'Provisions (other than tax) and contingencies'. Consolidated, Crores.",
        False,
    ),
    "profit_before_tax": (
        "What is the profit before tax? "
        "'Profit from ordinary activities before tax and minority interest'. Consolidated, Crores.",
        False,
    ),
    "tax": (
        "What is the total tax expense? "
        "'Tax expense (Refer note...)'. Consolidated, Crores.",
        False,
    ),
    "pat": (
        "What is the net profit BEFORE deducting minority interest? "
        "For banks: 'Net profit from ordinary activities after tax and before minority interest'. "
        "This is the row BEFORE the minority deduction. NOT (14)-(15). "
        "Consolidated current quarter, Crores.",
        False,
    ),
    "pat_minority": (
        "What is the net profit AFTER deducting minority interest? "
        "'Net profit for the period (14)-(15)' — the final bottom line. Consolidated, Crores.",
        False,
    ),
    "eps_basic": (
        "What is the basic EPS (not annualised)? "
        "'(a) Basic EPS before & after extraordinary items'. "
        "Return Rs per share — a small number like 20-30.",
        True,
    ),
    "eps_diluted": (
        "What is the diluted EPS (not annualised)? "
        "'(b) Diluted EPS before & after extraordinary items'. "
        "Return Rs per share — a small number like 20-30.",
        True,
    ),
    "npa_gross_cr": (
        "What is the gross NPA amount in Crores? "
        "'(a) Gross NPAs' — an absolute amount, NOT a percentage. "
        "Large number like 30000-50000.",
        False,
    ),
    "npa_net_cr": (
        "What is the net NPA amount in Crores? "
        "'(b) Net NPAs' — absolute amount NOT percentage. Crores.",
        False,
    ),
    "npa_pct_gross": (
        "What is the gross NPA percentage? "
        "'Percentage of gross NPAs to gross advances'. "
        "Small ratio like 1.5 to 4.0 percent.",
        True,
    ),
    "npa_pct_net": (
        "What is the net NPA percentage? "
        "'Percentage of net NPAs to net advances'. "
        "Very small ratio like 0.3 to 1.5 percent.",
        True,
    ),
    "car": (
        "What is the capital adequacy ratio (CAR or CRAR or CET1)? "
        "Typically 14-20 percent. NOT debt equity ratio. Return percent.",
        True,
    ),
    "equity_capital": (
        "What is the paid-up equity share capital? "
        "'Paid up equity share capital (Face value of Rs 1/- each)'. "
        "Small number like 500-1000 Crores.",
        False,
    ),
    "reserves_surplus": (
        "What are the reserves and surplus? "
        "'Reserves and surplus' or 'Reserves excluding revaluation reserves'. "
        "Large number. Crores.",
        False,
    ),
    "deposits_bank": (
        "What are the total customer deposits? "
        "'Deposits'. Very large number. Crores.",
        False,
    ),
    "borrowings_current": (
        "What are the total borrowings? 'Borrowings'. Crores.",
        False,
    ),
}

# Math consistency checks: result ≈ a - b  (or a + b for total_income)
MATH_CHECKS = [
    ("pat",               "profit_before_tax",       "tax",              "sub"),
    ("total_income",      "revenue",                 "other_income",     "add"),
    ("operating_profit_bank","total_income",          "interest_expended","sub"),
]

VERIFY_THRESHOLD = 0.05   # 5% diff = WARN, >10% = FAIL


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_path(symbol: str) -> Path:
    p = Path(CONFIG.storage.raw_dir) / "filings" / symbol.upper() / "mistral_cache.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _load_cache(symbol: str) -> dict:
    p = _cache_path(symbol)
    return json.load(open(p)) if p.exists() else {}

def _save_cache(symbol: str, cache: dict):
    with open(_cache_path(symbol), "w") as f:
        json.dump(cache, f, indent=2)
    log.info(f"  [CACHE] Saved → {_cache_path(symbol).name}")


# ── Page data helpers ──────────────────────────────────────────────────────────

def _build_page_text(page_results: dict, col_idx: int = 0) -> tuple:
    """
    Build a clean text representation of the PDF data for Mistral.
    Prioritises consolidated > standalone.
    Returns (page_text, all_values_set) where all_values_set is used for anti-hallucination.
    """
    PRIORITY = {"consolidated":0,"standalone":1,"balance_sheet":2,
                "segment":3,"cash_flow":4,"other":5}

    pages_by_type = {}
    all_values    = set()

    for page_num, page_data in sorted(page_results.items(), key=lambda x: int(x[0])):
        if not page_data.get("has_financial_table"):
            continue
        stype = page_data.get("statement_type","other")
        pages_by_type.setdefault(stype, []).append((int(page_num), page_data))

    lines = []
    for stype in ["consolidated","standalone","balance_sheet","cash_flow","other"]:
        pages = pages_by_type.get(stype, [])
        if not pages:
            continue
        lines.append(f"\n[{stype.upper()} STATEMENT]")
        for page_num, page_data in pages:
            data = page_data.get("data", {})
            for label, vals in data.items():
                val = None
                if isinstance(vals, list) and vals:
                    val = vals[col_idx] if col_idx < len(vals) else vals[0]
                elif vals is not None:
                    val = vals
                num = _clean_num(val)
                if num is not None:
                    all_values.add(round(num, 2))
                lines.append(f"  {label}: {val}")

    return "\n".join(lines), all_values


# ── Mistral API call ───────────────────────────────────────────────────────────

def _ask_mistral(page_text: str, field: str, question: str, is_ratio: bool) -> dict:
    """
    Ask Mistral one specific question about the financial data.
    Returns {value, label, confidence} or {value: None, error: str}

    THIS IS THE ONLY EXPENSIVE CALL — cached immediately after.
    """
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return {"value": None, "error": "MISTRAL_API_KEY not set"}

    unit_hint = (
        "Return as a plain number (e.g. 21.67 for EPS, 1.86 for NPA%)."
        if is_ratio else
        "Return as Crores (e.g. 81546.20). Do not include units or commas."
    )

    prompt = f"""Here is the financial data from an Indian company quarterly results PDF:

{page_text}

Question: {question}

{unit_hint}

Rules:
- The value MUST come from the data above — never invent numbers
- If you cannot find it with confidence, return null for value
- Return ONLY JSON, nothing else:

{{"value": 81546.20, "label": "exact row label from data above", "confidence": 0.95}}

or if not found:

{{"value": null, "label": null, "confidence": 0.0}}"""

    try:
        from mistralai import Mistral
        client = Mistral(api_key=api_key)

        log.info(f"  [MISTRAL] Calling for field: {field}")
        t0   = time.time()
        resp = client.chat.complete(
            model    = MISTRAL_MODEL,
            messages = [{"role": "user", "content": prompt}],
        )
        elapsed = time.time() - t0
        text    = resp.choices[0].message.content or ""
        log.info(f"  [MISTRAL] Response in {elapsed:.1f}s for {field}")

        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if not m:
            return {"value": None, "error": "no JSON in response", "raw": text[:200]}

        parsed = json.loads(m.group(0))
        val    = _clean_num(parsed.get("value"))
        return {
            "value":      val,
            "label":      str(parsed.get("label") or ""),
            "confidence": float(parsed.get("confidence") or 0.0),
        }

    except Exception as e:
        log.error(f"  [MISTRAL] Error for {field}: {e}")
        return {"value": None, "error": str(e)}


# ── Anti-hallucination check ───────────────────────────────────────────────────

def _value_exists_in_data(value: float, all_values: set, tol: float = 0.5) -> bool:
    """Check if value exists verbatim in the raw page data."""
    if value is None:
        return False
    return any(abs(value - v) <= tol for v in all_values)


# ── Math checks ────────────────────────────────────────────────────────────────

def _math_verify(fields: dict) -> list:
    results = []
    for result_f, a_f, b_f, op in MATH_CHECKS:
        rv = fields.get(result_f)
        av = fields.get(a_f)
        bv = fields.get(b_f)
        if rv is None or av is None or bv is None:
            continue
        expected = av + bv if op == "add" else av - bv
        diff     = abs(rv - expected) / (abs(expected) + 1e-9)
        status   = "PASS" if diff <= 0.03 else "WARN" if diff <= 0.08 else "FAIL"
        results.append({
            "field":    result_f,
            "status":   status,
            "expected": round(expected, 2),
            "got":      round(rv, 2),
            "diff_pct": round(diff * 100, 2),
        })
        log.info(f"  [MATH] {result_f}: {status} (expected {expected:.2f} got {rv:.2f})")
    return results


# ── Screener cross-check ───────────────────────────────────────────────────────

def _screener_fetch(symbol: str, quarter_label: str) -> dict:
    import requests, re as _re
    m = _re.match(r"(Q[1-4])_FY(\d{4})", quarter_label)
    if not m:
        return {}
    q, fy = m.group(1), int(m.group(2))
    month, year = {"Q1":("Jun",fy-1),"Q2":("Sep",fy-1),"Q3":("Dec",fy-1),"Q4":("Mar",fy)}[q]
    target = f"{month} {year}"

    try:
        r = requests.get(
            f"https://www.screener.in/company/{symbol}/consolidated/",
            headers={"User-Agent":"Mozilla/5.0"},
            timeout=12,
        )
        if r.status_code != 200:
            return {}
        idx = r.text.find('id="quarters"')
        if idx == -1:
            return {}
        section = r.text[idx: r.text.find('id="profit-loss"', idx)]
        headers = _re.findall(r'data-date-key="[^"]+"\s*>\s*\n\s*([\w ]+)\n', section)
        if target not in headers:
            log.info(f"  [SCREENER] {quarter_label} ({target}) not found in columns: {headers[:6]}")
            return {}
        ci = headers.index(target)
        result, metric_map = {}, {
            "sales":"revenue","net revenue":"revenue",
            "net profit":"pat","profit after tax":"pat","eps":"eps_basic",
        }
        for lh, ch in _re.findall(r'<tr[^>]*>\s*<td[^>]*>(.*?)</td>(.*?)</tr>', section, _re.DOTALL):
            lb = _re.sub(r"<[^>]+>","",lh).strip().lower()
            for key, field in metric_map.items():
                if key in lb and field not in result:
                    cells = _re.findall(r'<td[^>]*>(.*?)</td>', ch, _re.DOTALL)
                    vals  = []
                    for c in cells:
                        try: vals.append(float(_re.sub(r"<[^>]+>","",c).strip().replace(",","")))
                        except: vals.append(None)
                    if ci < len(vals) and vals[ci] is not None:
                        result[field] = vals[ci]
        log.info(f"  [SCREENER] {symbol} {quarter_label}: {result}")
        return result
    except Exception as e:
        log.warning(f"  [SCREENER] {e}")
        return {}


# ── Verdict ────────────────────────────────────────────────────────────────────

def _compute_verdict(
    fields:       dict,
    math_results: list,
    xbrl_row:     dict,
) -> tuple:
    """Returns (score 0-100, verdict GOOD/WARN/FAIL, issues list)"""
    issues = []
    passes = 0
    total  = 0

    # Math
    for r in math_results:
        total += 1
        if r["status"] == "PASS":
            passes += 1
        elif r["status"] == "WARN":
            passes += 0.5
            issues.append(f"[MATH WARN] {r['field']}: expected {r['expected']} got {r['got']} ({r['diff_pct']:.1f}%)")
        else:
            issues.append(f"[MATH FAIL] {r['field']}: expected {r['expected']} got {r['got']} ({r['diff_pct']:.1f}%)")

    # XBRL
    if xbrl_row:
        for f in ["revenue","pat","eps_basic","eps_diluted","equity_capital",
                  "interest_expended","npa_gross_cr","npa_net_cr","deposits_bank"]:
            xv = xbrl_row.get(f)
            pv = fields.get(f)
            if xv is None or pv is None or str(xv) == "nan":
                continue
            total += 1
            diff = abs(pv - float(xv)) / (abs(float(xv)) + 1e-9)
            if diff <= 0.01:
                passes += 1
            elif diff <= 0.05:
                passes += 0.5
                issues.append(f"[XBRL WARN] {f}: pdf={pv:.2f} xbrl={float(xv):.2f} ({diff*100:.1f}%)")
            else:
                issues.append(f"[XBRL FAIL] {f}: pdf={pv:.2f} xbrl={float(xv):.2f} ({diff*100:.1f}%)")

    score   = int(passes / total * 100) if total > 0 else 50
    verdict = "GOOD" if score >= 85 else "WARN" if score >= 60 else "FAIL"
    return score, verdict, issues


# ── Main extraction function ───────────────────────────────────────────────────

def extract_pdf_fields(
    symbol:        str,
    pdf_stem:      str,
    page_results:  dict,
    quarter_label: str  = "",
    col_idx:       int  = 0,
    xbrl_row:      dict = None,
    force:         bool = False,
    verbose:       bool = True,
    log_cb=None,          # callback(line: str) — called after every log event
) -> dict:
    """
    Extract all financial fields from a PDF using Mistral.

    Args:
        log_cb : optional callable(str) — called in real-time for every log line.
                 Use this to stream logs to a WebSocket or UI.
                 Example: log_cb=lambda line: ws_queue.put(line)
    """
    def _log(line: str):
        log.info(line)
        if log_cb:
            try: log_cb(line)
            except Exception: pass

    if verbose:
        print(f"\n{'='*60}")
        print(f"  Extracting: {symbol} / {pdf_stem}")
        print(f"{'='*60}")

    # ── Build page text (free, logged) ────────────────────────────────────────
    n_pages   = sum(1 for p in page_results.values() if p.get("has_financial_table"))
    _log(f"  [PDF] {symbol}/{pdf_stem} — {n_pages} pages with tables")
    page_text, all_values = _build_page_text(page_results, col_idx=col_idx)
    _log(f"  [PDF] {len(all_values)} unique values extracted from pages")

    # ── Load cache ────────────────────────────────────────────────────────────
    cache     = _load_cache(symbol)
    pdf_cache = cache.get(pdf_stem, {})
    fields    = {}
    labels    = {}
    sources   = {}

    n_total   = len(FIELDS)
    n_cached  = 0
    n_mistral = 0
    n_failed  = 0

    # ── Extract each field ────────────────────────────────────────────────────
    for i, (field, (question, is_ratio)) in enumerate(FIELDS.items(), 1):
        prefix = f"  [{i:2d}/{n_total}]"

        # Check cache first (free)
        if not force and field in pdf_cache:
            cached = pdf_cache[field]
            val    = cached.get("value")
            if val is not None:
                fields[field]  = val
                labels[field]  = cached.get("label", "")
                sources[field] = "cache"
                n_cached += 1
                _log(f"{prefix} [CACHE] {field:<28} = {val}")
                continue

        # Call Mistral (expensive — cache result immediately)
        _log(f"{prefix} [MISTRAL→] {field:<28} asking...")
        time.sleep(RATE_LIMIT_DELAY)
        result = _ask_mistral(page_text, field, question, is_ratio)
        val    = result.get("value")
        error  = result.get("error")

        if error:
            _log(f"{prefix} [FAIL] {field}: {error}")
            n_failed += 1
            pdf_cache[field] = {"value": None, "label": None,
                                "confidence": 0.0, "error": error}
            cache[pdf_stem] = pdf_cache
            _save_cache(symbol, cache)
            continue

        # Anti-hallucination: value must exist in raw data
        if val is not None and not _value_exists_in_data(val, all_values):
            _log(f"{prefix} [HALLUCINATION] {field}: {val} not in raw data — discarded")
            val = None
            n_failed += 1

        # Store result
        lbl  = result.get("label","")
        conf = result.get("confidence", 0.0)
        pdf_cache[field] = {"value": val, "label": lbl, "confidence": conf}
        cache[pdf_stem]  = pdf_cache
        _save_cache(symbol, cache)   # ← cache immediately after every Mistral call

        if val is not None:
            fields[field]  = val
            labels[field]  = lbl
            sources[field] = "mistral"
            n_mistral += 1
            _log(f"{prefix} [MISTRAL←] {field:<28} = {val}  '{lbl[:40]}'")
        else:
            n_failed += 1
            _log(f"{prefix} [NULL]     {field:<28} Mistral returned null")

    # ── Math checks ───────────────────────────────────────────────────────────
    _log(f"  [MATH] Running consistency checks...")
    math_results = _math_verify(fields)
    for mc in math_results:
        s = "✓" if mc["status"]=="PASS" else "⚠" if mc["status"]=="WARN" else "✗"
        _log(f"  [MATH] {s} {mc['field']:<25} exp={mc['expected']:.2f} got={mc['got']:.2f} ({mc['diff_pct']:.1f}%)")

    # ── Verdict ───────────────────────────────────────────────────────────────
    score, verdict, issues = _compute_verdict(fields, math_results, xbrl_row)

    # ── Print summary ─────────────────────────────────────────────────────────
    if verbose:
        print(f"\n  ── Results ──────────────────────────────────────────")
        print(f"  {'Field':<30} {'Value':>15} {'Source':<10} {'Label'}")
        print(f"  {'─'*85}")
        for f, v in sorted(fields.items()):
            src = sources.get(f, "?")
            lbl = (labels.get(f) or "")[:40]
            print(f"  {f:<30} {v:>15.4f} {src:<10} {lbl}")

        print(f"\n  ── Stats ────────────────────────────────────────────")
        print(f"  From cache  : {n_cached}")
        print(f"  From Mistral: {n_mistral}")
        print(f"  Failed      : {n_failed}")

        print(f"\n  ── Verification ─────────────────────────────────────")
        for r in math_results:
            sym = "✓" if r["status"]=="PASS" else "⚠" if r["status"]=="WARN" else "✗"
            print(f"  {sym} [MATH] {r['field']:<25} expected={r['expected']:.2f} got={r['got']:.2f} ({r['diff_pct']:.1f}%)")
        if xbrl_row:
            for f in ["revenue","pat","eps_basic","eps_diluted","equity_capital",
                      "interest_expended","npa_gross_cr","npa_net_cr","deposits_bank"]:
                xv = xbrl_row.get(f)
                pv = fields.get(f)
                if xv and pv and str(xv) != "nan":
                    diff = abs(pv-float(xv))/(abs(float(xv))+1e-9)*100
                    sym  = "✓" if diff < 1 else "⚠" if diff < 5 else "✗"
                    print(f"  {sym} [XBRL] {f:<28} pdf={pv:.4f}  xbrl={float(xv):.4f}  ({diff:.1f}%)")

        if issues:
            print(f"\n  ── Issues ───────────────────────────────────────────")
            for issue in issues:
                print(f"  ! {issue}")

        print(f"\n  Verdict: {verdict}  (score={score})")

    return {
        "fields":   fields,
        "labels":   labels,
        "sources":  sources,
        "math":     math_results,
        "score":    score,
        "verdict":  verdict,
        "issues":   issues,
    }


# ── Symbol-level runner ────────────────────────────────────────────────────────

def extract_symbol_pdfs(
    symbol:          str,
    raw_extractions: dict,
    db:              object = None,
    force:           bool   = False,
    verbose:         bool   = True,
) -> dict:
    """
    Extract all PDFs for a symbol from raw_extractions.json.
    Skips PDFs whose quarter is already fully covered in XBRL DB.

    Args:
        symbol          : NSE ticker
        raw_extractions : loaded raw_extractions.json
        db              : fundamentals DataFrame for XBRL lookup
        use_screener    : Screener.in cross-check
        force           : re-extract even if cached

    Returns:
        {pdf_stem: extract_result}
    """
    results = {}

    for pdf_stem, raw in raw_extractions.items():
        page_results = raw.get("page_results", {})
        if not page_results:
            log.info(f"  [SKIP] {pdf_stem}: no page_results")
            continue

        # Infer quarter_label from pdf_stem
        m = re.search(r"(Q[1-4])_FY(\d{4})", pdf_stem)
        quarter_label = f"{m.group(1)}_FY{m.group(2)}" if m else ""

        # Get XBRL row if available
        xbrl_row = None
        if db is not None and quarter_label:
            rows = db[(db["symbol"]==symbol) & (db["quarter_label"]==quarter_label)]
            if not rows.empty:
                xbrl_row = rows.iloc[0].to_dict()
                log.info(f"  [XBRL] Found reference data for {quarter_label}")

        if verbose:
            print(f"\n{'─'*60}")
            print(f"  {pdf_stem}  ({quarter_label or 'quarter unknown'})")
            if xbrl_row:
                print(f"  XBRL available: revenue={xbrl_row.get('revenue')} pat={xbrl_row.get('pat')}")

        results[pdf_stem] = extract_pdf_fields(
            symbol        = symbol,
            pdf_stem      = pdf_stem,
            page_results  = page_results,
            quarter_label = quarter_label,
            xbrl_row      = xbrl_row,
            force         = force,
            verbose       = verbose,
        )

    return results