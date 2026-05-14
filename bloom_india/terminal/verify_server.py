"""
bloom_india/terminal/verify_server.py
=======================================
FastAPI verification server — PDF × XBRL comparison.

WebSocket streams live progress to the browser.
Background tasks run independently of client connections.

Run:
    uvicorn bloom_india.terminal.verify_server:app --port 8502 --reload

Or via helper:
    python -m bloom_india.terminal.verify_server
"""

import os, re, json, time, asyncio, threading, logging
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── Global Mistral rate limiter (shared across ALL threads) ───────────────────
_mistral_lock       = threading.Lock()
_mistral_last_call  = 0.0
MISTRAL_MIN_GAP     = 1.8   # minimum seconds between ANY Mistral call

# ── Deduplication: prevent same symbol running twice ──────────────────────────
_running_syms: set  = set()
_running_lock       = threading.Lock()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

os.environ.setdefault(
    "BLOOM_INDIA_CONFIG",
    str(Path(__file__).parents[2] / "config.yaml"),
)

# Load API keys from environment — uvicorn doesn't inherit shell exports
# Add your keys to a .env file at the repo root or set them here
_env_file = Path(__file__).parents[2] / ".env"
if _env_file.exists():
    for line in open(_env_file).read().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from bloom_india.config import CONFIG
from bloom_india.data.process.pdf_extract import extract_symbol
from bloom_india.data.process.pdf_mistral_extractor import extract_pdf_fields, FIELDS

import pandas as pd

log = logging.getLogger("verify")

# File handler so Jupyter can tail logs
_log_file = Path(CONFIG.storage.base_dir) / "verify_server.log"
_fh = logging.FileHandler(str(_log_file), mode="a")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), _fh])

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="bloom_india · Verify")

# ── State ──────────────────────────────────────────────────────────────────────
STATE = {
    "running":  False,
    "progress": 0,
    "total":    0,
    "current":  "",
    "results":  {},   # sym → result dict
    "logs":     {},   # sym → list[str]  (in-memory, also saved to disk)
}

LOG_DIR = Path(CONFIG.storage.base_dir) / "verify_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

# ── Helpers ───────────────────────────────────────────────────────────────────
def _qkey(ql):
    m = re.match(r"Q([1-4])_FY(\d{4})", ql or "")
    return int(m.group(2))*4+int(m.group(1)) if m else 0

def _latest_xbrl(db, sym):
    rows = db[db["symbol"]==sym]["quarter_label"].dropna().unique().tolist()
    return max(rows, key=_qkey) if rows else ""

def _latest_pdf(sym, db=None):
    """
    Find best PDF for verification:
    - If db provided: prefer latest PDF whose quarter exists in XBRL (best for verify)
    - Fallback: absolute latest PDF (NEW DATA mode)
    """
    d = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()
    if not d.exists(): return None, None
    pdfs = list(d.glob("*.pdf"))
    if not pdfs: return None, None

    def _key(p):
        m = re.search(r"(Q[1-4])_FY(\d{4})", p.stem)
        return _qkey(f"{m.group(1)}_FY{m.group(2)}") if m else 0

    def _ql(p):
        m = re.search(r"(Q[1-4])_FY(\d{4})", p.stem)
        return f"{m.group(1)}_FY{m.group(2)}" if m else ""

    # Prefer latest PDF with XBRL coverage
    if db is not None:
        xbrl_qs = set(db[db["symbol"]==sym]["quarter_label"].dropna().tolist())
        covered = [p for p in pdfs if _ql(p) in xbrl_qs]
        if covered:
            p = max(covered, key=_key)
            return p.stem, _ql(p)

    # Fallback: absolute latest
    p = max(pdfs, key=_key)
    return p.stem, _ql(p)

def _save_log(sym, lines):
    with open(LOG_DIR/f"{sym}.log","w") as f:
        f.write(f"# {sym} — {datetime.now().isoformat()}\n\n")
        f.write("\n".join(lines))

def _load_log(sym):
    p = LOG_DIR/f"{sym}.log"
    return open(p).read().splitlines() if p.exists() else []

def _clean_result(r: dict) -> dict:
    """Make result JSON-serialisable."""
    def _c(v):
        if isinstance(v, float):  return round(v, 6)
        if isinstance(v, dict):   return {k2: _c(v2) for k2,v2 in v.items()}
        if isinstance(v, list):   return [_c(x) for x in v]
        if hasattr(v, "item"):    return v.item()
        return v
    return _c(r)

def _broadcast_log(sym: str, line: str, loop):
    """Thread-safe WebSocket broadcast of a single log line."""
    log.info(f"[{sym}] {line.strip()}")
    # Append to in-memory log
    STATE["logs"].setdefault(sym, []).append(line)
    # Write to disk immediately so LOGS tab can read it live
    _save_log(sym, STATE["logs"][sym])
    if loop:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type":"log","symbol":sym,"line":line}),
            loop,
        )

# ── Core verify (runs in thread) ───────────────────────────────────────────────
def verify_one_sync(sym: str, db: pd.DataFrame,
                    force_vision:  bool = False,
                    force_extract: bool = False,
                    loop=None,
                    vision_model:  str = "pixtral-12b-2409",
                    extract_model: str = "mistral-small-latest") -> dict:
    log_lines = []
    ts = datetime.now().strftime("%H:%M:%S")
    result = dict(symbol=sym, xbrl_latest="", pdf_latest="", status="UNKNOWN",
                  verdict="", score=0, fields={}, labels={}, math=[], issues=[],
                  is_new=False, ts=ts)
    log_lines.append(f"[{ts}] ══ {sym} ══")

    xbrl_ql = _latest_xbrl(db, sym)
    result["xbrl_latest"] = xbrl_ql
    if not xbrl_ql:
        log_lines.append("  [WARN] No XBRL data")
        result["status"] = "NO_XBRL"
        STATE["logs"][sym] = log_lines; _save_log(sym, log_lines)
        return result

    log_lines.append(f"  [XBRL] Latest: {xbrl_ql}")
    pdf_stem, pdf_ql = _latest_pdf(sym, db=db)
    result["pdf_latest"] = pdf_ql or ""

    if not pdf_stem:
        log_lines.append("  [DOWNLOAD] No PDFs found — downloading from BSE...")
        _broadcast_log(sym, log_lines[-1], loop)
        try:
            from bloom_india.data.fetch.filings import download_symbol
            # Build XBRL coverage so we only download what's needed
            xbrl_covered = set(db[db["symbol"]==sym]["quarter_label"].dropna().tolist())
            dl_result    = download_symbol(
                sym,
                from_fy      = 2024,
                force        = False,
                verbose      = False,
                xbrl_covered = xbrl_covered,
            )
            n_dl = dl_result.get("downloaded", 0)
            if n_dl:
                log_lines.append(f"  [DOWNLOAD] ✓ {n_dl} PDFs downloaded")
                _broadcast_log(sym, log_lines[-1], loop)
                # Re-check
                pdf_stem, pdf_ql = _latest_pdf(sym, db=db)
                result["pdf_latest"] = pdf_ql or ""
            else:
                gaps    = dl_result.get("gaps", [])
                missing = dl_result.get("missing", [])
                log_lines.append(f"  [DOWNLOAD] Nothing downloaded — gaps={gaps} missing={missing}")
                _broadcast_log(sym, log_lines[-1], loop)
        except Exception as e:
            log_lines.append(f"  [DOWNLOAD] Error: {e}")
            _broadcast_log(sym, log_lines[-1], loop)

    if not pdf_stem:
        log_lines.append("  [SKIP] No PDFs available after download attempt")
        result["status"] = "NO_PDF"
        STATE["logs"][sym] = log_lines; _save_log(sym, log_lines)
        return result

    log_lines.append(f"  [PDF]  Latest: {pdf_stem} ({pdf_ql})")

    # force_extract: clear mistral cache so all fields re-run
    if force_extract and pdf_stem:
        slug    = re.sub(r"[^\w\-]", "_", extract_model)
        cache_p = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()/f"mistral_cache_{slug}.json"
        if cache_p.exists():
            try:
                mc = json.load(open(cache_p))
                if pdf_stem in mc:
                    del mc[pdf_stem]
                    json.dump(mc, open(cache_p,"w"), indent=2)
                    msg = f"  [FORCE] Cleared mistral cache for {pdf_stem}"
                    log_lines.append(msg); _broadcast_log(sym, msg, loop)
            except Exception as e:
                log.warning(f"  [FORCE] Cache clear error: {e}")

    is_new = _qkey(pdf_ql) > _qkey(xbrl_ql)
    result["is_new"] = is_new
    log_lines.append(f"  [MODE] {'NEW DATA' if is_new else 'VERIFY'} — "
                     f"PDF={pdf_ql} {'>' if is_new else '=='} XBRL={xbrl_ql}")
    _broadcast_log(sym, log_lines[-1], loop)

    xbrl_row = None
    if not is_new and pdf_ql:
        rows = db[(db["symbol"]==sym)&(db["quarter_label"]==pdf_ql)]
        if not rows.empty:
            xbrl_row = rows.iloc[0].to_dict()
            log_lines.append(f"  [XBRL] rev={xbrl_row.get('revenue')} "
                             f"pat={xbrl_row.get('pat')} eps={xbrl_row.get('eps_basic')}")
        else:
            log_lines.append(f"  [WARN] No XBRL row for {pdf_ql}")

    try:
        raw_path = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()/"raw_extractions.json"
        all_raw = {}
        if raw_path.exists():
            with open(raw_path) as f: all_raw = json.load(f)

        if not force_vision and pdf_stem in all_raw and all_raw[pdf_stem].get("page_results"):
            # Check extractor matches — if switching from pixtral to docling, re-extract
            cached_extractor = all_raw[pdf_stem].get("extractor", "pixtral")
            req_extractor    = ("docling" if vision_model=="docling"
                                else "gemini" if vision_model.startswith("gemini")
                                else "pixtral")
            if cached_extractor == req_extractor:
                log_lines.append(f"  [CACHE] {pdf_stem} already extracted ({cached_extractor})")
                _broadcast_log(sym, log_lines[-1], loop)
                page_results = all_raw[pdf_stem].get("page_results", {})
            else:
                log_lines.append(f"  [SWITCH] Extractor changed {cached_extractor}→{req_extractor}, re-extracting...")
                _broadcast_log(sym, log_lines[-1], loop)
                page_results = None  # fall through to extraction
        else:
            page_results = None  # needs extraction

        if page_results is None:
            if vision_model == "docling":
                log_lines.append(f"  [DOCLING] Extracting {pdf_stem} locally...")
                _broadcast_log(sym, log_lines[-1], loop)
                try:
                    from bloom_india.data.process.pdf_docling_extractor import extract_pdf_docling
                    def _docling_cb(line): _broadcast_log(sym, line, loop)
                    pdf_path_obj = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()/f"{pdf_stem}.pdf"
                    raw = extract_pdf_docling(
                        str(pdf_path_obj), symbol=sym,
                        force=force_vision, log_cb=_docling_cb,
                    )
                    page_results = raw.get("page_results", {})
                    raw["extractor"] = "docling"
                except Exception as e:
                    log_lines.append(f"  [DOCLING] Error: {e} — falling back to Pixtral")
                    _broadcast_log(sym, log_lines[-1], loop)
                    page_results = {}

            elif vision_model.startswith("gemini"):
                log_lines.append(f"  [GEMINI] Extracting {pdf_stem} with {vision_model}...")
                _broadcast_log(sym, log_lines[-1], loop)
                try:
                    from bloom_india.data.process.pdf_gemini_extractor import extract_pdf_gemini
                    def _gemini_cb(line): _broadcast_log(sym, line, loop)
                    pdf_path_obj = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()/f"{pdf_stem}.pdf"
                    raw = extract_pdf_gemini(
                        str(pdf_path_obj), symbol=sym,
                        model=vision_model, force=force_vision,
                        log_cb=_gemini_cb,
                    )
                    page_results = raw.get("page_results", {})
                except Exception as e:
                    log_lines.append(f"  [GEMINI] Error: {e}")
                    _broadcast_log(sym, log_lines[-1], loop)
                    page_results = {}
            else:
                log_lines.append(f"  [VISION] Running {vision_model} on {pdf_stem}...")
                _broadcast_log(sym, log_lines[-1], loop)
                def _vision_cb(line): _broadcast_log(sym, line, loop)
                pdf_path_obj = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()/f"{pdf_stem}.pdf"
                from bloom_india.data.process.pdf_extract import extract_pdf
                raw = extract_pdf(
                    str(pdf_path_obj), symbol=sym,
                    force=force_vision, verbose=False, log_cb=_vision_cb,
                )
                raw["extractor"] = "pixtral"
                page_results = raw.get("page_results", {})

            # Save to raw_extractions.json
            raw_path2 = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()/"raw_extractions.json"
            all_raw2  = {}
            if raw_path2.exists():
                with open(raw_path2) as f: all_raw2 = json.load(f)
            all_raw2[pdf_stem] = raw
            with open(raw_path2, "w") as f: json.dump(all_raw2, f)

            log_lines.append(f"  [VISION] Done — {len(page_results)} pages")
            _broadcast_log(sym, log_lines[-1], loop)

    except Exception as e:
        log.error(f"  [ERROR] Extraction {sym}: {e}", exc_info=True)
        log_lines.append(f"  [ERROR] Extraction: {e}")
        _broadcast_log(sym, log_lines[-1], loop)
        result["status"] = "ERROR"
        STATE["logs"][sym] = log_lines; _save_log(sym, log_lines)
        return result

    if not page_results:
        log_lines.append("  [ERROR] No page_results after extraction")
        _broadcast_log(sym, log_lines[-1], loop)
        result["status"] = "ERROR"
        STATE["logs"][sym] = log_lines; _save_log(sym, log_lines)
        return result

    n_tables = sum(1 for p in page_results.values() if p.get("has_financial_table"))
    log_lines.append(f"  [PDF]  {len(page_results)} pages, {n_tables} with tables")
    _broadcast_log(sym, log_lines[-1], loop)

    # ── Detect reporting units (Lakhs vs Crores) ──────────────────────────────
    unit_scale = 1.0
    for page_data in page_results.values():
        raw_text = str(page_data).lower()
        if any(x in raw_text for x in ["in lakhs","rs. lakhs","rs lakhs","₹ lakhs","lakh"]):
            unit_scale = 0.01
            msg = f"  [UNITS] Lakhs detected — scaling ÷100 to Crores"
            log_lines.append(msg); _broadcast_log(sym, msg, loop)
            break
        elif any(x in raw_text for x in ["in millions","rs. millions","usd million"]):
            unit_scale = 0.1
            msg = f"  [UNITS] Millions detected — scaling ×0.1 to Crores"
            log_lines.append(msg); _broadcast_log(sym, msg, loop)
            break

    _broadcast_log(sym, f"  [MODEL] vision={vision_model}  extract={extract_model}", loop)

    # If unit_scale != 1.0, clear any existing cache since old values are unscaled
    if unit_scale != 1.0:
        slug    = re.sub(r"[^\w\-]", "_", extract_model)
        cache_p = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()/f"mistral_cache_{slug}.json"
        if cache_p.exists():
            try:
                mc = json.load(open(cache_p))
                if pdf_stem in mc:
                    # Check if cached values look unscaled (too large for unit_scale=0.01)
                    sample_val = next(
                        (v.get("value") for v in mc[pdf_stem].values()
                         if isinstance(v, dict) and v.get("value") and v["value"] > 100),
                        None
                    )
                    if sample_val and sample_val > 1000:
                        del mc[pdf_stem]
                        json.dump(mc, open(cache_p,"w"), indent=2)
                        msg = f"  [UNITS] Cleared unscaled cache for {pdf_stem} (values were in Lakhs)"
                        log_lines.append(msg); _broadcast_log(sym, msg, loop)
            except Exception as e:
                log.warning(f"  [UNITS] Cache check error: {e}")

    log.info(f"  Starting field extraction for {sym}/{pdf_stem}")
    try:
        _loop = loop
        _sym  = sym

        def _field_log_cb(line: str):
            _broadcast_log(_sym, line, _loop)

        _field_log_cb(f"  [START] Field extraction — {len(FIELDS)} fields  model={extract_model}  unit_scale={unit_scale}")

        log.info(f"  Calling extract_pdf_fields for {sym}/{pdf_stem} force_extract={force_extract}")
        ext = extract_pdf_fields(
            symbol=sym, pdf_stem=pdf_stem, page_results=page_results,
            quarter_label=pdf_ql or "", xbrl_row=xbrl_row,
            force=force_extract, verbose=False, log_cb=_field_log_cb,
            model=extract_model, unit_scale=unit_scale,
        )
        log.info(f"  extract_pdf_fields done for {sym}: {len(ext.get('fields',{}))} fields")
    except Exception as e:
        log.error(f"  [ERROR] Field extraction {sym}: {e}", exc_info=True)
        log_lines.append(f"  [ERROR] Field extraction: {e}")
        _broadcast_log(sym, log_lines[-1], loop)
        result["status"] = "ERROR"
        STATE["logs"][sym] = log_lines; _save_log(sym, log_lines)
        return result

    result.update(fields=ext["fields"], labels=ext.get("labels",{}),
                  math=ext["math"], verdict=ext["verdict"],
                  score=ext["score"], issues=ext["issues"])

    log_lines.append(f"  [RESULT] {len(ext['fields'])} fields | "
                     f"verdict={ext['verdict']} score={ext['score']}")

    log_lines.append("  [MATH]")
    for mc in ext.get("math",[]):
        s = "✓" if mc["status"]=="PASS" else "⚠" if mc["status"]=="WARN" else "✗"
        log_lines.append(f"    {s} {mc['field']:<28} "
                         f"exp={mc['expected']:.2f} got={mc['got']:.2f} ({mc['diff_pct']:.1f}%)")

    if xbrl_row:
        log_lines.append("  [XBRL COMPARE]")
        for field in ["revenue","pat","eps_basic","eps_diluted","interest_expended",
                      "equity_capital","npa_gross_cr","npa_net_cr","deposits_bank"]:
            pv = ext["fields"].get(field)
            if pv is None:
                log_lines.append(f"    — {field:<28} not extracted"); continue
            xv_raw = xbrl_row.get(field)
            if xv_raw is None or str(xv_raw)=="nan":
                log_lines.append(f"    · {field:<28} pdf={pv:.4f} xbrl=N/A"); continue
            xv   = float(xv_raw)
            diff = abs(pv-xv)/(abs(xv)+1e-9)*100
            s    = "✓" if diff<1 else "⚠" if diff<5 else "✗"
            log_lines.append(f"    {s} {field:<28} pdf={pv:.4f} xbrl={xv:.4f} ({diff:.1f}%)")

    if ext["issues"]:
        log_lines.append("  [ISSUES]")
        for issue in ext["issues"]: log_lines.append(f"    ! {issue}")

    result["status"] = ("NEW" if is_new else
                        "GOOD" if ext["verdict"]=="GOOD" else
                        "WARN" if ext["verdict"]=="WARN" else "FAIL")
    log_lines.append(f"  [DONE] → {result['status']}")
    STATE["logs"][sym] = log_lines
    _save_log(sym, log_lines)
    return result

# ── Background run thread ─────────────────────────────────────────────────────
_db_cache: Optional[pd.DataFrame] = None

def _get_db() -> pd.DataFrame:
    global _db_cache
    if _db_cache is None:
        _db_cache = pd.read_parquet(str(CONFIG.storage.fundamental_db))
    return _db_cache

def _run_thread(syms: list, force_vision: bool, force_extract: bool,
                loop: asyncio.AbstractEventLoop,
                vision_model: str = "pixtral-12b-2409",
                extract_model: str = "mistral-small-latest"):
    db = _get_db()
    STATE["running"]  = True
    STATE["progress"] = 0
    STATE["total"]    = len(syms)
    STATE["current"]  = ""

    for i, sym in enumerate(syms):
        if not STATE["running"]:
            break

        # Skip if already running (dedup)
        with _running_lock:
            if sym in _running_syms:
                log.warning(f"  [SKIP] {sym} already running — skipped")
                continue
            _running_syms.add(sym)

        STATE["current"]  = sym
        STATE["progress"] = i

        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type":"progress","current":sym,
                               "progress":i,"total":len(syms)}),
            loop,
        )

        try:
            r = verify_one_sync(sym, db,
                                force_vision=force_vision,
                                force_extract=force_extract,
                                loop=loop,
                                vision_model=vision_model,
                                extract_model=extract_model)
        except Exception as e:
            r = dict(symbol=sym, status="ERROR", xbrl_latest="", pdf_latest="",
                     verdict="", score=0, fields={}, labels={}, math=[],
                     issues=[str(e)], is_new=False,
                     ts=datetime.now().strftime("%H:%M:%S"))
            STATE["logs"][sym] = [f"[ERROR] {e}"]
            _save_log(sym, [f"[ERROR] {e}"])

        STATE["results"][sym] = _clean_result(r)

        with _running_lock:
            _running_syms.discard(sym)

        # Broadcast result
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type":"result","symbol":sym,
                               "result":STATE["results"][sym]}),
            loop,
        )

    STATE["running"]  = False
    STATE["current"]  = ""
    STATE["progress"] = len(syms)

    asyncio.run_coroutine_threadsafe(
        manager.broadcast({"type":"done","total":len(syms)}),
        loop,
    )

# ── FastAPI routes ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "verify.html"
    return HTMLResponse(html_path.read_text())

@app.get("/api/state")
async def get_state():
    return JSONResponse({
        "running":  STATE["running"],
        "progress": STATE["progress"],
        "total":    STATE["total"],
        "current":  STATE["current"],
        "results":  STATE["results"],
    })

@app.get("/api/universe")
async def get_universe_api():
    p = Path(CONFIG.storage.base_dir)/"nifty500.json"
    if p.exists():
        data = json.load(open(p))
        syms = [s["symbol"] if isinstance(s,dict) else s for s in data]
    else:
        db = _get_db()
        syms = sorted(db["symbol"].unique().tolist())
    # Mark which have PDFs
    filings_root = Path(CONFIG.storage.raw_dir)/"filings"
    result = []
    for sym in syms:
        has_pdf = (filings_root/sym).exists() and bool(list((filings_root/sym).glob("*.pdf")))
        result.append({"symbol":sym, "has_pdf":has_pdf})
    return JSONResponse(result)

@app.post("/api/run")
async def start_run(body: dict):
    if STATE["running"]:
        return JSONResponse({"error":"already running"}, status_code=409)

    mode          = body.get("mode","single")
    sym           = body.get("symbol","")
    force_vision  = body.get("force_vision",  False)
    force_extract = body.get("force_extract", False)
    # legacy: if old 'force' key sent, apply to both
    if body.get("force", False):
        force_vision = force_extract = True
    vision_model  = body.get("vision_model",  "pixtral-12b-2409")
    extract_model = body.get("extract_model", "mistral-small-latest")

    filings_root = Path(CONFIG.storage.raw_dir)/"filings"
    db = _get_db()
    all_syms = sorted(db["symbol"].unique().tolist())

    if mode == "single" and sym:
        syms = [sym.upper()]
    elif mode == "pdfs":
        syms = [s for s in all_syms
                if (filings_root/s).exists() and list((filings_root/s).glob("*.pdf"))]
    else:
        syms = all_syms

    for s in syms:
        STATE["results"].pop(s, None)

    loop = asyncio.get_event_loop()
    t = threading.Thread(
        target=_run_thread,
        args=(syms, force_vision, force_extract, loop, vision_model, extract_model),
        daemon=True,
    )
    t.start()

    return JSONResponse({"started":True,"total":len(syms)})

@app.post("/api/stop")
async def stop_run():
    STATE["running"] = False
    return JSONResponse({"stopped":True})

@app.get("/api/raw_symbols")
async def get_raw_symbols():
    """List all symbols that have any vision cache."""
    filings_root = Path(CONFIG.storage.raw_dir)/"filings"
    syms = set()
    # Symbols with vision cache dirs
    for p in filings_root.glob("*/.cache_*"):
        if p.is_dir():
            syms.add(p.parent.name)
    # Also symbols with raw_extractions.json
    for p in filings_root.glob("*/raw_extractions.json"):
        syms.add(p.parent.name)
    return JSONResponse(sorted(syms))

@app.get("/api/raw/{symbol}")
async def get_raw_extraction(symbol: str, model: str = ""):
    """
    Return all vision extractions for a symbol, keyed by model name.
    Also includes mistral extraction caches keyed as 'extract:{model}'.
    Returns: {model_name: {pdf_stem: {page_num: page_data}}}
    """
    filings_dir = Path(CONFIG.storage.raw_dir)/"filings"/symbol.upper()
    if not filings_dir.exists():
        return JSONResponse({})

    result = {}

    # ── Vision model caches (.cache_{model}/) ──────────────────────────────
    for cache_dir in sorted(filings_dir.glob(".cache_*")):
        model_name = cache_dir.name.replace(".cache_", "").replace("_", "-")
        if model and model.replace("-","_") not in cache_dir.name:
            continue
        for pdf_dir in sorted(cache_dir.iterdir()):
            if not pdf_dir.is_dir(): continue
            pdf_stem = pdf_dir.name
            pages    = {}
            for pf in sorted(pdf_dir.glob("page_*.json")):
                try:
                    pd_data  = json.load(open(pf))
                    page_num = str(int(pf.stem.replace("page_","")))
                    pages[page_num] = pd_data
                except Exception: continue
            if pages:
                result.setdefault(model_name, {})[pdf_stem] = pages

    # ── Extraction model caches (mistral_cache_{model}.json) ───────────────
    for cache_file in sorted(filings_dir.glob("mistral_cache_*.json")):
        model_slug = cache_file.stem.replace("mistral_cache_","").replace("_","-")
        key        = f"extract:{model_slug}"
        if model and model not in key: continue
        try:
            data = json.load(open(cache_file))
            # data = {pdf_stem: {field: {value, label, confidence}}}
            for pdf_stem, fields in data.items():
                # Convert to page-like format for display
                result.setdefault(key, {})[pdf_stem] = {
                    "fields": fields,
                    "_type":  "extraction_cache",
                    "_model": model_slug,
                }
        except Exception: continue

    return JSONResponse(result)

@app.get("/api/log/{symbol}")
async def get_log(symbol: str):
    lines = STATE["logs"].get(symbol.upper()) or _load_log(symbol.upper())
    return JSONResponse({"symbol":symbol.upper(),"lines":lines})

@app.get("/api/logs")
async def list_logs():
    saved = sorted([p.stem for p in LOG_DIR.glob("*.log")])
    return JSONResponse(saved)

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send current state immediately on connect
    await ws.send_json({
        "type":     "state",
        "running":  STATE["running"],
        "progress": STATE["progress"],
        "total":    STATE["total"],
        "current":  STATE["current"],
        "results":  STATE["results"],
    })
    try:
        while True:
            await asyncio.sleep(30)  # keep-alive
    except WebSocketDisconnect:
        manager.disconnect(ws)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bloom_india.terminal.verify_server:app",
                host="0.0.0.0", port=8502, reload=False, log_level="info")