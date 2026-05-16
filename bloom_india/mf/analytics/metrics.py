"""
bloom_india/mf/analytics/metrics.py
=====================================
Portfolio analytics: annualised return, volatility, Sharpe ratio,
max drawdown, rolling metrics — all computed from NAV series.

All functions are safe: return None on bad input, never raise.
"""

from __future__ import annotations

import math
import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

RISK_FREE_RATE = 0.065   # ~6.5% p.a. (approx India 91-day T-bill)
TRADING_DAYS   = 252


def _clean(nav_df: pd.DataFrame) -> pd.DataFrame:
    """Ensure nav_df has [date (datetime64), nav (float)], sorted, valid."""
    if nav_df is None or nav_df.empty:
        return pd.DataFrame(columns=["date", "nav"])
    df = nav_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["nav"]  = pd.to_numeric(df["nav"], errors="coerce")
    df = df.dropna(subset=["nav"])
    df = df[df["nav"] > 0]
    df = df[df["nav"].apply(math.isfinite)]
    return df.sort_values("date").reset_index(drop=True)


def daily_returns(nav_df: pd.DataFrame) -> pd.Series:
    """Compute daily percentage returns from NAV series."""
    df = _clean(nav_df)
    if len(df) < 2:
        return pd.Series(dtype=float)
    return df.set_index("date")["nav"].pct_change().dropna()


def annualised_return(nav_df: pd.DataFrame,
                      from_date: Optional[str] = None,
                      to_date:   Optional[str] = None) -> Optional[float]:
    """
    CAGR over the given date range.
    Returns decimal (0.15 = 15%) or None if insufficient data.
    """
    try:
        df = _clean(nav_df)
        if from_date:
            df = df[df["date"] >= pd.Timestamp(from_date)]
        if to_date:
            df = df[df["date"] <= pd.Timestamp(to_date)]
        if len(df) < 2:
            return None
        start_nav = df["nav"].iloc[0]
        end_nav   = df["nav"].iloc[-1]
        days      = (df["date"].iloc[-1] - df["date"].iloc[0]).days
        if days <= 0 or start_nav <= 0:
            return None
        cagr = (end_nav / start_nav) ** (365.0 / days) - 1
        return round(cagr, 6) if math.isfinite(cagr) else None
    except Exception as e:
        log.debug(f"annualised_return error: {e}")
        return None


def annualised_volatility(nav_df: pd.DataFrame,
                          from_date: Optional[str] = None,
                          to_date:   Optional[str] = None) -> Optional[float]:
    """
    Annualised standard deviation of daily returns (proxy for risk).
    Returns decimal or None.
    """
    try:
        df = _clean(nav_df)
        if from_date:
            df = df[df["date"] >= pd.Timestamp(from_date)]
        if to_date:
            df = df[df["date"] <= pd.Timestamp(to_date)]
        rets = df.set_index("date")["nav"].pct_change().dropna()
        if len(rets) < 20:
            return None
        vol = float(rets.std() * math.sqrt(TRADING_DAYS))
        return round(vol, 6) if math.isfinite(vol) else None
    except Exception as e:
        log.debug(f"annualised_volatility error: {e}")
        return None


def sharpe_ratio(nav_df: pd.DataFrame,
                 from_date: Optional[str] = None,
                 to_date:   Optional[str] = None,
                 risk_free: float = RISK_FREE_RATE) -> Optional[float]:
    """
    Sharpe ratio = (annualised_return - risk_free) / annualised_volatility.

    Uses daily returns to compute both numerator and denominator.
    Risk-free rate default: 6.5% p.a.
    Returns float or None if insufficient data.
    """
    try:
        df = _clean(nav_df)
        if from_date:
            df = df[df["date"] >= pd.Timestamp(from_date)]
        if to_date:
            df = df[df["date"] <= pd.Timestamp(to_date)]
        rets = df.set_index("date")["nav"].pct_change().dropna()
        if len(rets) < 20:
            return None
        rf_daily   = (1 + risk_free) ** (1 / TRADING_DAYS) - 1
        excess     = rets - rf_daily
        sharpe     = float(excess.mean() / excess.std() * math.sqrt(TRADING_DAYS))
        return round(sharpe, 4) if math.isfinite(sharpe) else None
    except Exception as e:
        log.debug(f"sharpe_ratio error: {e}")
        return None


def max_drawdown(nav_df: pd.DataFrame,
                 from_date: Optional[str] = None,
                 to_date:   Optional[str] = None) -> Optional[float]:
    """
    Maximum peak-to-trough decline over the period.
    Returns decimal (e.g. -0.35 = -35%) or None.
    """
    try:
        df = _clean(nav_df)
        if from_date:
            df = df[df["date"] >= pd.Timestamp(from_date)]
        if to_date:
            df = df[df["date"] <= pd.Timestamp(to_date)]
        if len(df) < 2:
            return None
        nav    = df["nav"].values
        peak   = np.maximum.accumulate(nav)
        dd     = (nav - peak) / peak
        mdd    = float(dd.min())
        return round(mdd, 6) if math.isfinite(mdd) else None
    except Exception as e:
        log.debug(f"max_drawdown error: {e}")
        return None


def rolling_returns(nav_df: pd.DataFrame, window_days: int = 365) -> pd.DataFrame:
    """
    Rolling annualised return over a sliding window.
    Returns DataFrame [date, rolling_return].
    """
    try:
        df = _clean(nav_df)
        if len(df) < window_days // 5:
            return pd.DataFrame(columns=["date", "rolling_return"])
        nav      = df.set_index("date")["nav"]
        shifted  = nav.shift(window_days)
        roll_ret = (nav / shifted) ** (365.0 / window_days) - 1
        result   = roll_ret.dropna().reset_index()
        result.columns = ["date", "rolling_return"]
        result["rolling_return"] = result["rolling_return"].apply(
            lambda v: round(v, 6) if math.isfinite(v) else None
        )
        return result
    except Exception as e:
        log.debug(f"rolling_returns error: {e}")
        return pd.DataFrame(columns=["date", "rolling_return"])


def full_metrics(nav_df: pd.DataFrame,
                 from_date: Optional[str] = None,
                 to_date:   Optional[str] = None,
                 risk_free: float = RISK_FREE_RATE) -> dict:
    """
    Compute all metrics for a fund over a date range.

    Returns dict:
        annualised_return, annualised_volatility, sharpe_ratio,
        max_drawdown, start_nav, end_nav, start_date, end_date,
        trading_days, risk_free_used
    """
    df = _clean(nav_df)
    if from_date:
        df = df[df["date"] >= pd.Timestamp(from_date)]
    if to_date:
        df = df[df["date"] <= pd.Timestamp(to_date)]

    if df.empty:
        return {
            "annualised_return"    : None,
            "annualised_volatility": None,
            "sharpe_ratio"         : None,
            "max_drawdown"         : None,
            "start_nav"            : None,
            "end_nav"              : None,
            "start_date"           : None,
            "end_date"             : None,
            "trading_days"         : 0,
            "risk_free_used"       : risk_free,
        }

    return {
        "annualised_return"    : annualised_return(df),
        "annualised_volatility": annualised_volatility(df),
        "sharpe_ratio"         : sharpe_ratio(df, risk_free=risk_free),
        "max_drawdown"         : max_drawdown(df),
        "start_nav"            : round(float(df["nav"].iloc[0]), 4),
        "end_nav"              : round(float(df["nav"].iloc[-1]), 4),
        "start_date"           : str(df["date"].iloc[0].date()),
        "end_date"             : str(df["date"].iloc[-1].date()),
        "trading_days"         : len(df),
        "risk_free_used"       : risk_free,
    }


def compare_metrics(
    nav_map   : dict,          # {name: nav_df}
    from_date : Optional[str] = None,
    to_date   : Optional[str] = None,
    risk_free : float         = RISK_FREE_RATE,
) -> pd.DataFrame:
    """
    Compute full_metrics for multiple funds and return a comparison DataFrame.

    Args:
        nav_map  : {fund_name: nav_df}
        from_date: 'YYYY-MM-DD' optional start
        to_date  : 'YYYY-MM-DD' optional end
        risk_free: annualised risk-free rate

    Returns:
        DataFrame with one row per fund, columns:
            Fund, Ann. Return %, Volatility %, Sharpe,
            Max Drawdown %, Start NAV, End NAV,
            Start Date, End Date, Trading Days
    """
    rows = []
    for name, nav_df in nav_map.items():
        try:
            m = full_metrics(nav_df, from_date, to_date, risk_free)
            rows.append({
                "Fund"            : name,
                "Ann. Return %"   : round(m["annualised_return"]     * 100, 2) if m["annualised_return"]     is not None else None,
                "Volatility %"    : round(m["annualised_volatility"]  * 100, 2) if m["annualised_volatility"] is not None else None,
                "Sharpe"          : m["sharpe_ratio"],
                "Max Drawdown %"  : round(m["max_drawdown"]           * 100, 2) if m["max_drawdown"]          is not None else None,
                "Start NAV"       : m["start_nav"],
                "End NAV"         : m["end_nav"],
                "Start Date"      : m["start_date"],
                "End Date"        : m["end_date"],
                "Trading Days"    : m["trading_days"],
            })
        except Exception as e:
            log.warning(f"compare_metrics: error for '{name}': {e}")
            rows.append({"Fund": name, "error": str(e)})
    return pd.DataFrame(rows)