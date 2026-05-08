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
            force_rebuild = st.checkbox("Force rebuild even if DB exists", value=False)
        with col2:
            st.markdown(
                '<div style="font-size:10px;color:#637b7d;margin-top:8px">'
                'Announcement dates fetched in background after build</div>',
                unsafe_allow_html=True,
            )

        if st.button("▶ Build Fundamental Database", key="build_btn"):
            from bloom_india.data.process.xbrl_parse import parse_xbrl_cache
            from bloom_india.data.process.cumulative_fix import fix_cumulative, fix_eps
            from bloom_india.data.process.factors import compute_factors
            import pandas as pd
            import threading

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
                update_log(); prog_bar.progress(0.3)
                log("Fixing cumulative YTD values...", "ok")
                df = fix_cumulative(df)
                st.session_state["step_cumfix"] = True

                # Fix EPS
                update_log(); prog_bar.progress(0.45)
                log("Fixing cumulative EPS...", "ok")
                df = fix_eps(df)
                st.session_state["step_epsfix"] = True

                # ── Announce dates ────────────────────────────────────────────
                # Use existing file if available, else estimate period_end+21d
                # Fetch from BSE happens in background thread — non-blocking
                update_log(); prog_bar.progress(0.55)
                ann_file = Path(CONFIG.storage.announce_dates_file)

                if ann_file.exists():
                    from bloom_india.data.fetch.announcements import load_announce_dates
                    dates = load_announce_dates(str(ann_file))
                    def _lookup(row):
                        return dates.get(row["symbol"], {}).get(row["quarter_label"])
                    df["announce_date"] = df.apply(_lookup, axis=1)
                    df["announce_date"] = pd.to_datetime(df["announce_date"], errors="coerce")
                    mask = df["announce_date"].isna()
                    df.loc[mask, "announce_date"] = (
                        df.loc[mask, "period_end"] + pd.Timedelta(days=21)
                    )
                    df["announce_date_estimated"] = mask
                    filled = (~mask).sum()
                    log(f"Dates merged: {filled}/{len(df)} from BSE "
                        f"({filled/len(df)*100:.1f}% filled)", "ok")
                else:
                    # Estimate all — BSE fetch runs in background
                    df["announce_date"]           = df["period_end"] + pd.Timedelta(days=21)
                    df["announce_date_estimated"] = True
                    log("No announce_dates.json yet — using period_end+21d for all", "warn")
                    log("BSE dates will fetch in background — rebuild DB after it completes", "warn")

                    # Launch background fetch
                    def _bg_fetch():
                        try:
                            from bloom_india.data.fetch.announcements import fetch_and_save_all
                            fetch_and_save_all(
                                symbols  = symbols,
                                out_file = str(ann_file),
                                force    = False,
                                verbose  = False,
                            )
                        except Exception as e:
                            pass  # silent — user can rebuild after
                    threading.Thread(target=_bg_fetch, daemon=True).start()
                    log("BSE date fetch started in background thread", "dim")

                st.session_state["step_dates"] = True
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

        # ── Separate: fetch announce dates ────────────────────────────────────
        st.markdown('<div class="section-head">ANNOUNCEMENT DATES  (optional — improves PIT accuracy)</div>',
                    unsafe_allow_html=True)

        ann_file   = Path(CONFIG.storage.announce_dates_file)
        ann_exists = ann_file.exists()

        if ann_exists:
            with open(ann_file) as f:
                ann_data = json.load(f)
            total_dates = sum(len(v) for v in ann_data.values())
            st.markdown(
                f'<div style="font-size:11px;color:#4ecca3;margin-bottom:8px">'
                f'✓ {len(ann_data)} symbols, {total_dates} announcement dates cached</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="font-size:11px;color:#e09a52;margin-bottom:8px">'
                '⚠ Not fetched yet — using period_end+21d estimates</div>',
                unsafe_allow_html=True,
            )

        col_a, col_b = st.columns([1, 3])
        with col_a:
            workers_ann = st.slider("Workers", 1, 8, 4, key="ann_workers")
        with col_b:
            st.markdown(
                '<div style="font-size:10px;color:#3d5052;margin-top:12px">'
                'Fetches BSE result announcement dates for all symbols in parallel.<br>'
                'After completion, rebuild DB to apply exact dates.</div>',
                unsafe_allow_html=True,
            )

        if st.button("▶ Fetch Announcement Dates from BSE", key="fetch_ann_btn"):  # noqa
            import threading
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from bloom_india.data.fetch.announcements import fetch_announce_dates

            existing = {}
            if ann_file.exists():
                with open(ann_file) as f:
                    existing = json.load(f)

            pending_ann = [s for s in symbols if s not in existing]
            log(f"Fetching BSE dates for {len(pending_ann)} symbols "
                f"({len(existing)} already cached)", "ok")

            prog_ann = st.progress(0.0)
            log_ann  = st.empty()
            counter  = {"done": 0}
            _lock    = threading.Lock()

            def _fetch_dates_one(sym):
                time.sleep(random.uniform(0.2, 1.0))
                try:
                    dates = fetch_announce_dates(sym)
                    return sym, dates, None
                except Exception as e:
                    return sym, {}, str(e)

            with ThreadPoolExecutor(max_workers=workers_ann) as pool:
                futures = {pool.submit(_fetch_dates_one, s): s for s in pending_ann}
                for future in as_completed(futures):
                    sym, dates, err = future.result()
                    with _lock:
                        counter["done"] += 1
                        existing[sym] = dates
                        if not err:
                            log(f"  ✓ {sym:<15} {len(dates)} quarters", "ok")
                        else:
                            log(f"  ✗ {sym:<15} {err[:40]}", "err")

                        # Save after every 10 symbols
                        if counter["done"] % 10 == 0:
                            ann_file.parent.mkdir(parents=True, exist_ok=True)
                            with open(ann_file, "w") as f:
                                json.dump(existing, f, indent=2)

                        prog_ann.progress(counter["done"] / max(len(pending_ann), 1))
                        log_ann.markdown(
                            '<div class="log-box">' +
                            "<br>".join(st.session_state.log[-20:]) +
                            '</div>', unsafe_allow_html=True,
                        )

            # Final save
            ann_file.parent.mkdir(parents=True, exist_ok=True)
            with open(ann_file, "w") as f:
                json.dump(existing, f, indent=2)

            total = sum(len(v) for v in existing.values())
            log(f"Done — {len(existing)} symbols, {total} dates. Rebuild DB to apply.", "ok")
            st.success(f"✓ {len(existing)} symbols, {total} announcement dates saved.")
            st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# PRICE DB — inside TAB 4 continued
# ══════════════════════════════════════════════════════════════════════════════

with tab4:
    st.markdown("---")
    st.markdown('<div class="section-head">PRICE DATABASE</div>', unsafe_allow_html=True)

    price_path   = Path(CONFIG.storage.price_db)
    price_exists = price_path.exists()

    if price_exists:
        _pstat = pd.read_parquet(str(price_path), columns=["Date","Symbol"])
        _pstat["Date"] = pd.to_datetime(_pstat["Date"])
        c1, c2, c3 = st.columns(3)
        c1.markdown(f'<div class="stat"><div class="stat-label">Rows</div>'
                    f'<div class="stat-val">{len(_pstat):,}</div></div>',
                    unsafe_allow_html=True)
        c2.markdown(f'<div class="stat"><div class="stat-label">Symbols</div>'
                    f'<div class="stat-val">{_pstat["Symbol"].nunique()}</div></div>',
                    unsafe_allow_html=True)
        c3.markdown(f'<div class="stat"><div class="stat-label">Date range</div>'
                    f'<div class="stat-val" style="font-size:12px">'
                    f'{_pstat["Date"].min().date()} → {_pstat["Date"].max().date()}'
                    f'</div></div>', unsafe_allow_html=True)
        del _pstat
    else:
        st.markdown(
            '<div style="color:#e09a52;font-size:11px;margin-bottom:8px">'
            '⚠ Price DB not built yet</div>',
            unsafe_allow_html=True,
        )

    # Options
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        price_from = st.text_input("From date", value="2018-01-01", key="price_from")
    with col2:
        price_to   = st.text_input("To date",
                                    value=datetime.now().strftime("%Y-%m-%d"),
                                    key="price_to")
    with col3:
        price_workers = st.slider("Workers", 1, 8, 5, key="price_workers",
                                   help="Parallel download threads (5 is safe for NSE)")
    with col4:
        force_price = st.checkbox("Force full rebuild", value=False, key="force_price",
                                   help="Re-download all dates even if raw DB exists")

    # Show what exists
    raw_path = Path(CONFIG.storage.price_raw_db)
    adj_path = Path(CONFIG.storage.price_db)

    raw_exists = raw_path.exists()
    adj_exists = adj_path.exists()

    col_info1, col_info2 = st.columns(2)
    with col_info1:
        if raw_exists:
            _r = pd.read_parquet(str(raw_path), columns=["Date"])
            _r["Date"] = pd.to_datetime(_r["Date"])
            st.markdown(
                f'<div style="font-size:10px;color:#4ecca3">✓ Raw DB: {len(_r):,} rows  '
                f'{_r["Date"].min().date()} → {_r["Date"].max().date()}</div>',
                unsafe_allow_html=True,
            )
            del _r
        else:
            st.markdown('<div style="font-size:10px;color:#e09a52">⚠ No raw DB yet</div>',
                        unsafe_allow_html=True)

    with col_info2:
        if adj_exists:
            _a = pd.read_parquet(str(adj_path), columns=["Date"])
            _a["Date"] = pd.to_datetime(_a["Date"])
            is_adj = "IsAdjusted" in pd.read_parquet(str(adj_path)).columns
            st.markdown(
                f'<div style="font-size:10px;color:#4ecca3">✓ Adjusted DB: {len(_a):,} rows  '
                f'{"corp-action adjusted" if is_adj else "raw copy"}</div>',
                unsafe_allow_html=True,
            )
            del _a
        else:
            st.markdown('<div style="font-size:10px;color:#e09a52">⚠ No adjusted DB yet</div>',
                        unsafe_allow_html=True)

    st.markdown(
        '<div style="font-size:9px;color:#3d5052;margin:6px 0 10px">'
        'prices_raw.parquet = untouched bhavcopy  ·  '
        'prices.parquet = adjusted version built from raw</div>',
        unsafe_allow_html=True,
    )

    col_a, col_b, col_c = st.columns(3)

    # ── Button 1: Fetch raw ───────────────────────────────────────────────────
    with col_a:
        if st.button("▶ Fetch Raw Prices", key="build_price_btn"):
            from bloom_india.data.fetch.bhavcopy import fetch_bhavcopy_range

            syms = st.session_state.state.get("symbols", [])
            if not syms:
                st.error("No universe selected — go to UNIVERSE tab first.")
            else:
                price_log = st.empty()
                price_bar = st.progress(0.0)

                def _upd_plog():
                    price_log.markdown(
                        '<div class="log-box">' +
                        "<br>".join(st.session_state.log[-20:]) +
                        '</div>', unsafe_allow_html=True,
                    )

                try:
                    # Decide fetch range
                    if raw_exists and not force_price:
                        existing_raw = pd.read_parquet(str(raw_path))
                        existing_raw["Date"] = pd.to_datetime(existing_raw["Date"])
                        last_dt    = existing_raw["Date"].max()
                        fetch_from = (last_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                        log(f"Raw DB up to {last_dt.date()} — fetching from {fetch_from}", "ok")
                    else:
                        existing_raw = pd.DataFrame()
                        fetch_from   = price_from
                        if force_price:
                            log("Force rebuild — fetching all dates from scratch", "warn")

                    if fetch_from > price_to:
                        log("Raw price DB already up to date", "ok")
                        st.success("Already up to date.")
                    else:
                        log(f"Fetching bhavcopy: {fetch_from} → {price_to} "
                            f"({price_workers} workers)", "ok")
                        _upd_plog(); price_bar.progress(0.05)

                        new_raw = fetch_bhavcopy_range(
                            from_date = fetch_from,
                            to_date   = price_to,
                            symbols   = syms,
                            verbose   = False,
                            workers   = price_workers,
                        )
                        log(f"Downloaded: {len(new_raw):,} new rows", "ok")
                        _upd_plog(); price_bar.progress(0.7)

                        # Combine with existing raw
                        if not existing_raw.empty and not force_price:
                            combined_raw = pd.concat(
                                [existing_raw, new_raw], ignore_index=True
                            )
                            combined_raw = (combined_raw
                                           .drop_duplicates(subset=["Date","Symbol"])
                                           .sort_values(["Date","Symbol"])
                                           .reset_index(drop=True))
                        else:
                            combined_raw = new_raw.sort_values(
                                ["Date","Symbol"]
                            ).reset_index(drop=True)

                        # Save RAW — never adjusted
                        raw_path.parent.mkdir(parents=True, exist_ok=True)
                        combined_raw.to_parquet(str(raw_path), index=False)
                        log(f"Raw DB saved: {raw_path}", "ok")
                        log(f"Shape: {combined_raw.shape}  "
                            f"Symbols: {combined_raw['Symbol'].nunique()}", "ok")
                        _upd_plog(); price_bar.progress(1.0)

                        st.session_state.state["last_price_fetch"] = datetime.now().isoformat()
                        save_state(st.session_state.state)
                        st.success(f"✓ Raw prices saved: {len(combined_raw):,} rows  "
                                   f"Now click '▶ Apply Adjustment'")
                        st.rerun()

                except Exception as e:
                    log(f"ERROR: {e}", "err")
                    _upd_plog()
                    st.error(str(e))

    # ── Button 2: Apply adjustment from raw ───────────────────────────────────
    with col_b:
        if st.button("▶ Apply Adjustment", key="adj_only_btn",
                     disabled=not raw_exists):
            from bloom_india.data.process.adjust import adjust_panel

            adj_log = st.empty()
            adj_bar = st.progress(0.0)

            def _upd_alog():
                adj_log.markdown(
                    '<div class="log-box">' +
                    "<br>".join(st.session_state.log[-20:]) +
                    '</div>', unsafe_allow_html=True,
                )

            try:
                # Always read from RAW — never from adjusted
                log("Loading raw price DB...", "ok")
                raw_df = pd.read_parquet(str(raw_path))
                raw_df["Date"] = pd.to_datetime(raw_df["Date"])
                log(f"Raw loaded: {len(raw_df):,} rows", "ok")
                _upd_alog(); adj_bar.progress(0.15)

                log("Fetching corp actions + applying backward adjustment...", "ok")
                _upd_alog()
                adj_df = adjust_panel(raw_df, workers=8, verbose=False)
                adj_bar.progress(0.85)

                # Save to prices.parquet (separate from raw)
                adj_path.parent.mkdir(parents=True, exist_ok=True)
                adj_df.to_parquet(str(adj_path), index=False)
                log(f"Adjusted DB saved: {adj_path}", "ok")
                log(f"Shape: {adj_df.shape}", "ok")

                # Verify a known split
                maz = adj_df[adj_df["Symbol"]=="MAZDOCK"].sort_values("Date")
                oct20 = maz[maz["Date"]=="2020-10-12"]["Close"]
                if not oct20.empty:
                    log(f"MAZDOCK Oct 2020 adjusted: {oct20.values[0]:.2f} "
                        f"(raw was 171.95, expected ~85.97)", "ok")

                _upd_alog(); adj_bar.progress(1.0)

                try:
                    from bloom_india.data.api.prices import refresh_cache as _rp
                    _rp()
                    log("Price API cache refreshed", "ok")
                except: pass

                st.success("✓ Adjustment applied from raw. prices.parquet updated.")
                st.rerun()

            except Exception as e:
                log(f"ERROR: {e}", "err")
                _upd_alog()
                st.error(str(e))

    # ── Button 3: Fetch + Adjust in one shot ──────────────────────────────────
    with col_c:
        if st.button("▶ Fetch + Adjust (full)", key="fetch_adj_btn"):
            st.info("Click '▶ Fetch Raw Prices' first, then '▶ Apply Adjustment'. "
                    "Keeping steps separate prevents double-adjustment.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — STATUS
# ══════════════════════════════════════════════════════════════════════════════

with tab5:
    # ── Fundamental DB ────────────────────────────────────────────────────────
    st.markdown('<div class="section-head">FUNDAMENTAL DATABASE</div>', unsafe_allow_html=True)
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
        st.dataframe(sym_stats, use_container_width=True, height=320)
    else:
        st.markdown('<div style="color:#3d5052;padding:16px">No fundamental DB — go to BUILD DB tab.</div>',
                    unsafe_allow_html=True)

    # ── Price DB ──────────────────────────────────────────────────────────────
    st.markdown('<div class="section-head">PRICE DATABASE</div>', unsafe_allow_html=True)
    raw_path_s = Path(CONFIG.storage.price_raw_db)
    adj_path_s = Path(CONFIG.storage.price_db)

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        st.markdown('<div style="font-size:10px;color:#3d5052;margin-bottom:4px">RAW (prices_raw.parquet)</div>',
                    unsafe_allow_html=True)
        if raw_path_s.exists():
            p_raw = pd.read_parquet(str(raw_path_s), columns=["Date","Symbol"])
            p_raw["Date"] = pd.to_datetime(p_raw["Date"])
            c1, c2 = st.columns(2)
            c1.markdown(f'<div class="stat"><div class="stat-label">Rows</div>'
                        f'<div class="stat-val">{len(p_raw):,}</div></div>', unsafe_allow_html=True)
            c2.markdown(f'<div class="stat"><div class="stat-label">Symbols</div>'
                        f'<div class="stat-val">{p_raw["Symbol"].nunique()}</div></div>', unsafe_allow_html=True)
            st.markdown(
                f'<div style="font-size:10px;color:#637b7d">'
                f'{p_raw["Date"].min().date()} → {p_raw["Date"].max().date()}</div>',
                unsafe_allow_html=True,
            )
            del p_raw
        else:
            st.markdown('<div style="color:#e09a52;font-size:11px">⚠ Not built yet</div>',
                        unsafe_allow_html=True)

    with col_s2:
        st.markdown('<div style="font-size:10px;color:#3d5052;margin-bottom:4px">ADJUSTED (prices.parquet)</div>',
                    unsafe_allow_html=True)
        if adj_path_s.exists():
            p_adj = pd.read_parquet(str(adj_path_s))
            p_adj["Date"] = pd.to_datetime(p_adj["Date"])
            is_adj = "IsAdjusted" in p_adj.columns
            adj_pct = p_adj["IsAdjusted"].mean()*100 if is_adj else 0
            c1, c2 = st.columns(2)
            c1.markdown(f'<div class="stat"><div class="stat-label">Rows</div>'
                        f'<div class="stat-val">{len(p_adj):,}</div></div>', unsafe_allow_html=True)
            c2.markdown(f'<div class="stat"><div class="stat-label">Symbols</div>'
                        f'<div class="stat-val">{p_adj["Symbol"].nunique()}</div></div>', unsafe_allow_html=True)
            adj_color = "#4ecca3" if is_adj else "#e09a52"
            st.markdown(
                f'<div style="font-size:10px;color:{adj_color}">'
                f'{"✓ Corp-action adjusted" if is_adj else "⚠ Not adjusted"} '
                f'{"("+str(round(adj_pct,1))+"%)" if is_adj else ""}</div>',
                unsafe_allow_html=True,
            )
            del p_adj
        else:
            st.markdown('<div style="color:#e09a52;font-size:11px">⚠ Not built yet — run Apply Adjustment</div>',
                        unsafe_allow_html=True)

    # ── XBRL cache ────────────────────────────────────────────────────────────
    st.markdown('<div class="section-head">XBRL CACHE</div>', unsafe_allow_html=True)
    xbrl_p = Path(CONFIG.storage.xbrl_dir)
    if xbrl_p.exists():
        sym_dirs = [d for d in xbrl_p.iterdir() if d.is_dir()]
        n_files  = sum(len(list(d.glob("*.json"))) for d in sym_dirs)
        c1, c2   = st.columns(2)
        c1.markdown(f'<div class="stat"><div class="stat-label">Companies</div>'
                    f'<div class="stat-val">{len(sym_dirs)}</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="stat"><div class="stat-label">XBRL files</div>'
                    f'<div class="stat-val">{n_files:,}</div></div>', unsafe_allow_html=True)

    # ── Announce dates ────────────────────────────────────────────────────────
    st.markdown('<div class="section-head">ANNOUNCEMENT DATES</div>', unsafe_allow_html=True)
    ann_p = Path(CONFIG.storage.announce_dates_file)
    if ann_p.exists():
        with open(ann_p) as f:
            ann_d = json.load(f)
        total_dates = sum(len(v) for v in ann_d.values())
        st.markdown(f'<div style="font-size:11px;color:#4ecca3">'
                    f'✓ {len(ann_d)} symbols, {total_dates:,} announcement dates</div>',
                    unsafe_allow_html=True)
    else:
        st.markdown('<div style="font-size:11px;color:#e09a52">⚠ Not fetched yet</div>',
                    unsafe_allow_html=True)

    # ── Log ───────────────────────────────────────────────────────────────────
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