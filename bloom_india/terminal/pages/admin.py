"""
bloom_india/terminal/pages/admin.py
=====================================
Data management UI — configure paths, select universe,
download XBRL filings, build databases, view logs.

Run:
    streamlit run bloom_india/terminal/pages/admin.py
"""

import os
import sys
import json
import time
import shutil
import threading
import subprocess
from pathlib import Path
from datetime import datetime
import random
import streamlit as st
import pandas as pd

# ── Ensure config found ───────────────────────────────────────────────────────
_candidates = [
    os.path.join(os.getcwd(), "config.yaml"),
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "config.yaml"),
]
for _c in _candidates:
    if os.path.exists(_c):
        os.environ.setdefault("BLOOM_INDIA_CONFIG", os.path.abspath(_c))
        break

from bloom_india.config import CONFIG, ensure_dirs

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title  = "◈ bloom_india — Admin",
    page_icon   = "◈",
    layout      = "wide",
    initial_sidebar_state = "expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500&display=swap');
html,body,[class*="css"]{background:#0a0e0f!important;color:#c8d0cc!important;
    font-family:'JetBrains Mono',monospace!important}
.stApp{background:#0a0e0f}
section[data-testid="stSidebar"]{background:#0d1617!important;
    border-right:1px solid #1e2a2c!important}
section[data-testid="stSidebar"] *{color:#c8d0cc!important}
.stButton>button{background:#111618;border:1px solid #1e2a2c;color:#c8d0cc;
    font-family:'JetBrains Mono',monospace;border-radius:3px;padding:6px 16px}
.stButton>button:hover{border-color:#4ecca3;color:#4ecca3}
.stSelectbox>div>div,.stTextInput>div>div>input{background:#111618!important;
    border:1px solid #1e2a2c!important;color:#c8d0cc!important;border-radius:3px!important}
.stProgress>div>div{background:#4ecca3!important}
.stTabs [data-baseweb="tab-list"]{background:#111618!important;
    border-bottom:1px solid #1e2a2c!important}
.stTabs [data-baseweb="tab"]{color:#3d5052!important;font-size:11px!important;
    letter-spacing:.08em!important;border-right:1px solid #1e2a2c!important}
.stTabs [aria-selected="true"]{color:#4ecca3!important;
    border-bottom:2px solid #4ecca3!important;background:#0d1617!important}
.card{background:#111618;border:1px solid #1e2a2c;border-radius:4px;padding:12px 16px;margin-bottom:8px}
.section-head{font-size:9px;color:#4ecca3;letter-spacing:.14em;text-transform:uppercase;
    border-left:2px solid #4ecca3;padding-left:8px;margin:14px 0 8px}
.stat{background:#0d1617;border:1px solid #1e2a2c;border-radius:3px;padding:10px 14px}
.stat-label{font-size:9px;color:#3d5052;letter-spacing:.1em;text-transform:uppercase}
.stat-val{font-size:18px;font-weight:500;color:#c8d0cc;margin-top:2px}
.log-box{background:#060809;border:1px solid #1e2a2c;border-radius:3px;
    padding:10px 14px;font-size:11px;color:#637b7d;height:300px;overflow-y:auto;
    font-family:'JetBrains Mono',monospace}
.ok{color:#4ecca3}.err{color:#e05252}.warn{color:#e09a52}.dim{color:#3d5052}
</style>
""", unsafe_allow_html=True)

# ── State ─────────────────────────────────────────────────────────────────────

STATE_FILE = Path(CONFIG.storage.raw_dir) / "admin_state.json"

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except: pass
    return {
        "base_dir":       str(CONFIG.storage.base_dir),
        "universe":       "NIFTY 50",
        "symbols":        [],
        "last_xbrl_fetch": None,
        "last_db_build":   None,
        "last_price_fetch":None,
        "xbrl_progress":  {},
    }

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

if "state" not in st.session_state:
    st.session_state.state = load_state()

if "log" not in st.session_state:
    st.session_state.log = []

def log(msg: str, kind: str = "info"):
    ts  = datetime.now().strftime("%H:%M:%S")
    css = {"info":"","ok":"ok","err":"err","warn":"warn","dim":"dim"}.get(kind,"")
    st.session_state.log.append(f'<span class="{css}">[{ts}] {msg}</span>')
    if len(st.session_state.log) > 200:
        st.session_state.log = st.session_state.log[-200:]

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<div style="font-size:18px;font-weight:500;color:#4ecca3;'
        'letter-spacing:.1em;margin-bottom:2px">◈ bloom_india</div>'
        '<div style="font-size:9px;color:#3d5052;letter-spacing:.1em;'
        'margin-bottom:20px;text-transform:uppercase">Admin · Data Manager</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div style="font-size:9px;color:#3d5052;letter-spacing:.1em;'
        'text-transform:uppercase;margin-bottom:6px">Config file</div>'
        f'<div style="font-size:10px;color:#637b7d;word-break:break-all">'
        f'{os.environ.get("BLOOM_INDIA_CONFIG","not set")}</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # Quick status
    xbrl_dir = Path(CONFIG.storage.xbrl_dir)
    n_companies = len([d for d in xbrl_dir.iterdir() if d.is_dir()]) if xbrl_dir.exists() else 0
    n_files     = sum(len(list(d.glob("*.json"))) for d in xbrl_dir.iterdir()
                      if d.is_dir()) if xbrl_dir.exists() else 0
    db_exists   = Path(CONFIG.storage.fundamental_db).exists()
    price_exists= Path(CONFIG.storage.price_db).exists()

    st.markdown(
        f'<div style="font-size:9px;color:#3d5052;letter-spacing:.1em;'
        f'text-transform:uppercase;margin-bottom:8px">Status</div>'
        f'<div style="font-size:11px;margin-bottom:4px">'
        f'<span style="color:#4ecca3">{"✓" if n_companies>0 else "○"}</span> '
        f'XBRL cache: {n_companies} companies, {n_files} files</div>'
        f'<div style="font-size:11px;margin-bottom:4px">'
        f'<span style="color:{"#4ecca3" if db_exists else "#e05252"}">{"✓" if db_exists else "✗"}</span> '
        f'Fundamental DB</div>'
        f'<div style="font-size:11px">'
        f'<span style="color:{"#4ecca3" if price_exists else "#e05252"}">{"✓" if price_exists else "✗"}</span> '
        f'Price DB</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    if st.button("◈ Open Terminal"):
        st.switch_page("bloom_india/terminal/app.py")

# ── Header ────────────────────────────────────────────────────────────────────

st.markdown(
    '<div style="font-size:18px;font-weight:500;color:#c8d0cc;'
    'padding:8px 0 12px;border-bottom:1px solid #1e2a2c;margin-bottom:16px">'
    '◈ Data Manager</div>',
    unsafe_allow_html=True,
)

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "SETUP", "UNIVERSE", "FETCH XBRL", "BUILD DB", "STATUS"
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — SETUP
# ══════════════════════════════════════════════════════════════════════════════

with tab1:
    st.markdown('<div class="section-head">DATA DIRECTORY</div>', unsafe_allow_html=True)

    col1, col2 = st.columns([3, 1])
    with col1:
        new_base = st.text_input(
            "Base directory (all data stored here)",
            value = str(CONFIG.storage.base_dir),
            key   = "base_dir_input",
        )
    with col2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Apply & Create"):
            import yaml
            config_path = os.environ.get("BLOOM_INDIA_CONFIG")
            if config_path and os.path.exists(config_path):
                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                cfg["storage"]["base_dir"] = new_base
                with open(config_path, "w") as f:
                    yaml.dump(cfg, f, default_flow_style=False)
                st.success(f"Saved. Restart app to apply.")
                log(f"base_dir set to: {new_base}", "ok")

    st.markdown('<div class="section-head">RESOLVED PATHS</div>', unsafe_allow_html=True)

    paths = {
        "base_dir":            CONFIG.storage.base_dir,
        "raw_dir":             CONFIG.storage.raw_dir,
        "xbrl_dir":            CONFIG.storage.xbrl_dir,
        "bhavcopy_dir":        CONFIG.storage.bhavcopy_dir,
        "announce_dates_file": CONFIG.storage.announce_dates_file,
        "db_dir":              CONFIG.storage.db_dir,
        "price_db":            CONFIG.storage.price_db,
        "fundamental_db":      CONFIG.storage.fundamental_db,
        "corp_actions_db":     CONFIG.storage.corp_actions_db,
    }

    html = ""
    for k, v in paths.items():
        exists = Path(v).exists()
        icon   = "✓" if exists else "○"
        color  = "#4ecca3" if exists else "#3d5052"
        html  += (f'<div style="display:flex;justify-content:space-between;'
                  f'padding:4px 0;border-bottom:1px solid #141d1e;font-size:11px">'
                  f'<span style="color:#637b7d;width:180px;flex-shrink:0">{k}</span>'
                  f'<span style="color:#c8d0cc;flex:1;font-size:10px">{v}</span>'
                  f'<span style="color:{color};margin-left:8px">{icon}</span>'
                  f'</div>')
    st.markdown(f'<div class="card">{html}</div>', unsafe_allow_html=True)

    if st.button("Create all directories"):
        ensure_dirs()
        log("All directories created", "ok")
        st.success("Directories created.")
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — UNIVERSE
# ══════════════════════════════════════════════════════════════════════════════

with tab2:
    st.markdown('<div class="section-head">SELECT UNIVERSE</div>', unsafe_allow_html=True)

    universe_choice = st.radio(
        "Index",
        options    = ["NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500", "Custom"],
        index      = ["NIFTY 50","NIFTY 100","NIFTY 200","NIFTY 500","Custom"].index(
                        st.session_state.state.get("universe", "NIFTY 50")
                     ),
        horizontal = True,
        key        = "universe_radio",
    )

    symbols_state = st.session_state.state.get("symbols", [])

    if universe_choice != "Custom":
        col1, col2 = st.columns([1, 3])
        with col1:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button(f"Fetch {universe_choice} constituents"):
                with st.spinner(f"Fetching {universe_choice}..."):
                    try:
                        import requests as _req, time as _t
                        s = _req.Session()
                        s.headers.update({
                            "User-Agent": CONFIG.fetch.user_agent,
                            "Referer":    "https://www.nseindia.com",
                        })
                        s.get("https://www.nseindia.com", timeout=10)
                        _t.sleep(1)
                        idx = universe_choice.replace(" ", "%20")
                        r   = s.get(
                            f"https://www.nseindia.com/api/equity-stockIndices?index={idx}",
                            timeout=15,
                        )
                        data = r.json().get("data", [])
                        syms = [d["symbol"] for d in data
                                if d.get("symbol") and d["symbol"] != universe_choice]

                        st.session_state.state["universe"] = universe_choice
                        st.session_state.state["symbols"]  = syms
                        save_state(st.session_state.state)

                        # Save to file
                        lists_dir = Path(CONFIG.storage.raw_dir) / "universe"
                        lists_dir.mkdir(parents=True, exist_ok=True)
                        fname = universe_choice.lower().replace(" ","") + ".json"
                        with open(lists_dir / fname, "w") as f:
                            json.dump(syms, f, indent=2)

                        log(f"Fetched {len(syms)} symbols for {universe_choice}", "ok")
                        st.success(f"Got {len(syms)} symbols")
                        st.rerun()

                    except Exception as e:
                        log(f"Failed to fetch {universe_choice}: {e}", "err")
                        st.error(str(e))
    else:
        custom_syms = st.text_area(
            "Enter symbols (comma or newline separated)",
            value = ", ".join(symbols_state),
            height = 120,
        )
        if st.button("Save custom list"):
            syms = [s.strip().upper() for s in
                    custom_syms.replace("\n",",").split(",") if s.strip()]
            st.session_state.state["universe"] = "Custom"
            st.session_state.state["symbols"]  = syms
            save_state(st.session_state.state)
            st.success(f"Saved {len(syms)} symbols")

    # Show current list
    symbols_state = st.session_state.state.get("symbols", [])
    if symbols_state:
        st.markdown(
            f'<div class="section-head">CURRENT UNIVERSE — '
            f'{st.session_state.state.get("universe","?")} '
            f'({len(symbols_state)} symbols)</div>',
            unsafe_allow_html=True,
        )
        # Show as grid
        cols = st.columns(8)
        for i, sym in enumerate(symbols_state):
            xbrl_sym = Path(CONFIG.storage.xbrl_dir) / sym
            n_files  = len(list(xbrl_sym.glob("*.json"))) if xbrl_sym.exists() else 0
            color    = "#4ecca3" if n_files > 0 else "#3d5052"
            cols[i % 8].markdown(
                f'<div style="font-size:10px;color:{color};padding:2px 0;'
                f'text-align:center">{sym}<br>'
                f'<span style="font-size:8px;color:#3d5052">{n_files}f</span></div>',
                unsafe_allow_html=True,
            )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — FETCH XBRL
# ══════════════════════════════════════════════════════════════════════════════

with tab3:
    symbols = st.session_state.state.get("symbols", [])

    if not symbols:
        st.warning("No universe selected. Go to UNIVERSE tab first.")
    else:
        # Status overview
        xbrl_dir = Path(CONFIG.storage.xbrl_dir)
        progress = {}
        for sym in symbols:
            sym_dir = xbrl_dir / sym
            n = len(list(sym_dir.glob("*.json"))) if sym_dir.exists() else 0
            progress[sym] = n

        done    = sum(1 for v in progress.values() if v > 0)
        pending = len(symbols) - done
        total_f = sum(progress.values())

        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f'<div class="stat"><div class="stat-label">Total symbols</div>'
                    f'<div class="stat-val">{len(symbols)}</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="stat"><div class="stat-label">Downloaded</div>'
                    f'<div class="stat-val" style="color:#4ecca3">{done}</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="stat"><div class="stat-label">Pending</div>'
                    f'<div class="stat-val" style="color:#e09a52">{pending}</div></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="stat"><div class="stat-label">Total files</div>'
                    f'<div class="stat-val">{total_f}</div></div>', unsafe_allow_html=True)

        st.markdown("")
        st.progress(done / len(symbols) if symbols else 0)

        st.markdown('<div class="section-head">OPTIONS</div>', unsafe_allow_html=True)
        col1, col2, col3 = st.columns(3)
        with col1:
            fetch_mode = st.radio(
                "Fetch mode",
                ["Skip existing (resume)", "Re-download all (clean)"],
                key = "fetch_mode",
            )
        with col2:
            workers = st.slider("Parallel workers", 1, 10, 6, 1,
                                help="NSE blocks >8-10 concurrent. 6 is safe.")
        with col3:
            jitter = st.slider("Jitter per request (sec)", 0.2, 3.0, 0.8, 0.1,
                               help="Random delay between requests per worker.")

        force = "Re-download" in fetch_mode

        st.markdown('<div class="section-head">SYMBOL STATUS</div>', unsafe_allow_html=True)

        # Grid view
        grid_cols = st.columns(10)
        for i, sym in enumerate(symbols):
            n = progress.get(sym, 0)
            if n > 20:
                color, status = "#4ecca3", "✓"
            elif n > 0:
                color, status = "#e09a52", "~"
            else:
                color, status = "#3d5052", "○"
            grid_cols[i % 10].markdown(
                f'<div style="font-size:9px;color:{color};text-align:center;'
                f'padding:2px;border:1px solid #141d1e;border-radius:2px;margin:1px">'
                f'{sym[:6]}<br>{status}{n}</div>',
                unsafe_allow_html=True,
            )

        st.markdown("")

        # Target symbols
        pending_syms = [s for s in symbols if progress.get(s, 0) == 0] if not force else symbols
        btn_label    = f"▶ Fetch {len(pending_syms) if not force else len(symbols)} symbols  ({workers} workers)"

        if st.button(btn_label, key="fetch_btn"):
            import requests as _req
            import threading
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from bloom_india.data.fetch.xbrl import fetch_xbrl_filings, download_xbrl

            target   = symbols if force else pending_syms
            _xbrl    = str(CONFIG.storage.xbrl_dir)
            _jitter  = jitter
            _force   = force

            log(f"Starting parallel XBRL fetch — {len(target)} symbols, {workers} workers", "ok")

            prog_bar  = st.progress(0.0)
            status_ph = st.empty()
            log_ph    = st.empty()

            # Thread-local sessions — one per worker
            _local      = threading.local()
            _print_lock = threading.Lock()
            _counter    = {"done": 0, "errors": 0}

            def _make_sess():
                s = _req.Session()
                s.headers.update({
                    "User-Agent": CONFIG.fetch.user_agent,
                    "Referer":    "https://www.nseindia.com",
                })
                try:
                    s.get("https://www.nseindia.com", timeout=10)
                    time.sleep(random.uniform(0.5, 1.5))
                except: pass
                return s

            def _get_sess():
                if not hasattr(_local, "sess"):
                    _local.sess = _make_sess()
                return _local.sess

            def _fetch_one(sym: str):
                time.sleep(random.uniform(0.1, _jitter))
                try:
                    sess    = _get_sess()
                    filings = fetch_xbrl_filings(sym, session=sess)
                    if not filings:
                        return sym, 0, "no_filings"
                    new   = download_xbrl(sym, filings, xbrl_dir=_xbrl,
                                          session=sess, force=_force)
                    total = len(list((Path(_xbrl)/sym).glob("*.json")))
                    return sym, total, "ok"
                except Exception as e:
                    err = str(e)
                    # Back off and retry once on rate limit
                    if any(x in err for x in ["429","403","rate","blocked"]):
                        time.sleep(random.uniform(10, 20))
                        try:
                            _local.sess = _make_sess()
                            sess        = _get_sess()
                            filings     = fetch_xbrl_filings(sym, session=sess)
                            new         = download_xbrl(sym, filings, xbrl_dir=_xbrl,
                                                        session=sess, force=_force)
                            total       = len(list((Path(_xbrl)/sym).glob("*.json")))
                            return sym, total, "ok_retry"
                        except Exception as e2:
                            return sym, 0, f"err:{str(e2)[:40]}"
                    return sym, 0, f"err:{err[:40]}"

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_fetch_one, sym): sym for sym in target}
                for future in as_completed(futures):
                    sym, n, status = future.result()
                    _counter["done"] += 1
                    if "err" in status:
                        _counter["errors"] += 1
                        log(f"  ✗ {sym:<15} {status}", "err")
                    else:
                        log(f"  ✓ {sym:<15} {n:>3} files  [{status}]", "ok")

                    st.session_state.state["xbrl_progress"][sym] = n
                    pct = _counter["done"] / len(target)
                    prog_bar.progress(pct)
                    status_ph.markdown(
                        f'<div style="font-size:11px;color:#637b7d">'
                        f'{_counter["done"]}/{len(target)} done  '
                        f'({_counter["errors"]} errors)  '
                        f'{pct*100:.1f}%</div>',
                        unsafe_allow_html=True,
                    )
                    log_ph.markdown(
                        '<div class="log-box">' +
                        "<br>".join(st.session_state.log[-25:]) +
                        '</div>',
                        unsafe_allow_html=True,
                    )

            st.session_state.state["last_xbrl_fetch"] = datetime.now().isoformat()
            save_state(st.session_state.state)
            log(f"Fetch complete — {_counter['done']} symbols, {_counter['errors']} errors", "ok")
            st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — BUILD DB
# ══════════════════════════════════════════════════════════════════════════════

with tab4:
    symbols = st.session_state.state.get("symbols", [])

    if not symbols:
        st.warning("No universe selected. Go to UNIVERSE tab first.")
    else:
        st.markdown('<div class="section-head">BUILD PIPELINE</div>', unsafe_allow_html=True)

        steps = [
            ("Parse XBRL cache",          "parse"),
            ("Fix cumulative YTD values", "cumfix"),
            ("Fix cumulative EPS",        "epsfix"),
            ("Fetch announcement dates",  "dates"),
            ("Merge announcement dates",  "merge"),
            ("Compute factors",           "factors"),
            ("Save to parquet + CSV",     "save"),
        ]

        for label, key in steps:
            done = st.session_state.get(f"step_{key}", False)
            icon = "✓" if done else "○"
            color = "#4ecca3" if done else "#3d5052"
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:10px;'
                f'padding:5px 0;border-bottom:1px solid #141d1e;font-size:11px">'
                f'<span style="color:{color};font-size:14px">{icon}</span>'
                f'<span style="color:#637b7d">{label}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

        st.markdown("")
        col1, col2 = st.columns(2)
        with col1:
            fetch_dates_opt = st.checkbox("Fetch announcement dates from BSE", value=True)
        with col2:
            force_rebuild   = st.checkbox("Force rebuild even if DB exists", value=False)

        if st.button("▶ Build Fundamental Database", key="build_btn"):
            from bloom_india.data.process.xbrl_parse import parse_xbrl_cache
            from bloom_india.data.process.cumulative_fix import fix_cumulative, fix_eps
            from bloom_india.data.process.factors import compute_factors
            import pandas as pd

            log_ph   = st.empty()
            prog_bar = st.progress(0.0)

            def update_log():
                log_ph.markdown(
                    '<div class="log-box">' +
                    "<br>".join(st.session_state.log[-30:]) +
                    '</div>', unsafe_allow_html=True,
                )

            try:
                # Parse
                log("Parsing XBRL cache...", "ok")
                update_log(); prog_bar.progress(0.1)
                df = parse_xbrl_cache(
                    xbrl_dir = str(CONFIG.storage.xbrl_dir),
                    symbols  = symbols,
                    verbose  = False,
                )
                log(f"Parsed: {len(df)} rows, {df['symbol'].nunique()} symbols, {len(df.columns)} cols", "ok")
                st.session_state["step_parse"] = True

                # Fix cumulative
                update_log(); prog_bar.progress(0.25)
                log("Fixing cumulative YTD values...", "ok")
                df = fix_cumulative(df)
                st.session_state["step_cumfix"] = True

                # Fix EPS
                update_log(); prog_bar.progress(0.35)
                log("Fixing cumulative EPS...", "ok")
                df = fix_eps(df)
                st.session_state["step_epsfix"] = True

                # Announce dates
                update_log(); prog_bar.progress(0.45)
                if fetch_dates_opt:
                    log("Fetching announcement dates from BSE...", "ok")
                    from bloom_india.data.fetch.announcements import fetch_and_save_all as _fd
                    _fd(
                        symbols  = symbols,
                        out_file = str(CONFIG.storage.announce_dates_file),
                        force    = False,
                        verbose  = False,
                    )
                    log("Announcement dates fetched", "ok")
                st.session_state["step_dates"] = True

                # Merge dates
                update_log(); prog_bar.progress(0.6)
                log("Merging announcement dates...", "ok")
                ann_file = Path(CONFIG.storage.announce_dates_file)
                if ann_file.exists():
                    from bloom_india.data.fetch.announcements import load_announce_dates
                    dates = load_announce_dates(str(ann_file))
                    def _lookup(row):
                        return dates.get(row["symbol"], {}).get(row["quarter_label"])
                    df["announce_date"] = df.apply(_lookup, axis=1)
                    df["announce_date"] = pd.to_datetime(df["announce_date"], errors="coerce")
                    mask = df["announce_date"].isna()
                    df.loc[mask, "announce_date"] = df.loc[mask, "period_end"] + pd.Timedelta(days=21)
                    df["announce_date_estimated"] = mask
                    filled = df["announce_date"].notna().sum()
                    log(f"Dates merged: {filled}/{len(df)} ({filled/len(df)*100:.1f}%)", "ok")
                else:
                    df["announce_date"] = df["period_end"] + pd.Timedelta(days=21)
                    df["announce_date_estimated"] = True
                    log("No announce dates file — using period_end+21d estimate", "warn")
                st.session_state["step_merge"] = True

                # Compute factors
                update_log(); prog_bar.progress(0.75)
                log("Computing factors...", "ok")
                df = compute_factors(df)
                st.session_state["step_factors"] = True

                # Save
                update_log(); prog_bar.progress(0.9)
                log("Saving databases...", "ok")
                out_par = Path(CONFIG.storage.fundamental_db)
                out_csv = out_par.with_suffix(".csv")
                out_par.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(str(out_par), index=False)
                df.to_csv(str(out_csv), index=False)
                log(f"Saved parquet: {out_par}", "ok")
                log(f"Saved CSV:     {out_csv}", "ok")
                log(f"Final shape:   {df.shape}", "ok")
                st.session_state["step_save"] = True

                st.session_state.state["last_db_build"] = datetime.now().isoformat()
                save_state(st.session_state.state)

                prog_bar.progress(1.0)
                update_log()
                st.success(f"✓ Database built: {len(df)} rows, {len(df.columns)} columns, {df['symbol'].nunique()} symbols")

                # Refresh API cache
                try:
                    from bloom_india.data.api.fundamentals import refresh_cache
                    refresh_cache()
                    log("API cache refreshed", "ok")
                except: pass

            except Exception as e:
                log(f"ERROR: {e}", "err")
                update_log()
                st.error(str(e))

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — STATUS
# ══════════════════════════════════════════════════════════════════════════════

with tab5:
    st.markdown('<div class="section-head">DATABASE STATUS</div>', unsafe_allow_html=True)

    # Fundamental DB
    fund_path = Path(CONFIG.storage.fundamental_db)
    if fund_path.exists():
        df_stat = pd.read_parquet(str(fund_path))
        df_stat["period_end"] = pd.to_datetime(df_stat["period_end"], errors="coerce")
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f'<div class="stat"><div class="stat-label">Rows</div>'
                    f'<div class="stat-val">{len(df_stat):,}</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="stat"><div class="stat-label">Symbols</div>'
                    f'<div class="stat-val">{df_stat["symbol"].nunique()}</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="stat"><div class="stat-label">Columns</div>'
                    f'<div class="stat-val">{len(df_stat.columns)}</div></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="stat"><div class="stat-label">Date range</div>'
                    f'<div class="stat-val" style="font-size:12px">'
                    f'{df_stat["period_end"].min().strftime("%Y-%m") if df_stat["period_end"].notna().any() else "—"}'
                    f' → {df_stat["period_end"].max().strftime("%Y-%m") if df_stat["period_end"].notna().any() else "—"}'
                    f'</div></div>', unsafe_allow_html=True)

        st.markdown('<div class="section-head">PER-SYMBOL COVERAGE</div>', unsafe_allow_html=True)
        sym_stats = (df_stat.groupby("symbol").agg(
            quarters   = ("quarter_label","count"),
            first_date = ("period_end","min"),
            last_date  = ("period_end","max"),
            nulls_rev  = ("revenue", lambda x: x.isna().sum()),
            nulls_pat  = ("pat",     lambda x: x.isna().sum()),
        ).reset_index().sort_values("quarters", ascending=False))

        sym_stats["first_date"] = sym_stats["first_date"].dt.strftime("%Y-%m")
        sym_stats["last_date"]  = sym_stats["last_date"].dt.strftime("%Y-%m")
        st.dataframe(sym_stats, use_container_width=True, height=400)

    else:
        st.markdown(
            '<div style="color:#3d5052;padding:20px">No fundamental database found. '
            'Go to BUILD DB tab to create it.</div>',
            unsafe_allow_html=True,
        )

    # Log
    st.markdown('<div class="section-head">SESSION LOG</div>', unsafe_allow_html=True)
    if st.session_state.log:
        st.markdown(
            '<div class="log-box">' +
            "<br>".join(st.session_state.log[-50:]) +
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div class="log-box dim">No activity yet.</div>', unsafe_allow_html=True)

    if st.button("Clear log"):
        st.session_state.log = []
        st.rerun()