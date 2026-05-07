"""
bloom_india/terminal/app.py
============================
Bloomberg-style fundamental data terminal.

Run:
    streamlit run bloom_india/terminal/app.py
"""

import os
import sys
import warnings
import numpy as np
import pandas as pd
import streamlit as st

warnings.filterwarnings("ignore")

# ── Ensure config is found ────────────────────────────────────────────────────
# Set BLOOM_INDIA_CONFIG before importing bloom_india modules
_config_candidates = [
    os.path.join(os.getcwd(), "config.yaml"),
    os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"),
]
for _c in _config_candidates:
    if os.path.exists(_c):
        os.environ.setdefault("BLOOM_INDIA_CONFIG", os.path.abspath(_c))
        break

from bloom_india.api import get_fundamentals, get_ohlcv, get_universe, get_panel
from bloom_india.data.fetch.universe import NIFTY50
from bloom_india.terminal.components.charts import (
    bar_chart, line_chart, waterfall_chart, radar_chart, npa_bar_chart,
)
from bloom_india.terminal.components.styles import THEME, inject_css

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title  = "◈ bloom_india",
    page_icon   = "◈",
    layout      = "wide",
    initial_sidebar_state = "expanded",
)
inject_css()

BANKS = {"HDFCBANK","ICICIBANK","AXISBANK","KOTAKBANK","SBIN",
         "INDUSINDBK","BAJFINANCE","BAJAJFINSV","HDFC"}

# ── Load data ─────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def load_db():
    from bloom_india.config import CONFIG
    import pandas as pd
    db = pd.read_parquet(str(CONFIG.storage.fundamental_db))
    db["announce_date"] = pd.to_datetime(db["announce_date"])
    db["period_end"]    = pd.to_datetime(db["period_end"], errors="coerce")
    return db

@st.cache_data(ttl=3600)
def load_panel():
    from bloom_india.config import CONFIG
    try:
        p = pd.read_parquet(str(CONFIG.storage.price_db))
        p["Date"] = pd.to_datetime(p["Date"])
        return p
    except Exception:
        return pd.DataFrame()

db    = load_db()
panel = load_panel()

# ── Sidebar ───────────────────────────────────────────────────────────────────

symbols = sorted(db["symbol"].unique())

with st.sidebar:
    st.markdown(
        '<div style="font-size:20px;font-weight:500;color:#4ecca3;'
        'letter-spacing:.1em;margin-bottom:2px">◈ bloom_india</div>'
        '<div style="font-size:9px;color:#3d5052;letter-spacing:.1em;'
        'margin-bottom:20px;text-transform:uppercase">NSE · PIT Fundamental Data</div>',
        unsafe_allow_html=True,
    )

    selected = st.selectbox(
        "Symbol",
        options = symbols,
        index   = symbols.index("HDFCBANK") if "HDFCBANK" in symbols else 0,
    )

    st.markdown("---")

    sym_df = db[db["symbol"] == selected].sort_values("period_end")
    latest = sym_df.iloc[-1] if not sym_df.empty else None

    if latest is not None:
        st.markdown(
            f'<div style="font-size:9px;color:#3d5052;letter-spacing:.1em;'
            f'text-transform:uppercase;margin-bottom:6px">Latest filing</div>'
            f'<div style="font-size:12px;color:#4ecca3">{latest.get("quarter_label","—")}</div>'
            f'<div style="font-size:10px;color:#637b7d;margin-bottom:12px">'
            f'Filed {str(latest.get("announce_date",""))[:10]}</div>',
            unsafe_allow_html=True,
        )

    st.markdown(
        '<div style="font-size:9px;color:#3d5052;letter-spacing:.1em;'
        'text-transform:uppercase;margin-bottom:4px">Data</div>'
        '<div style="font-size:10px;color:#637b7d">NSE XBRL · BSE Announcements</div>'
        '<div style="font-size:9px;color:#3d5052;margin-top:4px">Point-in-time safe</div>',
        unsafe_allow_html=True,
    )

# ── Guard ─────────────────────────────────────────────────────────────────────

if latest is None:
    st.error(f"No data for {selected}")
    st.stop()

is_bank = selected in BANKS

# ── Header ────────────────────────────────────────────────────────────────────

ql   = latest.get("quarter_label", "—")
ann  = str(latest.get("announce_date", ""))[:10]
cons = "Consolidated" if latest.get("consolidated", True) else "Standalone"

bank_badge = '<span class="badge badge-green">BANK</span>' if is_bank else ""
st.markdown(
    f'<div style="display:flex;align-items:baseline;gap:10px;'
    f'padding:10px 0 8px;border-bottom:1px solid #1e2a2c;margin-bottom:12px">'
    f'<span style="font-size:22px;font-weight:500;color:#c8d0cc">{selected}</span>'
    f'<span style="font-size:12px;color:#637b7d">Nifty 50</span>'
    f'<span class="badge badge-green">{ql}</span>'
    f'<span class="badge">Filed {ann}</span>'
    f'<span class="badge">{cons}</span>'
    f'{bank_badge}'
    f'</div>',
    unsafe_allow_html=True,
)

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "OVERVIEW", "FINANCIALS", "FACTORS", "HISTORY", "BANK DATA"
])

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_cr(v):
    if v is None or (isinstance(v, float) and np.isnan(v)): return "—"
    if abs(v) >= 1e5: return f"₹{v/1e5:.2f}L Cr"
    return f"₹{v:,.0f} Cr"

def fmt_pct(v, d=1):
    if v is None or (isinstance(v, float) and np.isnan(v)): return "—"
    return f"{v:.{d}f}%"

def fmt_num(v, d=2):
    if v is None or (isinstance(v, float) and np.isnan(v)): return "—"
    return f"{v:.{d}f}"

def delta_html(v, unit="%", inv=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return '<span style="color:#637b7d">—</span>'
    pos = v < 0 if inv else v > 0
    c   = "#4ecca3" if pos else "#e05252"
    s   = f"+{v:.1f}{unit}" if v > 0 else f"{v:.1f}{unit}"
    return f'<span style="color:{c};font-size:11px">{s}</span>'

def metric(label, value, delta=None, delta_unit="%", inv=False):
    d = f'<div style="margin-top:2px">{delta_html(delta, delta_unit, inv)}</div>' if delta is not None else ""
    st.markdown(
        f'<div class="metric-card">'
        f'<div class="metric-label">{label}</div>'
        f'<div class="metric-value">{value}</div>'
        f'{d}</div>',
        unsafe_allow_html=True,
    )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════

with tab1:
    c1, c2, c3, c4 = st.columns(4)
    with c1: metric("Revenue",    fmt_cr(latest.get("revenue")),   latest.get("revenue_yoy"),        "YoY")
    with c2: metric("PAT",        fmt_cr(latest.get("pat")),       latest.get("pat_yoy"),             "YoY")
    with c3: metric("EPS Basic",  f'₹{fmt_num(latest.get("eps_basic"))}', latest.get("eps_basic_yoy"), "YoY")
    with c4: metric("PAT Margin", fmt_pct(latest.get("pat_margin")), latest.get("margin_change_yoy"), "pp YoY")

    st.markdown("")
    col_l, col_r = st.columns([1, 2])

    with col_l:
        st.markdown('<div class="section-head">P&L SUMMARY</div>', unsafe_allow_html=True)
        rows = [
            ("Revenue",          latest.get("revenue")),
            ("Other Income",     latest.get("other_income")),
            ("Total Income",     latest.get("total_income")),
            ("Int. Expended" if is_bank else "Employee Cost",
             latest.get("interest_expended") if is_bank else latest.get("employee_cost")),
            ("Operating Profit", latest.get("operating_profit")),
            ("Tax",              latest.get("tax")),
            ("Net Profit (PAT)", latest.get("pat")),
        ]
        html = ""
        for lbl, val in rows:
            accent = lbl in ("Net Profit (PAT)", "Total Income")
            color  = "#4ecca3" if accent else "#c8d0cc"
            border = "border-top:1px solid #1e2a2c;" if accent else ""
            html  += (f'<div class="data-row" style="{border}">'
                      f'<span class="data-label">{lbl}</span>'
                      f'<span class="data-val" style="color:{color}">{fmt_cr(val)}</span>'
                      f'</div>')
        st.markdown(f'<div class="card">{html}</div>', unsafe_allow_html=True)

    with col_r:
        st.markdown('<div class="section-head">QUARTERLY TREND</div>', unsafe_allow_html=True)
        plot_df = sym_df.dropna(subset=["revenue","pat"]).tail(12)
        if not plot_df.empty:
            fig = bar_chart(
                x       = plot_df["quarter_label"].tolist(),
                y1      = plot_df["revenue"].tolist(),
                y2      = plot_df["pat"].tolist(),
                y1_name = "Revenue",
                y2_name = "PAT",
                height  = 260,
            )
            st.plotly_chart(fig, use_container_width=True)

        st.markdown('<div class="section-head">PAT MARGIN TREND</div>', unsafe_allow_html=True)
        m_df = sym_df.dropna(subset=["pat_margin"]).tail(12)
        if not m_df.empty:
            fig2 = line_chart(
                x      = m_df["quarter_label"].tolist(),
                y      = m_df["pat_margin"].round(2).tolist(),
                suffix = "%",
                height = 180,
            )
            st.plotly_chart(fig2, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — FINANCIALS
# ══════════════════════════════════════════════════════════════════════════════

with tab2:
    # ── Field groups — show everything available ──────────────────────────────
    GROUPS = {
        "P&L": [
            ("Revenue / Interest Earned", "revenue"),
            ("Interest on Advances",      "interest_on_advances"),
            ("Income on Investments",     "income_on_investments"),
            ("Interest on RBI Funds",     "interest_on_rbi"),
            ("Other Interest",            "other_interest"),
            ("Other Revenue from Ops",    "other_rev_from_ops"),
            ("Other Income",              "other_income"),
            ("Total Income",              "total_income"),
            ("Cost of Materials",         "cost_of_materials"),
            ("Purchases of Stock",        "purchases_stock"),
            ("Inventory Change",          "inventory_change"),
            ("Employee Cost",             "employee_cost"),
            ("Finance Costs / Int. Exp.", "finance_costs"),
            ("Depreciation & Amort.",     "depreciation"),
            ("Other Expenses",            "other_expenses"),
            ("Total Expenses",            "total_expenses"),
            ("Op. Expenses (Bank)",       "operating_expenses_bank"),
            ("Expenditure excl. Prov.",   "expenditure_excl_prov"),
            ("EBITDA / Op. Profit",       "ebitda"),
            ("Operating Profit (Bank)",   "operating_profit_bank"),
            ("Provisions (Bank)",         "provisions_bank"),
            ("Exceptional Items",         "exceptional_items"),
            ("Profit Before Tax",         "profit_before_tax"),
            ("Current Tax",               "current_tax"),
            ("Deferred Tax",              "deferred_tax"),
            ("Total Tax",                 "tax"),
            ("PAT (Continuing Ops)",      "pat_continuing"),
            ("PAT (Discontinued Ops)",    "pat_discontinued"),
            ("PAT",                       "pat"),
            ("Other Comprehensive Inc.",  "other_comprehensive_income"),
            ("Total Comprehensive Inc.",  "total_comprehensive_income"),
            ("PAT Attributable Owners",   "pat_owners"),
            ("PAT Minority Interest",     "pat_minority"),
            ("Share of Associates",       "share_of_associates"),
            ("Dividend Income",           "dividend_income"),
            ("Fees & Commission",         "fees_commission"),
            ("Net Gain Fair Value",       "net_gain_fair_value"),
            ("Impairment Fin. Inst.",     "impairment_fin_inst"),
            ("Net Interest Income",       "nii"),
        ],
        "EPS & Ratios": [
            ("EPS Basic",                "eps_basic"),
            ("EPS Diluted",              "eps_diluted"),
            ("EPS Basic (Continuing)",   "eps_basic_continuing"),
            ("EPS Basic (Discontinued)", "eps_basic_discontinued"),
            ("PAT Margin %",             "pat_margin"),
            ("Revenue YoY %",            "revenue_yoy"),
            ("PAT YoY %",               "pat_yoy"),
            ("EPS YoY %",               "eps_basic_yoy"),
            ("Revenue Acceleration",     "revenue_accel"),
            ("Margin Change QoQ pp",     "margin_change_qoq"),
            ("Margin Change YoY pp",     "margin_change_yoy"),
            ("SUE",                      "sue"),
            ("Debt / Equity Ratio",      "debt_equity_ratio"),
            ("Debt Service Cover",       "debt_service_ratio"),
            ("Interest Coverage",        "interest_coverage"),
            ("Return on Assets",         "roa"),
        ],
        "Balance Sheet — Assets": [
            ("Total Assets",             "total_assets"),
            ("Non-current Assets",       "noncurrent_assets"),
            ("Current Assets",           "current_assets"),
            ("PPE",                      "ppe"),
            ("Capital WIP",              "capwip"),
            ("Goodwill",                 "goodwill"),
            ("Intangibles",              "intangibles"),
            ("Intangibles WIP",          "intangibles_wip"),
            ("Non-current Investments",  "noncurrent_investments"),
            ("Current Investments",      "current_investments"),
            ("Inventories",              "inventories"),
            ("Trade Receivables",        "trade_receivables"),
            ("Cash & Equivalents",       "cash_equivalents"),
            ("Bank Balances",            "bank_balances"),
            ("Loans & Advances",         "loans_assets"),
            ("Other Financial Assets",   "other_financial_assets"),
            ("Other Current Assets",     "other_current_assets"),
            ("Other Non-current Assets", "other_noncurrent_assets"),
            ("Deferred Tax Assets",      "deferred_tax_assets"),
            ("Investment Property",      "investment_property"),
            ("Investments in Assoc.",    "investments_associates"),
            ("Financial Assets (Bank)",  "financial_assets_bank"),
        ],
        "Balance Sheet — Liabilities & Equity": [
            ("Equity Capital",           "equity_capital"),
            ("Reserves & Surplus",       "reserves_surplus"),
            ("Total Equity",             "equity_total"),
            ("Equity (Owners)",          "equity_owners"),
            ("Minority Interest",        "minority_interest"),
            ("Total Liabilities",        "total_liabilities"),
            ("Non-current Liabilities",  "noncurrent_liabilities"),
            ("Current Liabilities",      "current_liabilities"),
            ("Borrowings (Non-current)", "borrowings_noncurrent"),
            ("Borrowings (Current)",     "borrowings_current"),
            ("Deposits (Bank)",          "deposits_bank"),
            ("Debt Securities",          "debt_securities"),
            ("Subordinated Liabilities", "subordinated_liab"),
            ("Trade Payables",           "trade_payables"),
            ("Other Current Liab.",      "other_current_liab"),
            ("Other Non-current Liab.",  "other_noncurrent_liab"),
            ("Provisions (Current)",     "provisions_current"),
            ("Provisions (Non-current)", "provisions_noncurrent"),
            ("Deferred Tax Liab.",       "deferred_tax_liab"),
            ("Current Tax Liab.",        "current_tax_liab"),
            ("Other Fin. Liab.",         "other_fin_liab"),
            ("Financial Liab. (Bank)",   "financial_liab_bank"),
            ("Other Liab. & Prov.",      "other_liab_provisions"),
        ],
        "Cash Flow": [
            ("CFO (Operating)",          "cfo"),
            ("CFI (Investing)",          "cfi"),
            ("CFF (Financing)",          "cff"),
            ("CapEx",                    "capex"),
            ("Free Cash Flow",           "free_cash_flow"),
            ("Cash (End of Period)",     "cash_end"),
            ("Net Change in Cash",       "net_change_cash"),
            ("Interest Paid",            "interest_paid_cf"),
            ("Tax Paid",                 "tax_paid_cf"),
            ("Dividends Paid",           "dividends_paid"),
            ("Proceeds from Borrowings", "proceeds_borrowings"),
            ("Repayments of Borrowings", "repayments_borrowings"),
        ],
        "Segment": [
            ("Segment Revenue",          "segment_revenue"),
            ("Segment Revenue (Ops)",    "segment_revenue_ops"),
            ("Segment Profit BT",        "segment_profit_bt"),
            ("Segment Assets",           "segment_assets"),
            ("Segment Liabilities",      "segment_liabilities"),
            ("Inter-segment Revenue",    "inter_segment_rev"),
            ("Unallocable Assets",       "unallocable_assets"),
            ("Unallocable Liabilities",  "unallocable_liab"),
        ],
        "Banking": [
            ("Gross NPA",                "npa_gross_cr"),
            ("Net NPA",                  "npa_net_cr"),
            ("Gross NPA %",              "npa_pct_gross"),
            ("Net NPA %",                "npa_pct_net"),
            ("CAR / CET1",               "car"),
            ("Additional Tier 1",        "tier1_additional"),
            ("NII YoY %",                "nii_yoy"),
            ("NPA Change QoQ",           "npa_change"),
            ("Govt. Holding %",          "pct_govt_holding"),
        ],
    }

    def fmt_val(key, val):
        """Format value based on field type."""
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return "—", "#3d5052"
        # Ratio/percentage fields
        pct_fields = {"pat_margin","revenue_yoy","pat_yoy","eps_basic_yoy",
                      "revenue_accel","margin_change_qoq","margin_change_yoy",
                      "sue","nii_yoy","roa","npa_pct_gross","npa_pct_net",
                      "car","tier1_additional","npa_change",
                      "debt_equity_ratio","debt_service_ratio","interest_coverage",
                      "pct_govt_holding"}
        small_fields = {"eps_basic","eps_diluted","eps_basic_continuing",
                        "eps_basic_discontinued","face_value"}

        if key in pct_fields:
            s = f"{val*100:.2f}%" if abs(val) < 1 and key not in {"sue","revenue_accel","margin_change_qoq","margin_change_yoy","revenue_yoy","pat_yoy","eps_basic_yoy","nii_yoy","npa_change","debt_equity_ratio","debt_service_ratio","interest_coverage","pct_govt_holding"} else f"{val:.2f}"
            c = "#4ecca3" if val > 0 else "#e05252" if val < 0 else "#637b7d"
            return s, c
        elif key in small_fields:
            return f"₹{val:.2f}", "#c8d0cc"
        else:
            return fmt_cr(val), "#c8d0cc"

    # Render each group
    for group_name, fields in GROUPS.items():
        # Only show groups that have at least one non-null value
        has_data = any(
            latest.get(key) is not None and
            not (isinstance(latest.get(key), float) and np.isnan(latest.get(key)))
            for _, key in fields
        )
        if not has_data:
            continue

        st.markdown(f'<div class="section-head">{group_name.upper()}</div>',
                    unsafe_allow_html=True)

        html = ""
        for lbl, key in fields:
            val = latest.get(key)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue  # skip null fields entirely
            fval, fc = fmt_val(key, val)
            accent = key in ("pat","total_income","equity_total","total_assets","cfo","nii")
            border = "border-top:1px solid #1e2a2c;" if accent else ""
            color  = "#4ecca3" if accent else fc
            html  += (f'<div class="data-row" style="{border}">'
                      f'<span class="data-label">{lbl}</span>'
                      f'<span class="data-val" style="color:{color}">{fval}</span>'
                      f'</div>')

        if html:
            st.markdown(f'<div class="card">{html}</div>', unsafe_allow_html=True)

    # Waterfall always shown if revenue available
    if latest.get("revenue"):
        st.markdown('<div class="section-head">MARGIN WATERFALL</div>', unsafe_allow_html=True)
        rev   = latest.get("revenue") or 1
        op    = latest.get("ebitda") or latest.get("operating_profit_bank") or 0
        pat_v = latest.get("pat") or 0
        tax_v = latest.get("tax") or 0
        fig3  = waterfall_chart(rev, op, pat_v, tax_v, height=220)
        st.plotly_chart(fig3, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — FACTORS
# ══════════════════════════════════════════════════════════════════════════════

with tab3:
    c1, c2 = st.columns(2)

    with c1:
        st.markdown('<div class="section-head">SUE — STANDARDISED UNEXPECTED EARNINGS</div>', unsafe_allow_html=True)
        sue = latest.get("sue")
        if sue is not None and not np.isnan(float(sue)):
            sc = "#4ecca3" if sue > 0.5 else "#e05252" if sue < -0.5 else "#637b7d"
            label = ("Strong Beat ↑" if sue > 1 else "Moderate Beat ↑" if sue > 0.3
                     else "Strong Miss ↓" if sue < -1 else "Moderate Miss ↓" if sue < -0.3
                     else "Neutral")
            st.markdown(
                f'<div class="metric-card" style="text-align:center">'
                f'<div class="metric-label">SUE Score</div>'
                f'<div style="font-size:36px;font-weight:500;color:{sc};margin:8px 0">{sue:.3f}</div>'
                f'<div style="font-size:10px;color:#637b7d">{label}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            sue_df = sym_df.dropna(subset=["sue"]).tail(12)
            if not sue_df.empty:
                colors = ["#4ecca3" if v > 0 else "#e05252" for v in sue_df["sue"]]
                import plotly.graph_objects as go
                fig_s = go.Figure(go.Bar(
                    x=sue_df["quarter_label"], y=sue_df["sue"].round(3),
                    marker_color=colors, marker_line_width=0,
                ))
                fig_s.add_hline(y=0, line_dash="dot", line_color="#3d5052", line_width=1)
                fig_s.update_layout(
                    paper_bgcolor="#111618", plot_bgcolor="#0d1617",
                    font=dict(family="JetBrains Mono, monospace", color="#637b7d", size=9),
                    margin=dict(l=30,r=10,t=20,b=40), height=180,
                    yaxis=dict(gridcolor="#1e2a2c", zeroline=True, zerolinecolor="#1e2a2c"),
                    xaxis=dict(gridcolor="#1e2a2c", tickangle=45),
                    showlegend=False,
                )
                st.plotly_chart(fig_s, use_container_width=True)

        st.markdown('<div class="section-head">ALL FACTOR SCORES</div>', unsafe_allow_html=True)
        factors = [
            ("SUE",               latest.get("sue"),              -3,  3),
            ("Revenue YoY %",     latest.get("revenue_yoy"),     -50, 100),
            ("PAT YoY %",         latest.get("pat_yoy"),         -50, 100),
            ("Rev Acceleration",  latest.get("revenue_accel"),   -30,  30),
            ("Margin Chg YoY pp", latest.get("margin_change_yoy"),-10, 10),
            ("NII YoY %",         latest.get("nii_yoy"),         -30,  50),
        ]
        for fname, fval, flo, fhi in factors:
            if fval is None or (isinstance(fval, float) and np.isnan(fval)):
                continue
            pct = min(max((fval - flo) / (fhi - flo) * 100, 0), 100)
            fc  = "#4ecca3" if fval > 0 else "#e05252"
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:8px;padding:4px 0;'
                f'border-bottom:1px solid #141d1e;font-size:10px">'
                f'<span style="width:130px;color:#637b7d;flex-shrink:0">{fname}</span>'
                f'<div style="flex:1;background:#0d1617;height:6px;border-radius:3px;'
                f'position:relative;overflow:hidden">'
                f'<div style="position:absolute;left:0;width:{pct}%;height:100%;'
                f'background:{fc};border-radius:3px"></div>'
                f'<div style="position:absolute;left:50%;top:0;width:1px;height:100%;'
                f'background:#1e2a2c"></div></div>'
                f'<span style="width:52px;text-align:right;color:{fc}">'
                f'{"+" if fval>0 else ""}{fval:.2f}</span></div>',
                unsafe_allow_html=True,
            )

    with c2:
        st.markdown('<div class="section-head">FACTOR RADAR</div>', unsafe_allow_html=True)
        fig_r = radar_chart(latest, height=300)
        st.plotly_chart(fig_r, use_container_width=True)

        st.markdown('<div class="section-head">CROSS-SECTIONAL RANK</div>', unsafe_allow_html=True)
        latest_all = db.sort_values("announce_date").groupby("symbol").last().reset_index()
        rank_items = [("SUE","sue"), ("Rev YoY","revenue_yoy"),
                      ("PAT YoY","pat_yoy"), ("Margin","pat_margin")]
        for lbl, col in rank_items:
            if col not in latest_all.columns:
                continue
            rank_df  = latest_all[["symbol",col]].dropna()
            if selected not in rank_df["symbol"].values:
                continue
            rank_df["_r"] = rank_df[col].rank(ascending=False)
            r = int(rank_df.loc[rank_df.symbol==selected,"_r"].values[0])
            n = len(rank_df)
            rc = "#4ecca3" if r <= n//4 else "#e05252" if r >= 3*n//4 else "#637b7d"
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:8px;padding:4px 0;'
                f'border-bottom:1px solid #141d1e;font-size:10px">'
                f'<span style="width:80px;color:#637b7d">{lbl}</span>'
                f'<div style="flex:1;background:#0d1617;height:4px;border-radius:2px">'
                f'<div style="width:{(n-r)/n*100:.0f}%;height:100%;background:{rc};'
                f'border-radius:2px"></div></div>'
                f'<span style="color:{rc}">#{r}/{n}</span></div>',
                unsafe_allow_html=True,
            )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — HISTORY
# ══════════════════════════════════════════════════════════════════════════════

with tab4:
    st.markdown('<div class="section-head">FULL FILING HISTORY</div>', unsafe_allow_html=True)

    hist = sym_df[[
        "quarter_label","announce_date","period_end",
        "revenue","pat","eps_basic","pat_margin",
        "revenue_yoy","pat_yoy","sue",
    ]].copy()
    hist["announce_date"] = hist["announce_date"].dt.strftime("%Y-%m-%d")
    hist["period_end"]    = hist["period_end"].dt.strftime("%Y-%m-%d")
    for col in ["revenue","pat"]:
        hist[col] = hist[col].apply(lambda v: f"{v:,.0f}" if pd.notna(v) else "—")
    for col in ["eps_basic","pat_margin","revenue_yoy","pat_yoy","sue"]:
        hist[col] = hist[col].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "—")
    hist.columns = ["Quarter","Filed","Period End","Revenue Cr","PAT Cr",
                    "EPS","Margin%","Rev YoY%","PAT YoY%","SUE"]
    st.dataframe(hist.iloc[::-1], use_container_width=True, height=380)

    if not panel.empty and selected in panel["Symbol"].values:
        st.markdown('<div class="section-head">PRICE HISTORY  ◆ = earnings announcement</div>',
                    unsafe_allow_html=True)
        px_df  = panel[panel["Symbol"]==selected].sort_values("Date").tail(500)
        import plotly.graph_objects as go
        fig_p  = go.Figure()
        fig_p.add_trace(go.Scatter(
            x=px_df["Date"], y=px_df["Close"].round(2),
            mode="lines", line=dict(color="#4ecca3", width=1.5),
            fill="tozeroy", fillcolor="rgba(78,204,163,0.04)",
        ))
        for _, row in sym_df.iterrows():
            ann = row.get("announce_date")
            if pd.isna(ann) or ann < px_df["Date"].min(): continue
            sub = px_df[px_df["Date"] >= ann]
            if sub.empty: continue
            fig_p.add_trace(go.Scatter(
                x=[sub.iloc[0]["Date"]], y=[round(sub.iloc[0]["Close"],2)],
                mode="markers",
                marker=dict(color="#e09a52", size=7, symbol="diamond"),
                showlegend=False,
                hovertext=row.get("quarter_label",""),
            ))
        fig_p.update_layout(
            paper_bgcolor="#111618", plot_bgcolor="#0d1617",
            font=dict(family="JetBrains Mono, monospace", color="#637b7d", size=9),
            margin=dict(l=40,r=20,t=20,b=40), height=280,
            yaxis=dict(tickprefix="₹", gridcolor="#1e2a2c"),
            xaxis=dict(gridcolor="#1e2a2c"),
            showlegend=False,
        )
        st.plotly_chart(fig_p, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — BANK DATA
# ══════════════════════════════════════════════════════════════════════════════

with tab5:
    if not is_bank:
        st.markdown(
            f'<div style="text-align:center;padding:60px;color:#3d5052">'
            f'<div style="font-size:24px;margin-bottom:12px">◈</div>'
            f'{selected} is not a bank.<br>'
            f'<span style="font-size:10px;color:#1e2a2c">'
            f'Bank data: HDFCBANK · ICICIBANK · AXISBANK · KOTAKBANK · SBIN · INDUSINDBK'
            f'</span></div>',
            unsafe_allow_html=True,
        )
    else:
        npa_g  = (latest.get("npa_pct_gross") or 0) * 100
        npa_n  = (latest.get("npa_pct_net")   or 0) * 100
        car_v  = (latest.get("car")            or 0) * 100
        nii_v  =  latest.get("nii")
        nii_y  =  latest.get("nii_yoy")
        npa_ch = (latest.get("npa_change")     or 0) * 100

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            c = "#4ecca3" if npa_g < 2 else "#e09a52" if npa_g < 4 else "#e05252"
            st.markdown(
                f'<div class="metric-card"><div class="metric-label">Gross NPA %</div>'
                f'<div class="metric-value" style="color:{c}">{npa_g:.2f}%</div>'
                f'<div style="font-size:10px;color:{"#4ecca3" if npa_ch<0 else "#e05252"}">'
                f'{npa_ch:+.3f}pp QoQ</div></div>', unsafe_allow_html=True)
        with c2:
            c = "#4ecca3" if npa_n < 0.5 else "#e09a52" if npa_n < 1 else "#e05252"
            st.markdown(
                f'<div class="metric-card"><div class="metric-label">Net NPA %</div>'
                f'<div class="metric-value" style="color:{c}">{npa_n:.2f}%</div></div>',
                unsafe_allow_html=True)
        with c3:
            c = "#4ecca3" if car_v > 15 else "#e09a52" if car_v > 12 else "#e05252"
            st.markdown(
                f'<div class="metric-card"><div class="metric-label">CAR / CET1</div>'
                f'<div class="metric-value" style="color:{c}">{car_v:.1f}%</div>'
                f'<div style="font-size:10px;color:#637b7d">Min 11.5%</div></div>',
                unsafe_allow_html=True)
        with c4:
            dc = "#4ecca3" if (nii_y or 0) > 0 else "#e05252"
            st.markdown(
                f'<div class="metric-card"><div class="metric-label">Net Interest Income</div>'
                f'<div class="metric-value">{fmt_cr(nii_v)}</div>'
                f'<div style="font-size:11px;color:{dc}">'
                f'{f"+{nii_y:.1f}%" if nii_y and nii_y>0 else f"{nii_y:.1f}%" if nii_y else "—"}'
                f' YoY</div></div>', unsafe_allow_html=True)

        col_l, col_r = st.columns(2)
        with col_l:
            st.markdown('<div class="section-head">NPA HISTORY</div>', unsafe_allow_html=True)
            npa_df = sym_df.dropna(subset=["npa_pct_gross"]).tail(10)
            if not npa_df.empty:
                fig_npa = npa_bar_chart(npa_df, height=220)
                st.plotly_chart(fig_npa, use_container_width=True)

        with col_r:
            st.markdown('<div class="section-head">NII TREND</div>', unsafe_allow_html=True)
            nii_df = sym_df.dropna(subset=["nii"]).tail(10)
            if not nii_df.empty:
                fig_nii = bar_chart(
                    x=nii_df["quarter_label"].tolist(),
                    y1=nii_df["nii"].round(0).tolist(),
                    y1_name="NII",
                    height=220,
                )
                st.plotly_chart(fig_nii, use_container_width=True)

        st.markdown('<div class="section-head">INTEREST BREAKDOWN</div>', unsafe_allow_html=True)
        b_rows = [
            ("Interest Earned",     latest.get("revenue")),
            ("Interest Expended",   latest.get("interest_expended")),
            ("Net Interest Income", latest.get("nii")),
            ("Other Income",        latest.get("other_income")),
            ("Total Income",        latest.get("total_income")),
        ]
        html_b = ""
        for lbl, val in b_rows:
            c = "#4ecca3" if lbl in ("Net Interest Income","Total Income") else "#c8d0cc"
            html_b += (f'<div class="data-row"><span class="data-label">{lbl}</span>'
                       f'<span class="data-val" style="color:{c}">{fmt_cr(val)}</span></div>')
        st.markdown(f'<div class="card">{html_b}</div>', unsafe_allow_html=True)