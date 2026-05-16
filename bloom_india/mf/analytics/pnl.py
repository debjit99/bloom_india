"""
bloom_india/mf/analytics/pnl.py
================================
P&L (Profit & Loss) calculator for mutual fund investments.

Supports
--------
  - Lump sum: invest a fixed amount on one date, exit on another.
  - SIP: invest a fixed amount every N days (default 30) over a date range.
  - Multiple funds side-by-side for comparison.

Data source priority
--------------------
  1. SQLite DB (nav_db) — preferred, no network needed.
  2. mfapi.in live fetch → stored in DB — fallback if DB missing data.

Corner cases handled
--------------------
  - buy_date not a trading day  → use next available NAV date.
  - sell_date not a trading day → use latest available NAV date ≤ sell_date.
  - buy_date > sell_date        → ValueError with clear message.
  - buy_date before fund launch → raises DateRangeError.
  - sell_date in the future     → clamps to latest available NAV.
  - Zero / negative NAV         → filtered out before any calculation.
  - No NAV data in DB           → tries live fetch; if still empty, raises.
  - Investment amount ≤ 0       → raises ValueError.
  - NaN / inf in NAV series     → filtered out.
  - DB unavailable              → falls back gracefully to live fetch.
"""

from __future__ import annotations

import math
import logging
from datetime import datetime, date, timedelta
from typing import Optional, Union

import pandas as pd
import numpy as np

log = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────

class PnLError(Exception):
    """Base error for P&L calculation failures."""

class DateRangeError(PnLError):
    """The requested date range has no usable NAV data."""

class InsufficientDataError(PnLError):
    """Not enough NAV data to compute P&L."""


# ── NAV fetcher (DB-first, API fallback) ──────────────────────────────────────

def _get_nav_series(
    scheme_code: int,
    from_date  : str,
    to_date    : str,
) -> pd.DataFrame:
    """
    Return NAV DataFrame [date, nav] for a scheme in a date range.
    Tries DB first, falls back to live API if DB has no data.
    Returned DataFrame is always sorted ascending with clean nav values.
    """
    # Try DB first
    try:
        from bloom_india.mf.db.nav_db import get_nav_history, scheme_exists
        if scheme_exists(scheme_code):
            df = get_nav_history(scheme_code, from_date=from_date, to_date=to_date)
            if not df.empty:
                return _clean_nav(df)
    except Exception as e:
        log.debug(f"[pnl] DB read failed for {scheme_code}: {e}")

    # Fallback: live fetch and store in DB
    log.info(f"[pnl] DB miss for {scheme_code}, fetching from mfapi.in…")
    try:
        from bloom_india.mf.fetch.mfapi import fetch_scheme_nav
        from bloom_india.mf.process.enrich import enrich_nav_history
        from bloom_india.mf.db.ingest import seed_scheme

        # Seed into DB (no-op if already exists)
        seed_scheme(scheme_code)

        # Try DB again
        from bloom_india.mf.db.nav_db import get_nav_history
        df = get_nav_history(scheme_code, from_date=from_date, to_date=to_date)
        if not df.empty:
            return _clean_nav(df)

        # Last resort: in-memory from API response (don't persist)
        info = fetch_scheme_nav(scheme_code)
        if info:
            df = enrich_nav_history(info)
            df = df[df["date"] >= pd.Timestamp(from_date)]
            df = df[df["date"] <= pd.Timestamp(to_date)]
            return _clean_nav(df)

    except Exception as e:
        log.error(f"[pnl] Live fetch failed for {scheme_code}: {e}")

    return pd.DataFrame(columns=["date", "nav"])


def _clean_nav(df: pd.DataFrame) -> pd.DataFrame:
    """Remove invalid NAV rows (zero, negative, NaN, inf)."""
    if df.empty:
        return df
    df = df.copy()
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    df = df.dropna(subset=["nav"])
    df = df[df["nav"] > 0]
    df = df[df["nav"].apply(math.isfinite)]
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _nearest_nav(nav_df: pd.DataFrame, target: pd.Timestamp, direction: str = "forward") -> Optional[pd.Series]:
    """
    Find the closest NAV row to target date.

    direction:
        'forward'  — first available date >= target (for buy)
        'backward' — last available date  <= target (for sell)
    """
    if nav_df.empty:
        return None
    if direction == "forward":
        candidates = nav_df[nav_df["date"] >= target]
        return candidates.iloc[0] if not candidates.empty else None
    else:
        candidates = nav_df[nav_df["date"] <= target]
        return candidates.iloc[-1] if not candidates.empty else None


def _parse_date(d: Union[str, date, datetime, pd.Timestamp]) -> pd.Timestamp:
    if isinstance(d, pd.Timestamp):
        return d
    if isinstance(d, (datetime, date)):
        return pd.Timestamp(d)
    return pd.Timestamp(str(d))


# ── Lump sum P&L ──────────────────────────────────────────────────────────────

def lumpsum_pnl(
    scheme_code : int,
    buy_date    : Union[str, date],
    sell_date   : Union[str, date],
    amount      : float = 10_000.0,
) -> dict:
    """
    Calculate P&L for a lump sum investment.

    Args:
        scheme_code : mfapi.in scheme code
        buy_date    : date of investment (adjusted to next trading day if holiday)
        sell_date   : date of redemption (adjusted to prev trading day if holiday)
        amount      : investment amount in INR (default ₹10,000)

    Returns:
        dict with:
            scheme_code, buy_date, sell_date, buy_nav, sell_nav,
            units, invested, current_value, pnl, pnl_pct,
            holding_days, cagr, xirr (None — use SIP for XIRR),
            adjusted_buy_date, adjusted_sell_date, warnings

    Raises:
        ValueError          : bad inputs (amount ≤ 0, buy > sell)
        DateRangeError      : no NAV data in the requested range
        InsufficientDataError: can't find NAV on or after buy_date
    """
    warnings = []

    # ── Input validation ──────────────────────────────────────────────────────
    if amount <= 0:
        raise ValueError(f"Investment amount must be positive, got {amount}")

    buy_ts  = _parse_date(buy_date)
    sell_ts = _parse_date(sell_date)

    if buy_ts >= sell_ts:
        raise ValueError(
            f"buy_date ({buy_ts.date()}) must be before sell_date ({sell_ts.date()})"
        )

    if buy_ts > pd.Timestamp.today():
        raise ValueError(f"buy_date ({buy_ts.date()}) is in the future")

    # ── Fetch NAVs ────────────────────────────────────────────────────────────
    from_str = buy_ts.strftime("%Y-%m-%d")
    to_str   = min(sell_ts, pd.Timestamp.today()).strftime("%Y-%m-%d")

    nav_df = _get_nav_series(scheme_code, from_str, to_str)

    if nav_df.empty:
        raise DateRangeError(
            f"No NAV data for scheme {scheme_code} between {from_str} and {to_str}. "
            "Run: python -m bloom_india.mf.db.ingest seed {scheme_code}"
        )

    # ── Find buy NAV ──────────────────────────────────────────────────────────
    buy_row = _nearest_nav(nav_df, buy_ts, direction="forward")
    if buy_row is None:
        raise InsufficientDataError(
            f"No NAV data on or after {buy_ts.date()} for scheme {scheme_code}. "
            f"Earliest available: {nav_df['date'].min().date()}"
        )

    adj_buy = buy_row["date"]
    buy_nav = float(buy_row["nav"])

    if adj_buy != buy_ts:
        warnings.append(
            f"buy_date {buy_ts.date()} was not a trading day; "
            f"adjusted to {adj_buy.date()}"
        )

    # ── Find sell NAV ─────────────────────────────────────────────────────────
    sell_row = _nearest_nav(nav_df, sell_ts, direction="backward")

    if sell_row is None:
        # sell_date before any data — unusual, raise
        raise InsufficientDataError(
            f"No NAV data on or before {sell_ts.date()} for scheme {scheme_code}"
        )

    adj_sell  = sell_row["date"]
    sell_nav  = float(sell_row["nav"])

    if adj_sell != sell_ts:
        if sell_ts > pd.Timestamp.today():
            warnings.append(
                f"sell_date {sell_ts.date()} is in the future; "
                f"using latest available NAV ({adj_sell.date()})"
            )
        else:
            warnings.append(
                f"sell_date {sell_ts.date()} was not a trading day; "
                f"adjusted to {adj_sell.date()}"
            )

    # ── Ensure buy is before sell ─────────────────────────────────────────────
    if adj_buy >= adj_sell:
        raise InsufficientDataError(
            f"After date adjustment, buy ({adj_buy.date()}) >= sell ({adj_sell.date()}). "
            "No meaningful P&L can be computed."
        )

    # ── Calculations ──────────────────────────────────────────────────────────
    units         = amount / buy_nav
    current_value = units * sell_nav
    pnl           = current_value - amount
    pnl_pct       = (pnl / amount) * 100
    holding_days  = (adj_sell - adj_buy).days

    # CAGR = (sell/buy)^(365/days) - 1
    try:
        if holding_days > 0 and buy_nav > 0:
            cagr = ((sell_nav / buy_nav) ** (365.0 / holding_days) - 1) * 100
        else:
            cagr = None
    except Exception:
        cagr = None

    return {
        "scheme_code"        : scheme_code,
        "buy_date"           : str(buy_ts.date()),
        "sell_date"          : str(sell_ts.date()),
        "adjusted_buy_date"  : str(adj_buy.date()),
        "adjusted_sell_date" : str(adj_sell.date()),
        "buy_nav"            : round(buy_nav, 4),
        "sell_nav"           : round(sell_nav, 4),
        "units"              : round(units, 4),
        "invested"           : round(amount, 2),
        "current_value"      : round(current_value, 2),
        "pnl"                : round(pnl, 2),
        "pnl_pct"            : round(pnl_pct, 4),
        "holding_days"       : holding_days,
        "cagr_pct"           : round(cagr, 4) if cagr is not None else None,
        "warnings"           : warnings,
    }


# ── SIP P&L ───────────────────────────────────────────────────────────────────

def sip_pnl(
    scheme_code     : int,
    start_date      : Union[str, date],
    end_date        : Union[str, date],
    monthly_amount  : float = 5_000.0,
    frequency_days  : int   = 30,
) -> dict:
    """
    Calculate P&L for a Systematic Investment Plan (SIP).

    Invests `monthly_amount` every `frequency_days` starting from start_date.
    Redeems all units at end_date NAV.

    Args:
        scheme_code    : mfapi.in scheme code
        start_date     : first SIP instalment date
        end_date       : redemption date
        monthly_amount : amount per instalment in INR
        frequency_days : days between instalments (default 30)

    Returns:
        dict with:
            scheme_code, start_date, end_date,
            instalments (list of each buy),
            total_invested, current_value, pnl, pnl_pct,
            xirr_pct, cagr_pct (simple, based on avg cost),
            warnings

    Raises:
        ValueError          : bad inputs
        DateRangeError      : no NAV data
        InsufficientDataError: can't compute
    """
    warnings = []

    if monthly_amount <= 0:
        raise ValueError(f"SIP amount must be positive, got {monthly_amount}")
    if frequency_days < 1:
        raise ValueError(f"frequency_days must be >= 1, got {frequency_days}")

    start_ts = _parse_date(start_date)
    end_ts   = _parse_date(end_date)

    if start_ts >= end_ts:
        raise ValueError(
            f"start_date ({start_ts.date()}) must be before end_date ({end_ts.date()})"
        )

    # ── Fetch full NAV series ─────────────────────────────────────────────────
    from_str = start_ts.strftime("%Y-%m-%d")
    to_str   = min(end_ts, pd.Timestamp.today()).strftime("%Y-%m-%d")

    nav_df = _get_nav_series(scheme_code, from_str, to_str)
    if nav_df.empty:
        raise DateRangeError(
            f"No NAV data for scheme {scheme_code} between {from_str} and {to_str}"
        )

    # ── Generate instalment dates ─────────────────────────────────────────────
    instalment_dates = []
    current = start_ts
    while current <= end_ts:
        instalment_dates.append(current)
        current += timedelta(days=frequency_days)

    if not instalment_dates:
        raise InsufficientDataError("No instalment dates generated in range")

    # ── Process each instalment ───────────────────────────────────────────────
    instalments  = []
    total_units  = 0.0
    total_invested = 0.0
    cashflows    = []   # for XIRR: (date, amount) — negative = outflow

    for inst_date in instalment_dates:
        row = _nearest_nav(nav_df, inst_date, direction="forward")
        if row is None:
            warnings.append(
                f"No NAV available on or after {inst_date.date()} — instalment skipped"
            )
            continue

        adj_date = row["date"]
        nav_val  = float(row["nav"])

        if adj_date > end_ts:
            warnings.append(
                f"Instalment {inst_date.date()} would fall after end_date — skipped"
            )
            continue

        units = monthly_amount / nav_val
        total_units    += units
        total_invested += monthly_amount
        cashflows.append((adj_date, -monthly_amount))   # outflow

        instalments.append({
            "instalment_date"    : str(inst_date.date()),
            "adjusted_date"      : str(adj_date.date()),
            "nav"                : round(nav_val, 4),
            "amount"             : monthly_amount,
            "units_purchased"    : round(units, 4),
            "cumulative_units"   : round(total_units, 4),
            "cumulative_invested": round(total_invested, 2),
        })

    if not instalments:
        raise InsufficientDataError(
            "No valid instalments could be computed — no NAV data in range"
        )

    # ── Sell at end_date NAV ──────────────────────────────────────────────────
    sell_row = _nearest_nav(nav_df, end_ts, direction="backward")
    if sell_row is None:
        raise InsufficientDataError(
            f"No NAV available on or before {end_ts.date()} for redemption"
        )

    adj_sell      = sell_row["date"]
    sell_nav      = float(sell_row["nav"])
    current_value = total_units * sell_nav
    pnl           = current_value - total_invested
    pnl_pct       = (pnl / total_invested * 100) if total_invested > 0 else 0

    if adj_sell != end_ts:
        label = "future" if end_ts > pd.Timestamp.today() else "non-trading"
        warnings.append(
            f"end_date {end_ts.date()} is {label}; "
            f"using {adj_sell.date()} for redemption NAV"
        )

    cashflows.append((adj_sell, current_value))   # inflow at redemption

    # ── XIRR ─────────────────────────────────────────────────────────────────
    xirr_pct = _compute_xirr(cashflows)

    # ── Simple CAGR based on avg cost ────────────────────────────────────────
    holding_days = (adj_sell - _parse_date(instalments[0]["adjusted_date"])).days
    avg_cost     = total_invested / total_units if total_units > 0 else None
    try:
        cagr_pct = (
            ((sell_nav / avg_cost) ** (365.0 / holding_days) - 1) * 100
            if avg_cost and holding_days > 0 and avg_cost > 0
            else None
        )
    except Exception:
        cagr_pct = None

    return {
        "scheme_code"   : scheme_code,
        "start_date"    : str(start_ts.date()),
        "end_date"      : str(end_ts.date()),
        "sell_date"     : str(adj_sell.date()),
        "sell_nav"      : round(sell_nav, 4),
        "instalments"   : instalments,
        "instalment_count": len(instalments),
        "total_invested": round(total_invested, 2),
        "total_units"   : round(total_units, 4),
        "current_value" : round(current_value, 2),
        "pnl"           : round(pnl, 2),
        "pnl_pct"       : round(pnl_pct, 4),
        "xirr_pct"      : round(xirr_pct, 4) if xirr_pct is not None else None,
        "cagr_pct"      : round(cagr_pct, 4) if cagr_pct is not None else None,
        "warnings"      : warnings,
    }


# ── Multi-fund comparison ─────────────────────────────────────────────────────

def compare_pnl(
    scheme_codes: list[int],
    buy_date    : Union[str, date],
    sell_date   : Union[str, date],
    amount      : float = 10_000.0,
    mode        : str   = "lumpsum",   # "lumpsum" | "sip"
    **kwargs,
) -> list[dict]:
    """
    Compute P&L for multiple funds over the same date range.

    Args:
        scheme_codes : list of scheme codes
        buy_date     : investment start date
        sell_date    : redemption date
        amount       : amount (lumpsum total or SIP per instalment)
        mode         : 'lumpsum' or 'sip'
        **kwargs     : extra args forwarded to lumpsum_pnl / sip_pnl

    Returns:
        List of result dicts, one per scheme.
        Failed schemes get {"scheme_code": ..., "error": "..."} entries.
        Never raises — always returns something per scheme.
    """
    results = []
    for code in scheme_codes:
        try:
            if mode == "sip":
                r = sip_pnl(
                    scheme_code    = code,
                    start_date     = buy_date,
                    end_date       = sell_date,
                    monthly_amount = amount,
                    **kwargs,
                )
            else:
                r = lumpsum_pnl(
                    scheme_code = code,
                    buy_date    = buy_date,
                    sell_date   = sell_date,
                    amount      = amount,
                    **kwargs,
                )
            results.append(r)
        except PnLError as e:
            results.append({"scheme_code": code, "error": str(e), "error_type": type(e).__name__})
        except Exception as e:
            log.error(f"[pnl] compare_pnl unexpected error for {code}: {e}")
            results.append({"scheme_code": code, "error": str(e), "error_type": "UnexpectedError"})
    return results


# ── XIRR ─────────────────────────────────────────────────────────────────────

def _compute_xirr(
    cashflows: list[tuple],  # [(pd.Timestamp, float), ...]
    guess    : float = 0.1,
    tol      : float = 1e-6,
    max_iter : int   = 200,
) -> Optional[float]:
    """
    Extended Internal Rate of Return via Newton-Raphson.
    cashflows: list of (date, amount) — negative = outflow, positive = inflow.
    Returns decimal rate (e.g. 0.15 for 15%), or None on failure.
    """
    if len(cashflows) < 2:
        return None

    try:
        dates    = [cf[0] for cf in cashflows]
        amounts  = [cf[1] for cf in cashflows]
        t0       = dates[0]
        days     = [(d - t0).days / 365.0 for d in dates]

        def npv(rate):
            return sum(a / ((1 + rate) ** t) for a, t in zip(amounts, days))

        def dnpv(rate):
            return sum(-t * a / ((1 + rate) ** (t + 1)) for a, t in zip(amounts, days))

        rate = guess
        for _ in range(max_iter):
            f  = npv(rate)
            df = dnpv(rate)
            if abs(df) < 1e-12:
                break
            rate_new = rate - f / df
            if abs(rate_new - rate) < tol:
                rate = rate_new
                break
            rate = rate_new
            # Clamp to prevent divergence
            rate = max(-0.9999, min(rate, 100.0))

        if not math.isfinite(rate):
            return None
        return rate * 100   # return as percentage

    except Exception as e:
        log.debug(f"[pnl] XIRR failed: {e}")
        return None
