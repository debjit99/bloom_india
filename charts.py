"""
bloom_india/terminal/components/charts.py
==========================================
Reusable Plotly chart builders for the terminal.
All charts use the Bloomberg dark theme.
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from typing import Optional

BG       = "#111618"
BG2      = "#0d1617"
GRID     = "#1e2a2c"
GREEN    = "#4ecca3"
RED      = "#e05252"
AMBER    = "#e09a52"
MUTED    = "#637b7d"
FONT     = "JetBrains Mono, monospace"

BASE_LAYOUT = dict(
    paper_bgcolor = BG,
    plot_bgcolor  = BG2,
    font          = dict(family=FONT, color=MUTED, size=9),
    showlegend    = False,
    xaxis         = dict(gridcolor=GRID, tickangle=45, tickfont=dict(size=8)),
    yaxis         = dict(gridcolor=GRID, tickfont=dict(size=9)),
)


def _layout(height: int, **kwargs) -> dict:
    l = {**BASE_LAYOUT, "height": height, "margin": dict(l=40,r=20,t=20,b=50)}
    l.update(kwargs)
    return l


def bar_chart(
    x:       list,
    y1:      list,
    y2:      list = None,
    y1_name: str  = "Series 1",
    y2_name: str  = "Series 2",
    height:  int  = 260,
) -> go.Figure:
    """Grouped bar chart — one or two series."""
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=x, y=y1, name=y1_name,
        marker_color="#1e3a3a",
        marker_line_color=GREEN, marker_line_width=1,
        yaxis="y",
    ))
    if y2 is not None:
        fig.add_trace(go.Bar(
            x=x, y=y2, name=y2_name,
            marker_color="#3a2a1e",
            marker_line_color=AMBER, marker_line_width=1,
            yaxis="y2",
        ))
    layout = _layout(height,
        barmode    = "group",
        showlegend = y2 is not None,
        legend     = dict(font=dict(size=9, color=MUTED), bgcolor="transparent", x=0, y=1),
        yaxis      = dict(gridcolor=GRID, tickformat=",", tickfont=dict(size=9)),
        yaxis2     = dict(overlaying="y", side="right", gridcolor="#141d1e",
                          tickformat=",", tickfont=dict(size=9)) if y2 else {},
    )
    fig.update_layout(**layout)
    return fig


def line_chart(
    x:      list,
    y:      list,
    suffix: str = "",
    height: int = 200,
    color:  str = GREEN,
) -> go.Figure:
    """Simple line chart with fill."""
    fig = go.Figure(go.Scatter(
        x=x, y=y,
        mode="lines+markers",
        line=dict(color=color, width=1.5),
        marker=dict(color=color, size=4),
        fill="tozeroy",
        fillcolor=f"rgba(78,204,163,0.05)",
    ))
    fig.update_layout(**_layout(height,
        yaxis=dict(gridcolor=GRID, ticksuffix=suffix, tickfont=dict(size=9)),
    ))
    return fig


def waterfall_chart(
    revenue: float,
    op:      float,
    pat:     float,
    tax:     float,
    height:  int = 240,
) -> go.Figure:
    """Horizontal margin waterfall."""
    r     = revenue or 1
    opex  = revenue - op
    vals  = [100, -(opex/r*100), op/r*100, -(tax/r*100), pat/r*100]
    labels = ["Revenue","Op. Costs","Op. Profit","Tax","Net Profit"]
    colors = [GREEN, RED, GREEN, AMBER, GREEN]

    fig = go.Figure(go.Bar(
        x=[round(v,1) for v in vals], y=labels,
        orientation="h",
        marker_color=colors, marker_line_width=0,
        text=[f"{v:.1f}%" for v in vals],
        textposition="auto",
        textfont=dict(size=9, color="#0a0e0f"),
    ))
    fig.update_layout(**_layout(height,
        xaxis=dict(ticksuffix="%", gridcolor=GRID, tickfont=dict(size=9)),
        yaxis=dict(gridcolor=GRID, tickfont=dict(size=10, color=MUTED)),
        margin=dict(l=90,r=20,t=20,b=30),
    ))
    return fig


def radar_chart(latest: "pd.Series", height: int = 300) -> go.Figure:
    """Factor radar chart."""
    def norm(v, lo, hi):
        if v is None or (isinstance(v, float) and np.isnan(v)): return 5
        return min(max((v - lo) / (hi - lo) * 10, 0), 10)

    labels = ["SUE","Rev YoY","PAT YoY","Rev Accel","Margin Δ"]
    vals   = [
        norm(latest.get("sue"),              -3, 3),
        norm(latest.get("revenue_yoy"),      -50, 100),
        norm(latest.get("pat_yoy"),          -50, 100),
        norm(latest.get("revenue_accel"),    -30, 30),
        norm(latest.get("margin_change_yoy"),-10, 10),
    ]
    vals_c  = vals + [vals[0]]
    labels_c = labels + [labels[0]]

    fig = go.Figure(go.Scatterpolar(
        r=vals_c, theta=labels_c,
        fill="toself",
        fillcolor="rgba(78,204,163,0.08)",
        line=dict(color=GREEN, width=1.5),
        marker=dict(color=GREEN, size=4),
    ))
    fig.update_layout(
        polar=dict(
            bgcolor=BG2,
            radialaxis=dict(visible=True, range=[0,10], gridcolor=GRID,
                           tickfont=dict(size=7, color=MUTED)),
            angularaxis=dict(gridcolor=GRID, tickfont=dict(size=10, color=MUTED)),
        ),
        paper_bgcolor=BG,
        font=dict(family=FONT, color=MUTED),
        margin=dict(l=40,r=40,t=40,b=40),
        showlegend=False,
        height=height,
    )
    return fig


def npa_bar_chart(npa_df: "pd.DataFrame", height: int = 220) -> go.Figure:
    """NPA history — gross and net NPA % over time."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=npa_df["quarter_label"],
        y=(npa_df["npa_pct_gross"]*100).round(3),
        name="Gross NPA %",
        mode="lines+markers",
        line=dict(color=AMBER, width=1.5),
        marker=dict(size=4),
    ))
    if "npa_pct_net" in npa_df.columns:
        fig.add_trace(go.Scatter(
            x=npa_df["quarter_label"],
            y=(npa_df["npa_pct_net"]*100).round(3),
            name="Net NPA %",
            mode="lines+markers",
            line=dict(color=RED, width=1.5, dash="dot"),
            marker=dict(size=4),
        ))
    fig.update_layout(**_layout(height,
        showlegend=True,
        legend=dict(font=dict(size=9, color=MUTED), bgcolor="transparent"),
        yaxis=dict(ticksuffix="%", gridcolor=GRID, tickfont=dict(size=9)),
    ))
    return fig
