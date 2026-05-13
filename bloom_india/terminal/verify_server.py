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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

os.environ.setdefault(
    "BLOOM_INDIA_CONFIG",
    str(Path(__file__).parents[2] / "config.yaml"),
)

from bloom_india.config import CONFIG
from bloom_india.data.process.pdf_extract import extract_symbol
from bloom_india.data.process.pdf_mistral_extractor import extract_pdf_fields

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

def _latest_pdf(sym):
    d = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()
    if not d.exists(): return None, None
    pdfs = list(d.glob("*.pdf"))
    if not pdfs: return None, None
    def key(p):
        m = re.search(r"(Q[1-4])_FY(\d{4})", p.stem)
        return _qkey(f"{m.group(1)}_FY{m.group(2)}") if m else 0
    p = max(pdfs, key=key)
    m = re.search(r"(Q[1-4])_FY(\d{4})", p.stem)
    return p.stem, (f"{m.group(1)}_FY{m.group(2)}" if m else "")

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
    if loop:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type":"log","symbol":sym,"line":line}),
            loop,
        )

# ── Core verify (runs in thread) ───────────────────────────────────────────────
def verify_one_sync(sym: str, db: pd.DataFrame,
                    force: bool = False, loop=None) -> dict:
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
    pdf_stem, pdf_ql = _latest_pdf(sym)
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
                pdf_stem, pdf_ql = _latest_pdf(sym)
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

    # ── Broadcast log lines so far ────────────────────────────────────────────
    result["is_new"] = is_new
    log_lines.append(f"  [MODE] {'NEW DATA' if is_new else 'VERIFY'} — "
                     f"PDF={pdf_ql} {'>' if is_new else '=='} XBRL={xbrl_ql}")

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

        if pdf_stem in all_raw and all_raw[pdf_stem].get("page_results"):
            log_lines.append(f"  [CACHE] {pdf_stem} already extracted")
            _broadcast_log(sym, log_lines[-1], loop)
            page_results = all_raw[pdf_stem].get("page_results", {})
        else:
            log_lines.append(f"  [VISION] Running Mistral vision on {pdf_stem}...")
            _broadcast_log(sym, log_lines[-1], loop)
            new_raw = extract_symbol(sym, force=False, verbose=False)
            page_results = new_raw.get(pdf_stem, {}).get("page_results", {})
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

    log.info(f"  Starting field extraction for {sym}/{pdf_stem}")
    try:
        # log_cb streams each line via WebSocket in real-time
        def _field_log_cb(line: str):
            STATE["logs"].setdefault(sym, []).append(line)
            _broadcast_log(sym, line, loop)

        log.info(f"  Calling extract_pdf_fields for {sym}/{pdf_stem} force={force}")
        ext = extract_pdf_fields(
            symbol=sym, pdf_stem=pdf_stem, page_results=page_results,
            quarter_label=pdf_ql or "", xbrl_row=xbrl_row,
            force=force, verbose=False, log_cb=_field_log_cb,
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

def _run_thread(syms: list, force: bool, loop: asyncio.AbstractEventLoop):
    db = _get_db()
    STATE["running"]  = True
    STATE["progress"] = 0
    STATE["total"]    = len(syms)
    STATE["current"]  = ""

    for i, sym in enumerate(syms):
        if not STATE["running"]:  # allow stop
            break
        STATE["current"]  = sym
        STATE["progress"] = i

        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type":"progress","current":sym,
                               "progress":i,"total":len(syms)}),
            loop,
        )

        try:
            r = verify_one_sync(sym, db, force=force, loop=loop)
        except Exception as e:
            r = dict(symbol=sym, status="ERROR", xbrl_latest="", pdf_latest="",
                     verdict="", score=0, fields={}, labels={}, math=[],
                     issues=[str(e)], is_new=False,
                     ts=datetime.now().strftime("%H:%M:%S"))
            STATE["logs"][sym] = [f"[ERROR] {e}"]
            _save_log(sym, [f"[ERROR] {e}"])

        STATE["results"][sym] = _clean_result(r)

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

    mode  = body.get("mode","single")
    sym   = body.get("symbol","")
    force = body.get("force", False)

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

    # Reset results only for the symbols being run
    for s in syms:
        STATE["results"].pop(s, None)

    loop = asyncio.get_event_loop()
    t = threading.Thread(target=_run_thread, args=(syms, force, loop), daemon=True)
    t.start()

    return JSONResponse({"started":True,"total":len(syms)})

@app.post("/api/stop")
async def stop_run():
    STATE["running"] = False
    return JSONResponse({"stopped":True})

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