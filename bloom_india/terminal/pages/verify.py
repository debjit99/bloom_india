"""
bloom_india/terminal/pages/verify.py
=====================================
PDF × XBRL Verification Dashboard — 4 tabs:
  OVERVIEW      : run, stats bar, symbol grid
  SCORE ANALYSIS: distribution, field hit rate, failure patterns
  INSPECTOR     : per-symbol fields vs XBRL, math checks, issues
  LOGS          : saved logs per symbol, searchable, downloadable
"""

import os, re, json
from pathlib import Path
from datetime import datetime

import pandas as pd
import streamlit as st

st.set_page_config(page_title="bloom_india · Verify", layout="wide", page_icon="◈")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');
*,*::before,*::after{box-sizing:border-box}
html,body,[class*="css"]{font-family:'IBM Plex Mono',monospace;background:#020c14;color:#c9d8e8}
.stApp{background:#020c14}
.block-container{padding:1.2rem 1.8rem;max-width:100%}
.vhdr{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid #0d2035;
      padding-bottom:12px;margin-bottom:14px}
.vhdr-t{font-size:19px;font-weight:800;color:#38bdf8;letter-spacing:-0.02em}
.vhdr-s{font-size:9px;color:#1e3a52;letter-spacing:0.18em}
.sbar{display:flex;gap:20px;padding:6px 0 12px;flex-wrap:wrap}
.stat{text-align:center;min-width:52px}
.sv{font-size:22px;font-weight:800;line-height:1}
.sl{font-size:9px;color:#1e3a52;letter-spacing:0.1em;margin-top:2px}
.badge{display:inline-block;padding:2px 7px;border-radius:2px;
       font-size:10px;font-weight:700;letter-spacing:0.06em}
.bg{background:#052e16;color:#22c55e;border:1px solid #166534}
.bw{background:#1c1003;color:#f59e0b;border:1px solid #92400e}
.bf{background:#2a0a0a;color:#ef4444;border:1px solid #991b1b}
.bn{background:#0c1a2e;color:#38bdf8;border:1px solid #0369a1}
.bp{background:#111;color:#475569;border:1px solid #1e293b}
.be{background:#1a0010;color:#f472b6;border:1px solid #9d174d}
.srow{display:grid;grid-template-columns:110px 100px 100px 70px 48px 1fr;
      gap:6px;padding:5px 10px;border-bottom:1px solid #061525;font-size:11px;align-items:center}
.srow:hover{background:#061525}
.ftrow{display:grid;grid-template-columns:170px 118px 118px 80px 1fr;
       gap:6px;padding:5px 10px;border-bottom:1px solid #061525;font-size:11px;align-items:center}
.ftrow:hover{background:#061525}
.logbox{background:#030f1a;border:1px solid #0d2035;border-radius:3px;
        padding:10px 14px;font-size:10px;font-family:'IBM Plex Mono',monospace;
        color:#475569;height:400px;overflow-y:auto;white-space:pre-wrap;line-height:1.7}
.lok{color:#22c55e}.lwarn{color:#f59e0b}.lerr{color:#ef4444}
.linfo{color:#38bdf8}.ldim{color:#1e3a52}
.prog-wrap{background:#061525;border-radius:2px;height:3px;margin:4px 0}
.prog-fill{background:#38bdf8;height:3px;border-radius:2px}
.dvd{border:none;border-top:1px solid #0d2035;margin:10px 0}
.stTabs [data-baseweb="tab-list"]{background:#020c14 !important;border-bottom:1px solid #0d2035;gap:0}
.stTabs [data-baseweb="tab"]{font-family:'IBM Plex Mono',monospace !important;
  font-size:11px !important;letter-spacing:0.1em !important;
  color:#334155 !important;padding:8px 20px !important}
.stTabs [aria-selected="true"]{color:#38bdf8 !important;border-bottom:2px solid #38bdf8 !important}
.stTabs [data-baseweb="tab-panel"]{padding:14px 0 !important}
.stButton>button{background:#38bdf8 !important;color:#020c14 !important;
  font-family:'IBM Plex Mono',monospace !important;font-weight:800 !important;
  font-size:11px !important;letter-spacing:0.08em !important;
  border:none !important;border-radius:3px !important;padding:7px 18px !important}
.stButton>button:hover{background:#7dd3fc !important}
.stButton>button[disabled]{background:#061525 !important;color:#1e3a52 !important}
.stSelectbox>div>div,.stTextInput>div>div>input{background:#061525 !important;
  border:1px solid #0d2035 !important;font-family:'IBM Plex Mono',monospace !important;
  font-size:11px !important;color:#94a3b8 !important}
</style>
""", unsafe_allow_html=True)

# ── Setup ─────────────────────────────────────────────────────────────────────
os.environ.setdefault(
    "BLOOM_INDIA_CONFIG",
    str(Path(__file__).parents[3] / "config.yaml"),
)
try:
    from bloom_india.config import CONFIG
    from bloom_india.data.process.pdf_extract import extract_symbol
    from bloom_india.data.process.pdf_mistral_extractor import extract_pdf_fields
except Exception as e:
    st.error(f"Import error: {e}"); st.stop()

LOG_DIR = Path(CONFIG.storage.base_dir) / "verify_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

for k, v in [("results",{}),("running",False),("progress",0),("total",0),("sel_sym",None)]:
    if k not in st.session_state: st.session_state[k] = v

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

def _ensure_extracted(sym, pdf_stem, log_lines):
    raw_path = Path(CONFIG.storage.raw_dir)/"filings"/sym.upper()/"raw_extractions.json"
    all_raw = {}
    if raw_path.exists():
        with open(raw_path) as f: all_raw = json.load(f)
    if pdf_stem in all_raw and all_raw[pdf_stem].get("page_results"):
        log_lines.append(f"  [CACHE] {pdf_stem} already extracted")
        return all_raw[pdf_stem].get("page_results", {})
    log_lines.append(f"  [VISION] Running Mistral vision on {pdf_stem}...")
    new_raw = extract_symbol(sym, force=False, verbose=False)
    return new_raw.get(pdf_stem, {}).get("page_results", {})

def _badge(s):
    cls = {"GOOD":"bg","WARN":"bw","FAIL":"bf","NEW":"bn",
           "NO_PDF":"bp","NO_XBRL":"bp","ERROR":"be"}.get(s,"bp")
    return f'<span class="badge {cls}">{s}</span>'

def _log_cls(line):
    if any(x in line for x in ["[ERROR]","✗","FAIL"]): return "lerr"
    if any(x in line for x in ["[WARN]","⚠","ISSUE"]): return "lwarn"
    if any(x in line for x in ["✓","[DONE]","[RESULT]"]): return "lok"
    if any(x in line for x in ["[MISTRAL]","[VISION]","[XBRL]","[PDF]","[MODE]","[MATH"]): return "linfo"
    if any(x in line for x in ["[CACHE]","[INFO]"]): return "ldim"
    return ""

def _render_log(lines):
    html = ""
    for line in lines:
        cls  = _log_cls(line)
        safe = line.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        html += f'<div class="{cls}">{safe}</div>' if cls else f'<div>{safe}</div>'
    return f'<div class="logbox">{html}</div>'

def _save_log(sym, lines):
    with open(LOG_DIR/f"{sym}.log","w") as f:
        f.write(f"# bloom_india verify — {sym}\n# {datetime.now().isoformat()}\n\n")
        f.write("\n".join(lines))

def _load_log(sym):
    p = LOG_DIR/f"{sym}.log"
    return open(p).read().splitlines() if p.exists() else []

# ── Core ──────────────────────────────────────────────────────────────────────
def verify_one(sym, db, force=False):
    log_lines = []
    ts = datetime.now().strftime("%H:%M:%S")
    result = dict(symbol=sym, xbrl_latest="", pdf_latest="", status="UNKNOWN",
                  verdict="", score=0, fields={}, labels={}, math=[], issues=[],
                  is_new=False, ts=ts)
    log_lines.append(f"[{ts}] ══ {sym} ══")

    xbrl_ql = _latest_xbrl(db, sym)
    result["xbrl_latest"] = xbrl_ql
    if not xbrl_ql:
        log_lines.append("  [WARN] No XBRL data in fundamentals DB")
        result["status"] = "NO_XBRL"; _save_log(sym, log_lines); return result, log_lines
    log_lines.append(f"  [XBRL] Latest quarter: {xbrl_ql}")

    pdf_stem, pdf_ql = _latest_pdf(sym)
    result["pdf_latest"] = pdf_ql or ""
    if not pdf_stem:
        log_lines.append("  [INFO] No PDFs found")
        result["status"] = "NO_PDF"; _save_log(sym, log_lines); return result, log_lines
    log_lines.append(f"  [PDF]  Latest: {pdf_stem} ({pdf_ql})")

    is_new = _qkey(pdf_ql) > _qkey(xbrl_ql)
    result["is_new"] = is_new
    log_lines.append(f"  [MODE] {'NEW DATA' if is_new else 'VERIFY'} — "
                     f"PDF={pdf_ql} {'>' if is_new else '=='} XBRL={xbrl_ql}")

    xbrl_row = None
    if not is_new and pdf_ql:
        rows = db[(db["symbol"]==sym)&(db["quarter_label"]==pdf_ql)]
        if not rows.empty:
            xbrl_row = rows.iloc[0].to_dict()
            log_lines.append(f"  [XBRL] Reference: rev={xbrl_row.get('revenue')} "
                             f"pat={xbrl_row.get('pat')} eps={xbrl_row.get('eps_basic')}")
        else:
            log_lines.append(f"  [WARN] No XBRL row for {sym} {pdf_ql}")

    try:
        page_results = _ensure_extracted(sym, pdf_stem, log_lines)
    except Exception as e:
        log_lines.append(f"  [ERROR] Vision extraction: {e}")
        result["status"] = "ERROR"; _save_log(sym, log_lines); return result, log_lines

    if not page_results:
        log_lines.append("  [ERROR] No page_results after extraction")
        result["status"] = "ERROR"; _save_log(sym, log_lines); return result, log_lines

    n_tables = sum(1 for p in page_results.values() if p.get("has_financial_table"))
    log_lines.append(f"  [PDF]  {len(page_results)} pages, {n_tables} with tables")

    try:
        ext = extract_pdf_fields(
            symbol=sym, pdf_stem=pdf_stem, page_results=page_results,
            quarter_label=pdf_ql or "", xbrl_row=xbrl_row,
            force=force, verbose=False,
        )
    except Exception as e:
        log_lines.append(f"  [ERROR] Field extraction: {e}")
        result["status"] = "ERROR"; _save_log(sym, log_lines); return result, log_lines

    result.update(fields=ext["fields"], labels=ext.get("labels",{}),
                  math=ext["math"], verdict=ext["verdict"],
                  score=ext["score"], issues=ext["issues"])

    log_lines.append(f"  [RESULT] {len(ext['fields'])} fields | "
                     f"verdict={ext['verdict']} | score={ext['score']}")

    log_lines.append("  [MATH CHECKS]")
    for mc in ext.get("math",[]):
        s = "✓" if mc["status"]=="PASS" else "⚠" if mc["status"]=="WARN" else "✗"
        log_lines.append(f"    {s} {mc['field']:<28} "
                         f"expected={mc['expected']:.2f}  got={mc['got']:.2f}  ({mc['diff_pct']:.1f}%)")

    if xbrl_row:
        log_lines.append("  [XBRL FIELD COMPARISON]")
        for field in ["revenue","pat","eps_basic","eps_diluted","interest_expended",
                      "equity_capital","npa_gross_cr","npa_net_cr","deposits_bank"]:
            pv = ext["fields"].get(field)
            if pv is None:
                log_lines.append(f"    — {field:<28} not extracted"); continue
            xv_raw = xbrl_row.get(field)
            if xv_raw is None or str(xv_raw)=="nan":
                log_lines.append(f"    · {field:<28} pdf={pv:.4f}  xbrl=N/A"); continue
            xv   = float(xv_raw)
            diff = abs(pv-xv)/(abs(xv)+1e-9)*100
            s    = "✓" if diff<1 else "⚠" if diff<5 else "✗"
            log_lines.append(f"    {s} {field:<28} pdf={pv:.4f}  xbrl={xv:.4f}  ({diff:.1f}%)")

    if ext["issues"]:
        log_lines.append("  [ISSUES]")
        for issue in ext["issues"]: log_lines.append(f"    ! {issue}")

    result["status"] = ("NEW" if is_new else
                        "GOOD" if ext["verdict"]=="GOOD" else
                        "WARN" if ext["verdict"]=="WARN" else "FAIL")
    log_lines.append(f"  [DONE] → {result['status']}")
    _save_log(sym, log_lines)
    return result, log_lines

# ── Load data ─────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_db():
    return pd.read_parquet(str(CONFIG.storage.fundamental_db))

@st.cache_data
def load_universe():
    p = Path(CONFIG.storage.base_dir)/"nifty500.json"
    if p.exists():
        data = json.load(open(p))
        return [s["symbol"] if isinstance(s,dict) else s for s in data]
    return sorted(load_db()["symbol"].unique().tolist())

try:
    db       = load_db()
    universe = load_universe()
except Exception as e:
    st.error(f"Could not load data: {e}"); st.stop()

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="vhdr">
  <span class="vhdr-t">◈ PDF × XBRL</span>
  <span class="vhdr-s">VERIFICATION DASHBOARD</span>
</div>""", unsafe_allow_html=True)

# ── Controls ──────────────────────────────────────────────────────────────────
cc1, cc2, cc3, cc4, cc5 = st.columns([2,2,1,1,1])
with cc1:
    mode = st.selectbox("Mode",["Single symbol","All symbols with PDFs","Full Nifty500"],key="mode")
with cc2:
    filings_root = Path(CONFIG.storage.raw_dir)/"filings"
    if mode=="Single symbol":
        have_pdfs = sorted([s for s in universe
                            if (filings_root/s).exists() and list((filings_root/s).glob("*.pdf"))])
        sym_sel = st.selectbox("Symbol", have_pdfs or universe, key="sym_sel")
    else:
        sym_sel = None
        n_have = sum(1 for s in universe
                     if (filings_root/s).exists() and list((filings_root/s).glob("*.pdf")))
        st.markdown(f'<div style="padding-top:28px;color:#1e3a52;font-size:10px">'
                    f'{n_have} have PDFs / {len(universe)} total</div>',
                    unsafe_allow_html=True)
with cc3:
    force = st.checkbox("Force re-extract", False, key="force")
with cc4:
    run_btn = st.button("▶ RUN", disabled=st.session_state.running, key="run_btn")
with cc5:
    if st.button("✕ Clear", key="clear_btn"):
        st.session_state.results  = {}
        st.session_state.progress = 0
        st.session_state.total    = 0
        st.session_state.sel_sym  = None
        st.rerun()

if st.session_state.total > 0:
    pct = st.session_state.progress / st.session_state.total
    st.markdown(
        f'<div class="prog-wrap"><div class="prog-fill" style="width:{pct*100:.1f}%"></div></div>'
        f'<div style="font-size:9px;color:#1e3a52">'
        f'{st.session_state.progress}/{st.session_state.total} processed</div>',
        unsafe_allow_html=True,
    )

# ── Run ───────────────────────────────────────────────────────────────────────
if run_btn:
    if mode=="Single symbol" and sym_sel:
        syms = [sym_sel]
    elif mode=="All symbols with PDFs":
        syms = [s for s in universe
                if (filings_root/s).exists() and list((filings_root/s).glob("*.pdf"))]
    else:
        syms = universe

    st.session_state.running  = True
    st.session_state.progress = 0
    st.session_state.total    = len(syms)
    prog_ph   = st.empty()
    status_ph = st.empty()

    for sym in syms:
        status_ph.markdown(
            f'<div style="font-size:10px;color:#1e3a52">Processing {sym}...</div>',
            unsafe_allow_html=True,
        )
        try:
            r, _ = verify_one(sym, db, force=force)
        except Exception as e:
            r = dict(symbol=sym, status="ERROR", xbrl_latest="", pdf_latest="",
                     verdict="", score=0, fields={}, labels={}, math=[], issues=[str(e)],
                     is_new=False, ts=datetime.now().strftime("%H:%M:%S"))
            _save_log(sym, [f"[ERROR] {e}"])
        st.session_state.results[sym] = r
        st.session_state.progress    += 1
        pct = st.session_state.progress / st.session_state.total
        prog_ph.markdown(
            f'<div class="prog-wrap"><div class="prog-fill" style="width:{pct*100:.1f}%">'
            f'</div></div><div style="font-size:9px;color:#1e3a52">'
            f'{st.session_state.progress}/{st.session_state.total} — {sym} {r["status"]}</div>',
            unsafe_allow_html=True,
        )
    st.session_state.running = False
    status_ph.empty()
    st.rerun()

# ── Tabs ──────────────────────────────────────────────────────────────────────
results = st.session_state.results
t1, t2, t3, t4 = st.tabs(["OVERVIEW","SCORE ANALYSIS","INSPECTOR","LOGS"])

# ══════════════ TAB 1: OVERVIEW ══════════════
with t1:
    if not results:
        st.markdown('<div style="color:#1e3a52;padding:60px;text-align:center">'
                    'Select a mode and click ▶ RUN.</div>', unsafe_allow_html=True)
    else:
        counts = {}
        for r in results.values(): counts[r["status"]] = counts.get(r["status"],0)+1
        stat_html = '<div class="sbar">'
        for status,color in [("GOOD","#22c55e"),("WARN","#f59e0b"),("FAIL","#ef4444"),
                              ("NEW","#38bdf8"),("NO_PDF","#475569"),("ERROR","#f472b6")]:
            n = counts.get(status,0)
            stat_html += (f'<div class="stat"><div class="sv" style="color:{color}">{n}</div>'
                          f'<div class="sl">{status}</div></div>')
        stat_html += (f'<div class="stat"><div class="sv" style="color:#94a3b8">'
                      f'{len(results)}</div><div class="sl">TOTAL</div></div></div>')
        st.markdown(stat_html, unsafe_allow_html=True)

        st.markdown(
            '<div class="srow" style="color:#1e3a52;font-size:9px;letter-spacing:0.1em">'
            '<span>SYMBOL</span><span>XBRL</span><span>PDF</span>'
            '<span>STATUS</span><span>SCORE</span><span>ISSUES</span></div>',
            unsafe_allow_html=True,
        )
        sort_ord = {"FAIL":0,"ERROR":1,"WARN":2,"NEW":3,"GOOD":4,"NO_PDF":5,"NO_XBRL":6,"UNKNOWN":7}
        for sym in sorted(results, key=lambda s: sort_ord.get(results[s]["status"],9)):
            r  = results[sym]
            ni = len(r.get("issues",[]))
            sc = r.get("score",0)
            sc_color = "#22c55e" if sc>=85 else "#f59e0b" if sc>=60 else "#ef4444"
            st.markdown(
                f'<div class="srow">'
                f'<span style="color:#7dd3fc;font-weight:600">{sym}</span>'
                f'<span style="color:#334155">{r["xbrl_latest"]}</span>'
                f'<span style="color:#334155">{r["pdf_latest"]}</span>'
                f'<span>{_badge(r["status"])}</span>'
                f'<span style="color:{sc_color};font-weight:700">{sc if sc else "—"}</span>'
                f'<span style="color:{"#ef4444" if ni else "#1e3a52"}">'
                f'{"⚠ "+str(ni) if ni else "—"}</span></div>',
                unsafe_allow_html=True,
            )
            if st.button(f"→ inspect {sym}", key=f"ins_{sym}",
                         help=f"Open {sym} in Inspector tab"):
                st.session_state.sel_sym = sym

        st.markdown('<hr class="dvd">', unsafe_allow_html=True)
        if st.button("⬇ Export JSON", key="export"):
            out = []
            for sym, r in results.items():
                out.append({k:v for k,v in r.items() if k not in ("fields","labels")} | {
                    "fields_count": len(r.get("fields",{})),
                    "key_fields": {k:round(v,4) if isinstance(v,float) else v
                                   for k,v in r.get("fields",{}).items()
                                   if k in ["revenue","pat","eps_basic","eps_diluted"]}
                })
            p = Path(CONFIG.storage.base_dir)/"verify_report.json"
            json.dump(out, open(p,"w"), indent=2)
            st.success(f"Saved → {p}")

# ══════════════ TAB 2: SCORE ANALYSIS ══════════════
with t2:
    verified = {s:r for s,r in results.items()
                if r["status"] in ("GOOD","WARN","FAIL") and r["score"]>0}
    if not verified:
        st.markdown('<div style="color:#1e3a52;padding:40px">No verified results yet.</div>',
                    unsafe_allow_html=True)
    else:
        scores = [r["score"] for r in verified.values()]
        avg    = sum(scores)/len(scores)
        med    = sorted(scores)[len(scores)//2]

        c1,c2,c3 = st.columns(3)
        c1.markdown(f'<div class="stat"><div class="sv" style="color:#38bdf8">{avg:.0f}</div>'
                    f'<div class="sl">AVG SCORE</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="stat"><div class="sv" style="color:#7dd3fc">{med}</div>'
                    f'<div class="sl">MEDIAN</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="stat"><div class="sv" style="color:#94a3b8">{len(verified)}</div>'
                    f'<div class="sl">VERIFIED</div></div>', unsafe_allow_html=True)

        st.markdown('<hr class="dvd">', unsafe_allow_html=True)

        # Score distribution
        st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                    'margin-bottom:10px">SCORE DISTRIBUTION</div>', unsafe_allow_html=True)
        buckets = [(f"{i}–{i+9}", sum(1 for s in scores if i<=s<i+10)) for i in range(0,101,10)]
        max_n   = max(n for _,n in buckets) or 1
        chart   = ""
        for label,n in buckets:
            lo    = int(label.split("–")[0])
            color = "#22c55e" if lo>=85 else "#f59e0b" if lo>=60 else "#ef4444"
            w     = max(4, int(n/max_n*320))
            chart += (f'<div style="display:flex;align-items:center;gap:10px;margin:3px 0">'
                      f'<span style="color:#334155;font-size:10px;width:50px">{label}</span>'
                      f'<div style="background:{color};height:14px;width:{w}px;border-radius:2px"></div>'
                      f'<span style="color:#475569;font-size:10px">{n}</span></div>')
        st.markdown(chart, unsafe_allow_html=True)

        st.markdown('<hr class="dvd">', unsafe_allow_html=True)

        # Field hit rate
        st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                    'margin-bottom:10px">FIELD EXTRACTION HIT RATE</div>', unsafe_allow_html=True)
        key_flds = ["revenue","pat","eps_basic","eps_diluted","interest_expended",
                    "employee_cost","operating_expenses_bank","operating_profit_bank",
                    "provisions_bank","profit_before_tax","tax","npa_gross_cr",
                    "npa_net_cr","npa_pct_gross","car","equity_capital",
                    "reserves_surplus","deposits_bank","borrowings_current"]
        hit_html = ""
        for field in key_flds:
            n_hit = sum(1 for r in verified.values() if r["fields"].get(field) is not None)
            rate  = n_hit/len(verified)
            color = "#22c55e" if rate>=0.8 else "#f59e0b" if rate>=0.5 else "#ef4444"
            w     = max(4, int(rate*280))
            hit_html += (
                f'<div style="display:flex;align-items:center;gap:10px;margin:2px 0">'
                f'<span style="color:#475569;font-size:10px;width:200px;font-family:monospace">'
                f'{field}</span>'
                f'<div style="background:{color};height:10px;width:{w}px;border-radius:2px"></div>'
                f'<span style="color:{color};font-size:10px">{rate*100:.0f}%</span></div>'
            )
        st.markdown(hit_html, unsafe_allow_html=True)

        st.markdown('<hr class="dvd">', unsafe_allow_html=True)

        # Failure patterns
        fail_warn = {s:r for s,r in verified.items() if r["status"] in ("FAIL","WARN")}
        if fail_warn:
            st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                        'margin-bottom:10px">FAILURE PATTERNS</div>', unsafe_allow_html=True)
            issue_counts = {}
            for r in fail_warn.values():
                for issue in r.get("issues",[]):
                    if "MATH" in issue:   key = "Math check failed"
                    elif "XBRL FAIL" in issue:
                        f = re.search(r"\] (\w+):", issue)
                        key = f"XBRL mismatch: {f.group(1) if f else '?'}"
                    elif "HALLUCINATION" in issue: key = "Hallucination detected"
                    else: key = issue[:50]
                    issue_counts[key] = issue_counts.get(key,0)+1
            for issue,n in sorted(issue_counts.items(), key=lambda x:-x[1]):
                st.markdown(
                    f'<div style="display:flex;gap:12px;padding:4px 0;font-size:11px">'
                    f'<span style="color:#ef4444;min-width:28px;text-align:right">{n}×</span>'
                    f'<span style="color:#94a3b8">{issue}</span></div>',
                    unsafe_allow_html=True,
                )

        # Failing symbols table
        if fail_warn:
            st.markdown('<hr class="dvd">', unsafe_allow_html=True)
            st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                        'margin-bottom:8px">SYMBOLS NEEDING ATTENTION</div>',
                        unsafe_allow_html=True)
            for sym, r in sorted(fail_warn.items(), key=lambda x: x[1]["score"]):
                sc    = r["score"]
                color = "#f59e0b" if r["status"]=="WARN" else "#ef4444"
                issues_str = " | ".join(r.get("issues",[]))[:80]
                st.markdown(
                    f'<div style="display:grid;grid-template-columns:100px 60px 60px 1fr;'
                    f'gap:8px;padding:5px 10px;border-bottom:1px solid #061525;font-size:11px">'
                    f'<span style="color:#7dd3fc;font-weight:600">{sym}</span>'
                    f'<span>{_badge(r["status"])}</span>'
                    f'<span style="color:{color};font-weight:700">{sc}</span>'
                    f'<span style="color:#334155;overflow:hidden;white-space:nowrap;'
                    f'text-overflow:ellipsis">{issues_str}</span></div>',
                    unsafe_allow_html=True,
                )
                if st.button(f"→ inspect {sym}", key=f"fa_{sym}"):
                    st.session_state.sel_sym = sym

# ══════════════ TAB 3: INSPECTOR ══════════════
with t3:
    ic1, ic2 = st.columns([1,2])
    with ic1:
        if not results:
            st.markdown('<div style="color:#1e3a52;padding:20px">No results yet.</div>',
                        unsafe_allow_html=True)
        else:
            st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                        'margin-bottom:8px">SELECT SYMBOL</div>', unsafe_allow_html=True)
            sort_ord = {"FAIL":0,"ERROR":1,"WARN":2,"NEW":3,"GOOD":4,"NO_PDF":5,"NO_XBRL":6}
            for sym in sorted(results, key=lambda s: sort_ord.get(results[s]["status"],9)):
                r  = results[sym]
                sc = f" [{r['score']}]" if r.get("score") else ""
                if st.button(f"{sym}{sc}", key=f"insp_{sym}",
                             help=f"{r['status']} {r.get('verdict','')}",
                             use_container_width=True):
                    st.session_state.sel_sym = sym
                    st.rerun()

    with ic2:
        sel = st.session_state.sel_sym
        if not sel or sel not in results:
            st.markdown('<div style="color:#1e3a52;font-size:12px;padding:60px;text-align:center">'
                        '← Select a symbol.</div>', unsafe_allow_html=True)
        else:
            r = results[sel]
            st.markdown(
                f'<div style="margin-bottom:14px">'
                f'<span style="font-size:20px;font-weight:800;color:#7dd3fc">{sel}</span>'
                f'  {_badge(r["status"])}'
                f'  <span style="font-size:10px;color:#1e3a52">'
                f'score={r["score"]} | XBRL:{r["xbrl_latest"]} → PDF:{r["pdf_latest"]}'
                f'{"  🆕 NEW QUARTER" if r.get("is_new") else ""}'
                f'</span></div>',
                unsafe_allow_html=True,
            )

            # Math checks
            if r.get("math"):
                st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                            'margin-bottom:6px">MATH CHECKS</div>', unsafe_allow_html=True)
                for mc in r["math"]:
                    s     = "✓" if mc["status"]=="PASS" else "⚠" if mc["status"]=="WARN" else "✗"
                    color = "#22c55e" if mc["status"]=="PASS" else \
                            "#f59e0b" if mc["status"]=="WARN" else "#ef4444"
                    st.markdown(
                        f'<div style="display:flex;gap:10px;padding:4px 10px;font-size:11px;'
                        f'border-bottom:1px solid #061525">'
                        f'<span style="color:{color};font-weight:700;min-width:14px">{s}</span>'
                        f'<span style="color:#475569;width:160px;font-family:monospace">'
                        f'{mc["field"]}</span>'
                        f'<span style="color:#334155">exp={mc["expected"]:.2f}</span>'
                        f'<span style="color:#334155">got={mc["got"]:.2f}</span>'
                        f'<span style="color:{color}">({mc["diff_pct"]:.1f}%)</span></div>',
                        unsafe_allow_html=True,
                    )
                st.markdown('<hr class="dvd">', unsafe_allow_html=True)

            # Field comparison
            fields = r.get("fields",{})
            labels = r.get("labels",{})
            xbrl_ref = {}
            if not r.get("is_new") and r["pdf_latest"]:
                rows = db[(db["symbol"]==sel)&(db["quarter_label"]==r["pdf_latest"])]
                if not rows.empty: xbrl_ref = rows.iloc[0].to_dict()

            if fields:
                st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                            'margin-bottom:6px">FIELD VALUES</div>', unsafe_allow_html=True)
                st.markdown(
                    '<div class="ftrow" style="color:#1e3a52;font-size:9px">'
                    '<span>FIELD</span><span style="text-align:right">PDF</span>'
                    '<span style="text-align:right">XBRL</span>'
                    '<span style="text-align:center">MATCH</span>'
                    '<span>LABEL</span></div>',
                    unsafe_allow_html=True,
                )
                all_flds = ["revenue","pat","eps_basic","eps_diluted","other_income",
                            "total_income","interest_expended","employee_cost",
                            "operating_expenses_bank","operating_profit_bank",
                            "provisions_bank","profit_before_tax","tax","pat_minority",
                            "npa_gross_cr","npa_net_cr","npa_pct_gross","npa_pct_net",
                            "car","equity_capital","reserves_surplus",
                            "deposits_bank","borrowings_current"]
                rows_html = ""
                for field in all_flds:
                    pv = fields.get(field)
                    if pv is None: continue
                    xv_raw = xbrl_ref.get(field)
                    lbl    = (labels.get(field) or "")[:42]
                    if xv_raw is not None and str(xv_raw)!="nan":
                        try:
                            xv    = float(xv_raw)
                            diff  = abs(pv-xv)/(abs(xv)+1e-9)*100
                            color = "#22c55e" if diff<1 else "#f59e0b" if diff<5 else "#ef4444"
                            match = (f'<span style="color:{color};font-weight:700">'
                                     f'{"✓" if diff<1 else "⚠" if diff<5 else "✗"} '
                                     f'{diff:.1f}%</span>')
                            xv_s  = f"{xv:,.4f}"
                        except:
                            match,xv_s = '<span style="color:#1e3a52">—</span>',"—"
                    else:
                        match = '<span style="color:#1e3a52">—</span>'
                        xv_s  = "N/A"
                    rows_html += (
                        f'<div class="ftrow">'
                        f'<span style="color:#475569;font-family:monospace">{field}</span>'
                        f'<span style="text-align:right;color:#94a3b8;font-family:monospace">'
                        f'{pv:,.4f}</span>'
                        f'<span style="text-align:right;color:#334155;font-family:monospace">'
                        f'{xv_s}</span>'
                        f'<span style="text-align:center">{match}</span>'
                        f'<span style="color:#1e3a52;font-size:10px;overflow:hidden;'
                        f'white-space:nowrap;text-overflow:ellipsis">{lbl}</span></div>'
                    )
                st.markdown(rows_html, unsafe_allow_html=True)

            # Issues
            if r.get("issues"):
                st.markdown('<hr class="dvd">', unsafe_allow_html=True)
                st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                            'margin-bottom:6px">ISSUES</div>', unsafe_allow_html=True)
                for issue in r["issues"]:
                    color = "#ef4444" if "FAIL" in issue else "#f59e0b"
                    st.markdown(
                        f'<div style="font-size:11px;color:{color};padding:3px 10px">'
                        f'! {issue}</div>',
                        unsafe_allow_html=True,
                    )

            # Log inline
            st.markdown('<hr class="dvd">', unsafe_allow_html=True)
            st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                        'margin-bottom:6px">EXECUTION LOG</div>', unsafe_allow_html=True)
            lines = _load_log(sel)
            st.markdown(_render_log(lines), unsafe_allow_html=True)

# ══════════════ TAB 4: LOGS ══════════════
with t4:
    lc1, lc2 = st.columns([2,1])
    with lc1:
        log_filter = st.text_input("Search logs", placeholder="e.g. FAIL, revenue, ERROR",
                                   key="log_filter")
    with lc2:
        show_only = st.selectbox("Filter by status",
                                 ["All","FAIL only","WARN + FAIL","GOOD only","Has issues"],
                                 key="log_show")

    saved_logs = sorted([p.stem for p in LOG_DIR.glob("*.log")])

    if not saved_logs:
        st.markdown('<div style="color:#1e3a52;padding:40px;text-align:center">'
                    'No logs saved yet. Run verification first.</div>',
                    unsafe_allow_html=True)
    else:
        # Symbol selector + viewer
        lv1, lv2 = st.columns([1,3])
        with lv1:
            log_sym = st.selectbox("Symbol log", saved_logs, key="log_sym")
        with lv2:
            if st.button("⬇ Download log", key="dl_log"):
                pass  # handled by download_button below

        log_sym = st.session_state.get("log_sym", saved_logs[0])
        lines   = _load_log(log_sym)

        # Search filter
        filtered = [l for l in lines if log_filter.lower() in l.lower()] \
                   if log_filter else lines

        col_dlbtn, col_cnt = st.columns([1,4])
        with col_dlbtn:
            log_path = LOG_DIR/f"{log_sym}.log"
            if log_path.exists():
                st.download_button(
                    f"⬇ {log_sym}.log",
                    data      = open(log_path).read(),
                    file_name = f"{log_sym}_verify.log",
                    mime      = "text/plain",
                    key       = "dl_log_btn",
                )
        with col_cnt:
            st.markdown(
                f'<div style="padding-top:10px;font-size:10px;color:#1e3a52">'
                f'{len(filtered)} lines {("(filtered)" if log_filter else "")}</div>',
                unsafe_allow_html=True,
            )

        st.markdown(_render_log(filtered), unsafe_allow_html=True)

        st.markdown('<hr class="dvd">', unsafe_allow_html=True)

        # All logs summary
        st.markdown('<div style="font-size:9px;color:#1e3a52;letter-spacing:0.12em;'
                    'margin-bottom:8px">ALL SAVED LOGS</div>', unsafe_allow_html=True)

        hdr = (
            '<div style="display:grid;grid-template-columns:100px 72px 60px 1fr;'
            'gap:6px;padding:4px 10px;font-size:9px;color:#1e3a52;letter-spacing:0.1em">'
            '<span>SYMBOL</span><span>STATUS</span><span>SCORE</span>'
            '<span>LAST LOG LINE</span></div>'
        )
        rows_html = hdr
        for sym in saved_logs:
            r      = results.get(sym,{})
            status = r.get("status","?")

            if show_only=="FAIL only"   and status!="FAIL":               continue
            if show_only=="WARN + FAIL" and status not in ("WARN","FAIL"): continue
            if show_only=="GOOD only"   and status!="GOOD":               continue
            if show_only=="Has issues"  and not r.get("issues"):          continue

            lines_all = _load_log(sym)
            if log_filter and log_filter.lower() not in " ".join(lines_all).lower():
                continue

            last  = lines_all[-1] if lines_all else ""
            sc    = r.get("score","")
            color = "#22c55e" if status=="GOOD" else \
                    "#f59e0b" if status=="WARN" else \
                    "#ef4444" if status=="FAIL" else "#475569"
            rows_html += (
                f'<div style="display:grid;grid-template-columns:100px 72px 60px 1fr;'
                f'gap:6px;padding:5px 10px;border-bottom:1px solid #061525;font-size:11px">'
                f'<span style="color:#7dd3fc;font-weight:600">{sym}</span>'
                f'<span style="color:{color}">{status}</span>'
                f'<span style="color:{color};font-weight:700">{sc}</span>'
                f'<span style="color:#334155;overflow:hidden;white-space:nowrap;'
                f'text-overflow:ellipsis">{last[:80]}</span></div>'
            )
        st.markdown(rows_html, unsafe_allow_html=True)