"""
bloom_india/mf_terminal/app.py
================================
Streamlit MF Dashboard.
Features: Fund Explorer, Fund Detail, Compare Funds,
          P&L Calculator, Watchlist, Fund Screener, Cache Admin.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

st.set_page_config(
    page_title="bloom_india · MF Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .positive { color: #a6e3a1; font-weight: 600; }
    .negative { color: #f38ba8; font-weight: 600; }
    .tag { background:#313244; border-radius:6px; padding:2px 8px;
           font-size:0.75rem; color:#cba6f7; margin-right:4px; }
    [data-testid="stMetricValue"] { font-size: 1.3rem; }
</style>
""", unsafe_allow_html=True)

COLORS = ["#cba6f7","#89b4fa","#a6e3a1","#fab387","#f38ba8",
          "#f9e2af","#74c7ec","#b4befe","#94e2d5","#eba0ac"]


# ── Cached loaders ────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner="Loading scheme list…")
def load_catalogue() -> pd.DataFrame:
    from bloom_india.mf.api.funds import list_funds
    df = list_funds()
    return df if df is not None and not df.empty else pd.DataFrame(
        columns=["scheme_code","scheme_name","fund_house"])

@st.cache_data(ttl=600)
def load_fund_detail(code: int) -> dict:
    from bloom_india.mf.api.funds import get_fund
    return get_fund(code)

@st.cache_data(ttl=600)
def load_nav(code: int) -> pd.DataFrame:
    from bloom_india.mf.api.funds import get_nav_history
    return get_nav_history(code)

@st.cache_data(ttl=300)
def load_enrich(codes: tuple) -> pd.DataFrame:
    from bloom_india.mf.api.funds import get_fund_snapshot
    return get_fund_snapshot(list(codes))


# ── Helpers ───────────────────────────────────────────────────────────────────

def name_to_code(cat: pd.DataFrame, name: str) -> int | None:
    row = cat[cat["scheme_name"] == name]
    return int(row["scheme_code"].iloc[0]) if not row.empty else None

def code_to_name(cat: pd.DataFrame, code: int) -> str:
    row = cat[cat["scheme_code"] == code]
    return row["scheme_name"].iloc[0] if not row.empty else str(code)

def fund_picker(cat: pd.DataFrame, key: str, label: str = "Search fund or AMC") -> tuple:
    """
    Type-first search: search box → filtered dropdown.
    Returns (scheme_name, scheme_code). Never shows full list unprompted.
    """
    search = st.text_input(label, placeholder="Type fund name or AMC…",
                           key=f"fp_{key}_q")
    if not search.strip():
        st.caption("⬆ Type above to search.")
        return None, None

    hits = cat[
        cat["scheme_name"].str.contains(search.strip(), case=False, na=False) |
        cat["fund_house"].str.contains(search.strip(), case=False, na=False)
    ]
    if hits.empty:
        st.warning(f"No funds matching '{search}'.")
        return None, None

    st.caption(f"{len(hits):,} matching funds")
    chosen = st.selectbox("Select fund", hits["scheme_name"].tolist(),
                          key=f"fp_{key}_sel", label_visibility="collapsed")
    return chosen, name_to_code(cat, chosen)

def multi_fund_picker(cat: pd.DataFrame, key: str,
                      max_sel: int = 10, label: str = "Search funds") -> list[tuple]:
    """
    Search → filtered multiselect. Returns list of (name, code) tuples.
    """
    search = st.text_input(label, placeholder="Type fund name or AMC…",
                           key=f"mfp_{key}_q")
    if not search.strip():
        st.caption("⬆ Type above to filter the fund list.")
        return []

    hits = cat[
        cat["scheme_name"].str.contains(search.strip(), case=False, na=False) |
        cat["fund_house"].str.contains(search.strip(), case=False, na=False)
    ]
    if hits.empty:
        st.warning(f"No funds matching '{search}'.")
        return []

    st.caption(f"{len(hits):,} matching — select below")
    chosen = st.multiselect("Select funds", hits["scheme_name"].tolist(),
                            max_selections=max_sel,
                            key=f"mfp_{key}_sel",
                            label_visibility="collapsed")
    return [(n, name_to_code(cat, n)) for n in chosen if name_to_code(cat, n)]

def nav_chart(nav_df, title, color="#cba6f7"):
    if nav_df is None or nav_df.empty:
        st.info("No NAV data.")
        return
    r,g,b = int(color[1:3],16),int(color[3:5],16),int(color[5:7],16)
    fig = go.Figure(go.Scatter(
        x=nav_df["date"], y=nav_df["nav"], mode="lines", name="NAV",
        line=dict(color=color, width=2), fill="tozeroy",
        fillcolor=f"rgba({r},{g},{b},0.07)",
    ))
    fig.update_layout(title=title, paper_bgcolor="#1e1e2e", plot_bgcolor="#1e1e2e",
                      font=dict(color="#cdd6f4"),
                      xaxis=dict(gridcolor="#313244"),
                      yaxis=dict(gridcolor="#313244", title="NAV (₹)"),
                      hovermode="x unified", margin=dict(l=0,r=0,t=40,b=0))
    st.plotly_chart(fig, use_container_width=True)

def analytics_strip(nav_df, from_date=None, to_date=None, rf=0.065):
    from bloom_india.mf.analytics.metrics import full_metrics
    df = nav_df.copy() if nav_df is not None else pd.DataFrame()
    if from_date and not df.empty:
        df = df[df["date"] >= pd.Timestamp(from_date)]
    if to_date and not df.empty:
        df = df[df["date"] <= pd.Timestamp(to_date)]
    m  = full_metrics(df, risk_free=rf)
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Ann. Return",
              f"{m['annualised_return']*100:+.2f}%" if m['annualised_return'] else "—")
    c2.metric("Volatility",
              f"{m['annualised_volatility']*100:.2f}%" if m['annualised_volatility'] else "—")
    c3.metric("Sharpe Ratio",
              f"{m['sharpe_ratio']:.3f}" if m['sharpe_ratio'] else "—")
    c4.metric("Max Drawdown",
              f"{m['max_drawdown']*100:.2f}%" if m['max_drawdown'] else "—")
    return m

def pct(v, d=2):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+.{d}f}%"

def _trim(nav_df, f, t):
    if nav_df is None or nav_df.empty:
        return pd.DataFrame(columns=["date","nav"])
    df = nav_df.copy()
    if f: df = df[df["date"] >= pd.Timestamp(f)]
    if t: df = df[df["date"] <= pd.Timestamp(t)]
    return df


# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.markdown("## 📈 bloom_india\n### Mutual Fund Dashboard")
st.sidebar.markdown("---")
page = st.sidebar.radio("Navigation", [
    "🔍 Fund Explorer",
    "⭐ Watchlist",
    "🔬 Fund Screener",
    "📊 Fund Detail",
    "⚖️ Compare Funds",
    "💰 P&L Calculator",
    "⚙️ Cache Admin",
], label_visibility="collapsed")
st.sidebar.markdown("---")
st.sidebar.caption("Data: [mfapi.in](https://mfapi.in) · MIT License")

cat = load_catalogue()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — Fund Explorer
# ══════════════════════════════════════════════════════════════════════════════

if page == "🔍 Fund Explorer":
    st.title("🔍 Fund Explorer")
    st.caption("Browse 37,000+ funds. Search, filter, enrich with NAV + returns, add to watchlist.")

    with st.sidebar:
        st.markdown("### Filters")
        search_q  = st.text_input("Search name / AMC", placeholder="e.g. HDFC, Bluechip")
        cat_kw    = st.text_input("Category keyword",  placeholder="e.g. Large Cap, ELSS")
        sort_col  = st.selectbox("Sort by", ["scheme_name","fund_house"])
        asc       = st.checkbox("Ascending", value=True)
        page_size = st.slider("Rows per page", 25, 200, 50, 25)

    filtered = cat.copy()
    if search_q:
        filtered = filtered[
            filtered["scheme_name"].str.contains(search_q, case=False, na=False) |
            filtered["fund_house"].str.contains(search_q, case=False, na=False)]
    if cat_kw:
        filtered = filtered[
            filtered["scheme_name"].str.contains(cat_kw, case=False, na=False)]
    filtered = filtered.sort_values(sort_col, ascending=asc)

    total       = len(filtered)
    total_pages = max(1, (total-1)//page_size+1)
    _,cm,_ = st.columns([1,2,1])
    with cm:
        pg = st.number_input("Page", 1, total_pages, 1, label_visibility="collapsed")

    start   = (pg-1)*page_size
    page_df = filtered.iloc[start:start+page_size]

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Matching",f"{total:,}"); c2.metric("Total",f"{len(cat):,}")
    c3.metric("AMCs",filtered["fund_house"].nunique())
    c4.metric(f"Page {pg}/{total_pages}",f"{start+1}–{min(start+page_size,total)}")

    st.markdown("---")
    st.dataframe(page_df[["scheme_name","fund_house"]].rename(
        columns={"scheme_name":"Scheme Name","fund_house":"Fund House"}),
        use_container_width=True, height=450)

    # ── Add to watchlist ──────────────────────────────────────────────────────
    from bloom_india.mf.db.watchlist import add_fund, is_in_watchlist
    st.markdown("---")
    col_wl, col_enrich = st.columns(2)

    with col_wl:
        st.markdown("#### ⭐ Add to Watchlist")
        wl_names = st.multiselect("Select funds to watch",
                                  filtered["scheme_name"].tolist(),
                                  max_selections=20, key="explorer_wl")
        if st.button("Add to Watchlist", key="explorer_wl_btn") and wl_names:
            added = 0
            for n in wl_names:
                code = name_to_code(cat, n)
                row  = cat[cat["scheme_name"] == n]
                fh   = row["fund_house"].iloc[0] if not row.empty else ""
                if code and add_fund(code, n, fh):
                    added += 1
            st.success(f"Added {added} fund(s) to watchlist. "
                       f"{len(wl_names)-added} already in watchlist.")

    with col_enrich:
        st.markdown("#### 📊 Enrich with NAV + Returns")
        enrich_names = st.multiselect("Select funds to enrich",
                                      filtered["scheme_name"].tolist(),
                                      default=page_df["scheme_name"].head(3).tolist(),
                                      max_selections=20, key="explorer_enrich")
        if st.button("Fetch NAV + Returns", type="primary", key="explorer_enrich_btn") and enrich_names:
            codes = tuple(name_to_code(cat,n) for n in enrich_names if name_to_code(cat,n))
            if codes:
                enriched = load_enrich(codes)
                if enriched is None or enriched.empty:
                    st.warning("No NAV data. Seed these funds first:\n"
                               f"```\npython -m bloom_india.mf.db.ingest seed {' '.join(map(str,codes))}\n```")
                else:
                    nm = cat.set_index("scheme_code")["scheme_name"].to_dict()
                    enriched["Fund Name"] = enriched["scheme_code"].map(nm)
                    show = enriched.copy()
                    for c in ["return_1m","return_3m","return_6m","return_1y","return_3y"]:
                        if c in show.columns:
                            show[c] = show[c].apply(lambda v: f"{v*100:+.2f}%" if pd.notna(v) and v is not None else "—")
                    if "latest_nav" in show.columns:
                        show["latest_nav"] = show["latest_nav"].apply(
                            lambda v: f"₹{v:.4f}" if pd.notna(v) and v else "—")
                    dc = ["Fund Name","fund_house","category_clean","latest_nav",
                          "nav_date","return_1m","return_3m","return_6m","return_1y","return_3y"]
                    show = show[[c for c in dc if c in show.columns]]
                    show.columns = [c.replace("_"," ").title() for c in show.columns]
                    st.dataframe(show, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — Watchlist
# ══════════════════════════════════════════════════════════════════════════════

elif page == "⭐ Watchlist":
    st.title("⭐ Watchlist")
    st.caption("Your saved funds — persisted in the database, restored on every restart.")

    from bloom_india.mf.db.watchlist import (
        get_watchlist, add_fund, remove_fund, update_notes, clear_watchlist
    )

    wl = get_watchlist()

    # ── Add a fund ────────────────────────────────────────────────────────────
    with st.expander("➕ Add fund to watchlist", expanded=wl.empty):
        add_name, add_code = fund_picker(cat, key="wl_add", label="Search fund or AMC")
        notes_input = st.text_input("Notes (optional)", placeholder="e.g. Core holding, Watch for dip",
                                    key="wl_notes")
        if st.button("Add to Watchlist", type="primary", key="wl_add_btn"):
            if not add_code:
                st.error("Please search and select a fund first.")
            else:
                row = cat[cat["scheme_code"] == add_code]
                fh  = row["fund_house"].iloc[0] if not row.empty else ""
                cat_val = row.get("fund_house", pd.Series([""])).iloc[0]
                ok  = add_fund(add_code, add_name, fh, notes=notes_input)
                if ok:
                    st.success(f"✅ '{add_name}' added to watchlist!")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.info(f"'{add_name}' is already in your watchlist.")

    if wl.empty:
        st.info("Your watchlist is empty. Add funds from the Fund Explorer or the search above.")
        st.stop()

    st.markdown(f"**{len(wl)} fund(s) in watchlist**")
    st.markdown("---")

    # ── Live NAV + returns for watchlist ──────────────────────────────────────
    if st.button("🔄 Refresh NAV + Returns for all", type="secondary"):
        codes = tuple(int(c) for c in wl["scheme_code"].tolist())
        with st.spinner("Fetching…"):
            enriched = load_enrich(codes)
        if enriched is not None and not enriched.empty:
            nm = cat.set_index("scheme_code")["scheme_name"].to_dict()
            enriched["Fund Name"] = enriched["scheme_code"].map(nm)
            show = enriched.copy()
            for c in ["return_1m","return_3m","return_6m","return_1y","return_3y"]:
                if c in show.columns:
                    show[c] = show[c].apply(lambda v: f"{v*100:+.2f}%" if pd.notna(v) and v is not None else "—")
            if "latest_nav" in show.columns:
                show["latest_nav"] = show["latest_nav"].apply(
                    lambda v: f"₹{v:.4f}" if pd.notna(v) and v else "—")
            dc = ["Fund Name","fund_house","category_clean","latest_nav",
                  "nav_date","return_1m","return_3m","return_6m","return_1y","return_3y"]
            show = show[[c for c in dc if c in show.columns]]
            show.columns = [c.replace("_"," ").title() for c in show.columns]
            st.dataframe(show, use_container_width=True)

    st.markdown("---")

    # ── Per-fund cards ────────────────────────────────────────────────────────
    for _, row in wl.iterrows():
        code = int(row["scheme_code"])
        name = row["scheme_name"]
        with st.expander(f"**{name}**  ·  {row['fund_house']}  ·  Added {str(row['added_at'])[:10]}"):
            col1, col2 = st.columns([3, 1])
            with col1:
                new_notes = st.text_input("Notes", value=row.get("notes",""),
                                          key=f"wl_note_{code}")
                if st.button("Save notes", key=f"wl_save_{code}"):
                    update_notes(code, new_notes)
                    st.success("Saved.")

            with col2:
                if st.button("🗑 Remove", key=f"wl_rm_{code}", type="secondary"):
                    remove_fund(code)
                    st.rerun()

            # Mini NAV chart
            nav_df = load_nav(code)
            if nav_df is not None and not nav_df.empty:
                recent = nav_df[nav_df["date"] >= nav_df["date"].max() - pd.Timedelta(days=365)]
                nav_chart(recent, f"{name[:45]} — Last 1Y NAV")
                analytics_strip(recent)
            else:
                st.caption("No NAV data. Add to DB to see analytics.")

    st.markdown("---")
    if st.button("🗑 Clear entire watchlist", type="secondary"):
        n = clear_watchlist()
        st.warning(f"Cleared {n} funds from watchlist.")
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 3 — Fund Screener
# ══════════════════════════════════════════════════════════════════════════════

elif page == "🔬 Fund Screener":
    st.title("🔬 Fund Screener")
    st.caption("Filter funds in your DB by annualised return, Sharpe ratio, volatility and drawdown.")

    from bloom_india.mf.analytics.screener import run_screen
    from bloom_india.mf.db.nav_db          import db_stats
    from bloom_india.mf.db.watchlist        import add_fund

    ds = db_stats()
    st.info(f"🗄️ Screening across **{ds.get('schemes',0)}** funds in DB · "
            f"**{ds.get('nav_rows',0):,}** NAV rows.  "
            f"Add more funds via `python -m bloom_india.mf.db.ingest seed <codes>`")

    with st.form("screener_form"):
        st.markdown("### Date range")
        col1, col2 = st.columns(2)
        with col1:
            sc_from = st.date_input("From", value=pd.Timestamp("2021-01-01").date())
        with col2:
            sc_to   = st.date_input("To",   value=pd.Timestamp.today().date())

        st.markdown("### Return filters")
        col1, col2 = st.columns(2)
        with col1:
            min_ret = st.number_input("Min Ann. Return (%)", value=0.0,  step=1.0,
                                      help="e.g. 10 = only funds with CAGR ≥ 10%")
            max_ret = st.number_input("Max Ann. Return (%)", value=100.0, step=1.0)
        with col2:
            min_sharpe = st.number_input("Min Sharpe Ratio", value=0.0,  step=0.1,
                                         help="Higher = better risk-adjusted return")
            max_vol    = st.number_input("Max Volatility (%)", value=100.0, step=1.0,
                                         help="Lower = less risk")

        st.markdown("### Other filters")
        col1, col2 = st.columns(2)
        with col1:
            max_dd  = st.number_input("Max Drawdown (%, enter negative)",
                                      value=-100.0, step=5.0, max_value=0.0,
                                      help="e.g. -30 = only funds with drawdown better than -30%")
            cat_kw  = st.text_input("Category keyword (optional)",
                                    placeholder="e.g. Large Cap, Flexi, Liquid")
        with col2:
            rf_sc   = st.number_input("Risk-free rate (%)", value=6.5, step=0.1)
            sort_by = st.selectbox("Sort by", ["sharpe_ratio","ann_return","volatility","max_drawdown"])
            top_n   = st.slider("Max results", 10, 200, 50)

        submitted = st.form_submit_button("🔬 Run Screen", type="primary")

    if submitted:
        if sc_from >= sc_to:
            st.error("From date must be before To date.")
        else:
            with st.spinner("Running screen… (may take a moment for large DBs)"):
                results = run_screen(
                    min_ann_return = min_ret  / 100 if min_ret  > 0   else None,
                    max_ann_return = max_ret  / 100 if max_ret  < 100 else None,
                    min_sharpe     = min_sharpe     if min_sharpe > 0  else None,
                    max_volatility = max_vol   / 100 if max_vol  < 100 else None,
                    max_drawdown   = max_dd    / 100 if max_dd   > -100 else None,
                    category_kw    = cat_kw.strip()  if cat_kw.strip() else None,
                    from_date      = str(sc_from),
                    to_date        = str(sc_to),
                    risk_free      = rf_sc / 100,
                    top_n          = top_n,
                    sort_by        = sort_by,
                    ascending      = sort_by in ["volatility","max_drawdown"],
                )

            if results.empty:
                st.warning("No funds matched the criteria. Try relaxing the filters, "
                           "or seed more funds: `python -m bloom_india.mf.db.ingest seed`")
            else:
                st.success(f"✅ {len(results)} fund(s) matched")

                # Format display
                disp = results.copy()
                for c in ["ann_return_pct","volatility_pct","max_drawdown_pct"]:
                    if c in disp.columns:
                        disp[c] = disp[c].apply(lambda v: f"{v:+.2f}%" if pd.notna(v) and v is not None else "—")
                if "sharpe_ratio" in disp.columns:
                    disp["sharpe_ratio"] = disp["sharpe_ratio"].apply(
                        lambda v: f"{v:.3f}" if pd.notna(v) and v is not None else "—")
                drop = ["scheme_code","start_date","end_date","trading_days"]
                disp = disp.drop(columns=[c for c in drop if c in disp.columns])
                disp.columns = [c.replace("_"," ").title() for c in disp.columns]
                st.dataframe(disp, use_container_width=True, height=500)

                # Scatter: Ann Return vs Sharpe
                plot_df = results.dropna(subset=["ann_return_pct","sharpe_ratio"])
                if not plot_df.empty:
                    st.markdown("### Return vs Sharpe Scatter")
                    fig = go.Figure(go.Scatter(
                        x=plot_df["sharpe_ratio"],
                        y=plot_df["ann_return_pct"],
                        mode="markers+text",
                        text=[n[:25] for n in plot_df["scheme_name"]],
                        textposition="top center",
                        textfont=dict(size=9),
                        marker=dict(
                            size=8,
                            color=plot_df["sharpe_ratio"],
                            colorscale="Viridis",
                            showscale=True,
                            colorbar=dict(title="Sharpe"),
                        ),
                    ))
                    fig.update_layout(
                        paper_bgcolor="#1e1e2e", plot_bgcolor="#1e1e2e",
                        font=dict(color="#cdd6f4"),
                        xaxis=dict(gridcolor="#313244", title="Sharpe Ratio"),
                        yaxis=dict(gridcolor="#313244", title="Ann. Return %"),
                        margin=dict(l=0,r=0,t=20,b=0), height=420,
                    )
                    st.plotly_chart(fig, use_container_width=True)

                # Add screener results to watchlist
                st.markdown("---")
                st.markdown("#### Add results to Watchlist")
                wl_sel = st.multiselect(
                    "Select funds to add",
                    results["scheme_name"].tolist(),
                    key="screener_wl_sel"
                )
                if st.button("⭐ Add to Watchlist", key="screener_wl_btn") and wl_sel:
                    added = 0
                    for n in wl_sel:
                        row  = results[results["scheme_name"] == n].iloc[0]
                        code = int(row["scheme_code"])
                        if add_fund(code, n, row.get("fund_house",""),
                                    row.get("category","")):
                            added += 1
                    st.success(f"Added {added} fund(s) to watchlist.")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 4 — Fund Detail
# ══════════════════════════════════════════════════════════════════════════════

elif page == "📊 Fund Detail":
    st.title("📊 Fund Detail")
    from bloom_india.mf.db.watchlist import add_fund, is_in_watchlist

    name, code = fund_picker(cat, key="detail")
    if name and code:
        in_wl = is_in_watchlist(code)
        col_btn, col_wl = st.columns([2,1])
        with col_btn:
            load = st.button("Load Fund", type="primary")
        with col_wl:
            if not in_wl:
                if st.button("⭐ Add to Watchlist"):
                    row = cat[cat["scheme_code"]==code]
                    add_fund(code, name, row["fund_house"].iloc[0] if not row.empty else "")
                    st.success("Added!")
            else:
                st.caption("⭐ In your watchlist")

        if load:
            fund = load_fund_detail(code)
            if not fund:
                st.error(f"No data for '{name}'.")
            else:
                st.markdown(f"## {fund['scheme_name']}")
                st.markdown(
                    f"<span class='tag'>{fund['fund_house']}</span>"
                    f"<span class='tag'>{fund.get('category_clean','')}</span>"
                    f"<span class='tag'>{fund.get('scheme_type','')}</span>",
                    unsafe_allow_html=True)
                nav_val  = fund.get("latest_nav")
                st.metric(f"Latest NAV  ({fund.get('nav_date','')})",
                          f"₹{nav_val:.4f}" if nav_val else "—")

                st.markdown("---")
                st.subheader("Trailing Returns")
                returns = fund.get("returns",{})
                cols = st.columns(7)
                for i,(lbl,k) in enumerate([("1D","1d"),("1W","1w"),("1M","1m"),
                                             ("3M","3m"),("6M","6m"),("1Y","1y"),("3Y","3y")]):
                    v = returns.get(k)
                    cols[i].metric(lbl, pct(v) if v is not None else "—")

                st.markdown("---")
                st.subheader("NAV History + Risk Analytics")
                period_map = {"1M":30,"3M":91,"6M":182,"1Y":365,"3Y":1095,"5Y":1825}
                period = st.selectbox("Period", list(period_map)+["All"], index=3)
                nav_df = load_nav(code)
                if nav_df is not None and not nav_df.empty:
                    sliced = nav_df.copy()
                    if period != "All":
                        cutoff = nav_df["date"].max()-pd.Timedelta(days=period_map[period])
                        sliced = nav_df[nav_df["date"]>=cutoff]
                    nav_chart(sliced, f"{name[:50]} — NAV ({period})")
                    analytics_strip(sliced)
                    st.download_button("⬇ Download NAV CSV",
                                       data=nav_df.to_csv(index=False).encode(),
                                       file_name=f"nav_{name[:30]}.csv", mime="text/csv")
                else:
                    st.warning("No NAV history. Seed this fund first.")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 5 — Compare Funds
# ══════════════════════════════════════════════════════════════════════════════

elif page == "⚖️ Compare Funds":
    st.title("⚖️ Compare Funds")
    st.caption("NAV performance, annualised returns, Sharpe ratio, volatility and max drawdown side-by-side.")

    pairs = multi_fund_picker(cat, key="cmp", max_sel=5, label="Search funds to compare")

    col1,col2,col3 = st.columns(3)
    with col1:
        from_date = st.date_input("From", value=pd.Timestamp("2020-01-01").date(), key="cmp_from")
    with col2:
        to_date   = st.date_input("To",   value=pd.Timestamp.today().date(),       key="cmp_to")
    with col3:
        rf_pct    = st.number_input("Risk-free rate (%)", 0.0, 20.0, 6.5, 0.1)

    if st.button("Compare", type="primary") and pairs:
        if from_date >= to_date:
            st.error("From must be before To.")
        else:
            from bloom_india.mf.analytics.metrics import compare_metrics, rolling_returns
            from_s, to_s = str(from_date), str(to_date)

            nav_map = {}
            for name, code in pairs:
                nd = load_nav(code)
                if nd is not None and not nd.empty:
                    nav_map[name] = nd
                else:
                    st.warning(f"No NAV data for '{name}'. Seed it first.")

            if nav_map:
                # Normalised NAV
                st.markdown("### Normalised NAV Performance")
                fig = go.Figure()
                for i,(name,nd) in enumerate(nav_map.items()):
                    trimmed = _trim(nd, from_s, to_s)
                    if trimmed.empty: continue
                    base = trimmed["nav"].iloc[0]
                    norm = trimmed["nav"]/base*100
                    fig.add_trace(go.Scatter(x=trimmed["date"], y=norm, mode="lines",
                                             name=name[:45], line=dict(color=COLORS[i],width=2)))
                fig.update_layout(
                    title=f"Normalised NAV ({from_date} → {to_date}) — Base=100",
                    paper_bgcolor="#1e1e2e", plot_bgcolor="#1e1e2e",
                    font=dict(color="#cdd6f4"),
                    xaxis=dict(gridcolor="#313244"),
                    yaxis=dict(gridcolor="#313244", title="Normalised NAV"),
                    hovermode="x unified", legend=dict(bgcolor="#1e1e2e"),
                    margin=dict(l=0,r=0,t=50,b=0))
                st.plotly_chart(fig, use_container_width=True)

                # Analytics table
                st.markdown("### 📊 Analytics Summary")
                st.caption(f"{from_date} → {to_date} · Risk-free: {rf_pct}%")
                mdf = compare_metrics(nav_map, from_s, to_s, rf_pct/100)
                disp = mdf.copy()
                for c in ["Ann. Return %","Volatility %","Max Drawdown %"]:
                    if c in disp.columns:
                        disp[c] = disp[c].apply(lambda v: f"{v:+.2f}%" if pd.notna(v) and v is not None else "—")
                if "Sharpe" in disp.columns:
                    disp["Sharpe"] = disp["Sharpe"].apply(lambda v: f"{v:.3f}" if pd.notna(v) and v is not None else "—")
                st.dataframe(disp, use_container_width=True)

                # Bar tabs
                st.markdown("### 📈 Visual Breakdown")
                t1,t2,t3,t4 = st.tabs(["Ann. Return","Volatility","Sharpe","Max Drawdown"])
                clean = mdf.dropna(subset=["Ann. Return %"])

                def bar(tab, col, title, ylabel, inv=False):
                    with tab:
                        if clean.empty or col not in clean.columns: return
                        vals  = clean[col].tolist()
                        names = [n[:40] for n in clean["Fund"].tolist()]
                        cols  = ["#f38ba8" if (inv and v<0) or (not inv and v<0) else "#a6e3a1" for v in vals]
                        f = go.Figure(go.Bar(x=names, y=vals, marker_color=cols))
                        f.update_layout(title=title,
                                        paper_bgcolor="#1e1e2e",plot_bgcolor="#1e1e2e",
                                        font=dict(color="#cdd6f4"),
                                        xaxis=dict(gridcolor="#313244",tickangle=-20),
                                        yaxis=dict(gridcolor="#313244",title=ylabel),
                                        margin=dict(l=0,r=0,t=40,b=80),height=340)
                        st.plotly_chart(f, use_container_width=True)

                bar(t1,"Ann. Return %","Annualised Return %","Return %")
                bar(t2,"Volatility %","Volatility %","Volatility %",inv=True)
                bar(t3,"Sharpe","Sharpe Ratio","Sharpe")
                bar(t4,"Max Drawdown %","Max Drawdown %","Drawdown %",inv=True)

                # Rolling 1Y
                st.markdown("### 📉 Rolling 1Y Return")
                fig2 = go.Figure()
                any_rolling = False
                for i,(name,nd) in enumerate(nav_map.items()):
                    roll = rolling_returns(nd, 365)
                    roll = _trim(roll.rename(columns={"rolling_return":"nav"}),
                                 from_s,to_s).rename(columns={"nav":"rolling_return"})
                    if roll.empty: continue
                    any_rolling = True
                    fig2.add_trace(go.Scatter(x=roll["date"], y=roll["rolling_return"]*100,
                                              mode="lines", name=name[:45],
                                              line=dict(color=COLORS[i],width=1.5)))
                if any_rolling:
                    fig2.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
                    fig2.update_layout(
                        title="Rolling 1Y Annualised Return (%)",
                        paper_bgcolor="#1e1e2e",plot_bgcolor="#1e1e2e",
                        font=dict(color="#cdd6f4"),
                        xaxis=dict(gridcolor="#313244"),
                        yaxis=dict(gridcolor="#313244",title="1Y Return %"),
                        hovermode="x unified",legend=dict(bgcolor="#1e1e2e"),
                        margin=dict(l=0,r=0,t=40,b=0))
                    st.plotly_chart(fig2, use_container_width=True)
                else:
                    st.info("Need 365+ days of data for rolling returns.")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 6 — P&L Calculator
# ══════════════════════════════════════════════════════════════════════════════

elif page == "💰 P&L Calculator":
    st.title("💰 P&L Calculator")
    st.caption("Lump sum, SIP and multi-fund P&L with risk analytics.")
    import traceback
    from bloom_india.mf.analytics.pnl     import lumpsum_pnl, sip_pnl, compare_pnl, PnLError
    from bloom_india.mf.db.nav_db         import db_stats, scheme_exists
    from bloom_india.mf.db.ingest         import seed_scheme

    ds = db_stats()
    if ds.get("nav_rows",0)==0:
        st.warning("⚠️ NAV DB empty. First calculation fetches live data.")
    else:
        st.info(f"🗄️ DB: **{ds['nav_rows']:,}** rows · **{ds['schemes']}** schemes · latest: **{ds['latest_nav']}**")

    st.markdown("---")
    tab1,tab2,tab3 = st.tabs(["📌 Lump Sum","🔄 SIP","⚖️ Compare"])

    # Lump sum
    with tab1:
        ls_name, ls_code = fund_picker(cat, key="ls")
        c1,c2 = st.columns(2)
        with c1:
            ls_amt  = st.number_input("Investment (₹)", min_value=100.0, value=10000.0, step=1000.0)
        with c2:
            ls_buy  = st.date_input("Buy Date",  value=pd.Timestamp("2022-01-03").date(), key="ls_buy")
            ls_sell = st.date_input("Sell Date", value=pd.Timestamp.today().date(),       key="ls_sell")
        if st.button("Calculate", type="primary", key="ls_btn"):
            if not ls_code: st.error("Select a fund.")
            elif ls_buy>=ls_sell: st.error("Buy must be before sell.")
            else:
                with st.spinner("Computing…"):
                    try:
                        if not scheme_exists(ls_code):
                            st.info(f"Seeding '{ls_name}'…"); seed_scheme(ls_code)
                        r = lumpsum_pnl(ls_code, str(ls_buy), str(ls_sell), float(ls_amt))
                        for w in r.get("warnings",[]): st.warning(w)
                        c1,c2,c3,c4,c5 = st.columns(5)
                        c1.metric("Invested",      f"₹{r['invested']:,.2f}")
                        c2.metric("Value",         f"₹{r['current_value']:,.2f}", delta=f"₹{r['pnl']:,.2f}")
                        c3.metric("P&L %",         f"{r['pnl_pct']:+.2f}%")
                        c4.metric("CAGR",          f"{r['cagr_pct']:+.2f}%" if r["cagr_pct"] else "—")
                        c5.metric("Days",          f"{r['holding_days']}")
                        with st.expander("Details"):
                            st.json({"Fund":ls_name,"Buy":r["adjusted_buy_date"],
                                     "Sell":r["adjusted_sell_date"],
                                     "Buy NAV":f"₹{r['buy_nav']}","Sell NAV":f"₹{r['sell_nav']}",
                                     "Units":r["units"]})
                        nd = load_nav(ls_code)
                        if nd is not None and not nd.empty:
                            mask = ((nd["date"]>=pd.Timestamp(r["adjusted_buy_date"])) &
                                    (nd["date"]<=pd.Timestamp(r["adjusted_sell_date"])))
                            color = "#a6e3a1" if r["pnl"]>=0 else "#f38ba8"
                            nav_chart(nd[mask], f"{ls_name[:50]} — holding period NAV", color)
                            st.markdown("#### Risk Analytics (holding period)")
                            analytics_strip(nd[mask])
                    except PnLError as e: st.error(f"P&L Error: {e}")
                    except Exception as e:
                        st.error(f"Error: {e}")
                        with st.expander("Traceback"): st.code(traceback.format_exc())

    # SIP
    with tab2:
        sip_name, sip_code = fund_picker(cat, key="sip")
        c1,c2 = st.columns(2)
        with c1:
            sip_amt  = st.number_input("Monthly SIP (₹)", min_value=100.0, value=5000.0, step=500.0)
            sip_freq = st.number_input("Frequency (days)", 1, 365, 30)
        with c2:
            sip_s = st.date_input("Start", value=pd.Timestamp("2020-01-01").date())
            sip_e = st.date_input("End",   value=pd.Timestamp.today().date())
        if st.button("Calculate SIP", type="primary", key="sip_btn"):
            if not sip_code: st.error("Select a fund.")
            elif sip_s>=sip_e: st.error("Start must be before end.")
            else:
                with st.spinner("Computing SIP…"):
                    try:
                        if not scheme_exists(sip_code):
                            st.info(f"Seeding '{sip_name}'…"); seed_scheme(sip_code)
                        r = sip_pnl(sip_code, str(sip_s), str(sip_e), float(sip_amt), int(sip_freq))
                        for w in r.get("warnings",[]): st.warning(w)
                        c1,c2,c3,c4,c5 = st.columns(5)
                        c1.metric("Invested",    f"₹{r['total_invested']:,.2f}")
                        c2.metric("Value",       f"₹{r['current_value']:,.2f}", delta=f"₹{r['pnl']:,.2f}")
                        c3.metric("P&L %",       f"{r['pnl_pct']:+.2f}%")
                        c4.metric("XIRR",        f"{r['xirr_pct']:+.2f}%" if r["xirr_pct"] else "—")
                        c5.metric("Instalments", r["instalment_count"])
                        nd = load_nav(sip_code)
                        if nd is not None and not nd.empty:
                            st.markdown("#### Risk Analytics")
                            analytics_strip(_trim(nd, str(sip_s), str(sip_e)))
                        if r["instalments"]:
                            with st.expander(f"All {r['instalment_count']} instalments"):
                                st.dataframe(pd.DataFrame(r["instalments"]), use_container_width=True)
                            idf = pd.DataFrame(r["instalments"])
                            idf["adjusted_date"] = pd.to_datetime(idf["adjusted_date"])
                            idf["current_value"] = idf["cumulative_units"]*r["sell_nav"]
                            fig = go.Figure()
                            fig.add_trace(go.Scatter(x=idf["adjusted_date"],y=idf["cumulative_invested"],
                                                     name="Invested",mode="lines",
                                                     line=dict(color="#89b4fa",width=2)))
                            fig.add_trace(go.Scatter(x=idf["adjusted_date"],y=idf["current_value"],
                                                     name="Value",mode="lines",
                                                     line=dict(color="#a6e3a1" if r["pnl"]>=0 else "#f38ba8",width=2),
                                                     fill="tonexty",
                                                     fillcolor="rgba(166,227,161,0.1)" if r["pnl"]>=0 else "rgba(243,139,168,0.1)"))
                            fig.update_layout(
                                title=f"{sip_name[:50]} — Cumulative invested vs value",
                                paper_bgcolor="#1e1e2e",plot_bgcolor="#1e1e2e",
                                font=dict(color="#cdd6f4"),
                                xaxis=dict(gridcolor="#313244"),
                                yaxis=dict(gridcolor="#313244",title="₹"),
                                legend=dict(bgcolor="#1e1e2e"),
                                margin=dict(l=0,r=0,t=40,b=0))
                            st.plotly_chart(fig, use_container_width=True)
                    except PnLError as e: st.error(f"P&L Error: {e}")
                    except Exception as e:
                        st.error(f"Error: {e}")
                        with st.expander("Traceback"): st.code(traceback.format_exc())

    # Compare
    with tab3:
        st.caption("Compare P&L + risk analytics across funds over the same date range.")
        cmp_pairs = multi_fund_picker(cat, key="pnl_cmp", max_sel=10,
                                      label="Search funds to compare")
        c1,c2,c3,c4 = st.columns(4)
        with c1:
            cmp_mode   = st.selectbox("Mode", ["lumpsum","sip"])
            cmp_amount = st.number_input("Amount (₹)", min_value=100.0, value=10000.0, step=1000.0)
        with c2:
            cmp_buy  = st.date_input("Buy/Start", value=pd.Timestamp("2021-01-04").date(), key="cmp_pnl_buy")
        with c3:
            cmp_sell = st.date_input("Sell/End",  value=pd.Timestamp.today().date(),       key="cmp_pnl_sell")
        with c4:
            rf_cmp   = st.number_input("Risk-free (%)", 0.0, 20.0, 6.5, 0.1, key="cmp_rf")

        if st.button("Compare P&L", type="primary", key="cmp_pnl_btn") and cmp_pairs:
            if cmp_buy>=cmp_sell: st.error("Buy must be before sell.")
            else:
                name_code = {n:c for n,c in cmp_pairs}
                code_name = {c:n for n,c in cmp_pairs}
                with st.spinner("Computing…"):
                    pnl_res = compare_pnl(list(name_code.values()), str(cmp_buy), str(cmp_sell),
                                          float(cmp_amount), cmp_mode)
                # P&L table
                st.markdown("#### P&L Results")
                rows = []
                for r in pnl_res:
                    fn = code_name.get(r["scheme_code"], str(r["scheme_code"]))
                    if "error" in r:
                        rows.append({"Fund":fn[:55],"Invested":"—","Value":"—","P&L":"—",
                                     "P&L %":"—","CAGR/XIRR":"—","Status":f"❌ {r['error'][:50]}"})
                    else:
                        ret = r.get("xirr_pct") or r.get("cagr_pct")
                        rows.append({"Fund":fn[:55],
                                     "Invested":f"₹{r.get('total_invested',r.get('invested',0)):,.2f}",
                                     "Value":f"₹{r.get('current_value',0):,.2f}",
                                     "P&L":f"₹{r.get('pnl',0):+,.2f}",
                                     "P&L %":f"{r.get('pnl_pct',0):+.2f}%",
                                     "CAGR/XIRR":f"{ret:+.2f}%" if ret else "—","Status":"✅"})
                st.dataframe(pd.DataFrame(rows), use_container_width=True)

                # Analytics
                st.markdown("#### Risk Analytics (same period)")
                from bloom_india.mf.analytics.metrics import compare_metrics
                nav_map = {n: load_nav(c) for n,c in name_code.items()
                           if load_nav(c) is not None and not load_nav(c).empty}
                if nav_map:
                    mdf  = compare_metrics(nav_map, str(cmp_buy), str(cmp_sell), rf_cmp/100)
                    disp = mdf.copy()
                    for col in ["Ann. Return %","Volatility %","Max Drawdown %"]:
                        if col in disp.columns:
                            disp[col] = disp[col].apply(lambda v: f"{v:+.2f}%" if pd.notna(v) and v is not None else "—")
                    if "Sharpe" in disp.columns:
                        disp["Sharpe"] = disp["Sharpe"].apply(lambda v: f"{v:.3f}" if pd.notna(v) and v is not None else "—")
                    st.dataframe(disp, use_container_width=True)

                # P&L bar
                ok_r = [r for r in pnl_res if "error" not in r]
                if ok_r:
                    nx = [code_name.get(r["scheme_code"],"")[:40] for r in ok_r]
                    vy = [r.get("pnl_pct",0) for r in ok_r]
                    fig = go.Figure(go.Bar(x=nx, y=[v*100 for v in vy],
                                          marker_color=["#a6e3a1" if v>=0 else "#f38ba8" for v in vy]))
                    fig.update_layout(title="P&L % comparison",
                                      paper_bgcolor="#1e1e2e",plot_bgcolor="#1e1e2e",
                                      font=dict(color="#cdd6f4"),
                                      xaxis=dict(gridcolor="#313244",tickangle=-30),
                                      yaxis=dict(gridcolor="#313244",title="P&L %"),
                                      margin=dict(l=0,r=0,t=40,b=100),height=380)
                    st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 7 — Cache Admin
# ══════════════════════════════════════════════════════════════════════════════

elif page == "⚙️ Cache Admin":
    st.title("⚙️ Cache Admin")
    from bloom_india.mf.cache.disk_cache import cache_stats, clear_all_cache
    from bloom_india.mf.db.nav_db        import db_stats

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Disk cache")
        cs = cache_stats()
        st.metric("Valid entries",   cs["valid_entries"])
        st.metric("Expired entries", cs["expired"])
        st.caption(f"Dir: `{cs['cache_dir']}`")
        if st.button("🗑️ Clear disk cache", type="secondary"):
            clear_all_cache(); st.cache_data.clear(); st.success("Cache cleared.")
    with c2:
        st.subheader("NAV database")
        ds = db_stats()
        st.metric("Schemes",    ds.get("schemes",0))
        st.metric("NAV rows",   f"{ds.get('nav_rows',0):,}")
        st.metric("DB size",    f"{ds.get('size_mb',0)} MB")
        st.metric("Latest NAV", ds.get("latest_nav","—"))
        st.caption(f"Path: `{ds.get('db_path','')}`")