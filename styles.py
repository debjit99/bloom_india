"""
bloom_india/terminal/components/styles.py
==========================================
Bloomberg dark terminal theme for Streamlit.
"""

import streamlit as st

THEME = {
    "bg":       "#0a0e0f",
    "surface":  "#111618",
    "surface2": "#0d1617",
    "border":   "#1e2a2c",
    "border2":  "#141d1e",
    "text":     "#c8d0cc",
    "muted":    "#637b7d",
    "dim":      "#3d5052",
    "green":    "#4ecca3",
    "red":      "#e05252",
    "amber":    "#e09a52",
    "font":     "'JetBrains Mono', 'IBM Plex Mono', monospace",
}


def inject_css():
    st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500&display=swap');

html, body, [class*="css"] {{
    background-color: {THEME['bg']} !important;
    color: {THEME['text']} !important;
    font-family: {THEME['font']} !important;
}}
.stApp {{ background-color: {THEME['bg']}; }}

section[data-testid="stSidebar"] {{
    background-color: {THEME['surface2']} !important;
    border-right: 1px solid {THEME['border']} !important;
}}
section[data-testid="stSidebar"] * {{ color: {THEME['text']} !important; }}

.stSelectbox > div > div {{
    background-color: {THEME['surface']} !important;
    border: 1px solid {THEME['border']} !important;
    color: {THEME['text']} !important;
    border-radius: 3px !important;
}}

.stTabs [data-baseweb="tab-list"] {{
    background-color: {THEME['surface']} !important;
    border-bottom: 1px solid {THEME['border']} !important;
    gap: 0 !important;
}}
.stTabs [data-baseweb="tab"] {{
    background-color: transparent !important;
    color: {THEME['dim']} !important;
    border: none !important;
    border-right: 1px solid {THEME['border']} !important;
    border-radius: 0 !important;
    font-family: {THEME['font']} !important;
    font-size: 11px !important;
    letter-spacing: .08em !important;
    padding: 10px 18px !important;
}}
.stTabs [aria-selected="true"] {{
    background-color: {THEME['surface2']} !important;
    color: {THEME['green']} !important;
    border-bottom: 2px solid {THEME['green']} !important;
}}

.metric-card {{
    background: {THEME['surface']};
    border: 1px solid {THEME['border']};
    border-radius: 4px;
    padding: 14px 16px;
    margin-bottom: 8px;
}}
.metric-label {{
    font-size: 9px;
    color: {THEME['dim']};
    letter-spacing: .12em;
    text-transform: uppercase;
    margin-bottom: 4px;
}}
.metric-value {{
    font-size: 22px;
    font-weight: 500;
    color: {THEME['text']};
}}
.card {{
    background: {THEME['surface']};
    border: 1px solid {THEME['border']};
    border-radius: 4px;
    padding: 10px 14px;
    margin-bottom: 8px;
}}
.section-head {{
    font-size: 9px;
    color: {THEME['green']};
    letter-spacing: .14em;
    text-transform: uppercase;
    border-left: 2px solid {THEME['green']};
    padding-left: 8px;
    margin: 14px 0 8px;
}}
.data-row {{
    display: flex;
    justify-content: space-between;
    padding: 5px 0;
    border-bottom: 1px solid {THEME['border2']};
    font-size: 12px;
}}
.data-row:last-child {{ border-bottom: none; }}
.data-label {{ color: {THEME['muted']}; }}
.data-val {{
    color: {THEME['text']};
    font-family: {THEME['font']};
}}
.badge {{
    display: inline-block;
    font-size: 9px;
    background: {THEME['surface2']};
    border: 1px solid {THEME['border']};
    border-radius: 2px;
    padding: 2px 6px;
    color: {THEME['muted']};
    margin-left: 6px;
    letter-spacing: .06em;
}}
.badge-green {{
    border-color: {THEME['green']};
    color: {THEME['green']};
}}
div[data-testid="stHorizontalBlock"] {{ gap: 8px; }}
</style>
""", unsafe_allow_html=True)
