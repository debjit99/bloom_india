"""
bloom_india/mf_rest/server.py
==============================
FastAPI REST API for the bloom_india Mutual Fund module.

Run with:
    uvicorn bloom_india.mf_rest.server:app --host 0.0.0.0 --port 8000 --reload

Or via CLI shortcut (added to pyproject.toml):
    bloom-india-mf-api

Endpoints
---------
  GET  /mf/funds                  → list funds (filterable)
  GET  /mf/funds/search           → search by name / AMC
  GET  /mf/funds/{scheme_code}    → single fund detail
  GET  /mf/funds/{scheme_code}/nav → NAV history
  GET  /mf/funds/{scheme_code}/returns → trailing returns
  GET  /mf/amcs                   → list distinct AMCs
  GET  /mf/categories             → list distinct categories
  GET  /mf/cache/stats            → cache diagnostics
  POST /mf/cache/clear            → clear all cache

All list endpoints support:
  ?limit=100  ?offset=0  (pagination)
  ?sort_by=<col>  ?ascending=true

Caching: all GET responses are cached at the application layer with
  Cache-Control headers and an in-process TTL dict.
"""

from __future__ import annotations

import time
import functools
import logging
from typing import Any, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bloom_india.mf.api.funds import (
    list_funds,
    get_fund,
    get_fund_snapshot,
    get_nav_history,
    get_fund_returns,
    search_funds,
    get_cache_info,
)
from bloom_india.mf.fetch.mfapi import fetch_all_schemes
from bloom_india.mf.process.enrich import build_scheme_catalogue, _normalise_category
from bloom_india.mf.cache.disk_cache import clear_all_cache
from bloom_india.mf.db.nav_db import db_stats, list_schemes_in_db
from bloom_india.mf.analytics.pnl import (
    lumpsum_pnl, sip_pnl, compare_pnl,
    PnLError, DateRangeError, InsufficientDataError,
)

import math
import json

log = logging.getLogger(__name__)


def _safe_json(obj):
    """Recursively replace nan/inf with None for JSON safety."""
    if isinstance(obj, dict):
        return {k: _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_json(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "bloom_india MF API",
    description = "Open mutual fund data for Indian markets — no auth required.",
    version     = "1.0.0",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["GET", "POST"],
    allow_headers  = ["*"],
)


# ── In-process response cache ─────────────────────────────────────────────────

_RESP_CACHE: dict[str, tuple[float, Any]] = {}   # key → (expire_ts, payload)
RESP_TTL    = 300   # 5 minutes


def _cached_response(key: str, fn, *args, **kwargs):
    now = time.time()
    if key in _RESP_CACHE:
        exp, val = _RESP_CACHE[key]
        if now < exp:
            return val
    result = fn(*args, **kwargs)
    _RESP_CACHE[key] = (now + RESP_TTL, result)
    return result


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-safe list of dicts (no NaN, no inf)."""
    if df.empty:
        return []
    import math
    df = df.copy()
    for col in df.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns:
        df[col] = df[col].dt.strftime("%Y-%m-%d")
    records = df.where(pd.notna(df), None).to_dict(orient="records")
    # Replace any remaining float nan/inf that slipped through
    clean = []
    for row in records:
        clean.append({
            k: (None if isinstance(v, float) and not math.isfinite(v) else v)
            for k, v in row.items()
        })
    return clean


# ── Helpers ───────────────────────────────────────────────────────────────────

def _paginate(records: list, limit: int, offset: int) -> dict:
    total  = len(records)
    sliced = records[offset: offset + limit]
    return {
        "total" : total,
        "limit" : limit,
        "offset": offset,
        "count" : len(sliced),
        "data"  : sliced,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", tags=["health"])
def root():
    return {"service": "bloom_india MF API", "status": "ok", "version": "1.0.0"}


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "timestamp": time.time()}


@app.get("/mf/funds", tags=["funds"], summary="List mutual funds (fast — name + code only)")
def api_list_funds(
    fund_house   : Optional[str]   = Query(None, description="AMC name (partial match)"),
    category     : Optional[str]   = Query(None, description="Keyword in scheme name"),
    scheme_type  : Optional[str]   = Query(None, description="Keyword in scheme name"),
    sort_by      : str             = Query("scheme_name", description="scheme_name | fund_house | scheme_code"),
    ascending    : bool            = Query(True,  description="Sort direction"),
    limit        : int             = Query(50,  ge=1, le=1000, description="Page size"),
    offset       : int             = Query(0,   ge=0,          description="Page offset"),
):
    """
    Fast fund listing from the scheme catalogue (37k+ schemes, no NAV fetch).
    Returns: scheme_code, scheme_name, fund_house.
    For NAV + returns on a subset use POST /mf/funds/enrich.
    """
    cache_key = f"list:{fund_house}:{category}:{scheme_type}:{sort_by}:{ascending}"
    df        = _cached_response(cache_key, list_funds,
                                 fund_house=fund_house, category=category,
                                 scheme_type=scheme_type, sort_by=sort_by,
                                 ascending=ascending)
    records = _df_to_records(df)
    return JSONResponse(_safe_json(_paginate(records, limit, offset)))


@app.post("/mf/funds/enrich", tags=["funds"],
          summary="Get NAV + returns for a list of scheme codes")
def api_enrich_funds(
    scheme_codes: list[int],
    limit: int = Query(50, ge=1, le=50, description="Max 50 per call"),
):
    """
    Fetch full NAV + returns snapshot for a specific set of scheme codes.
    Capped at 50 per call to keep response times reasonable.
    Each fund result is individually disk-cached (6h TTL).
    """
    codes = scheme_codes[:limit]
    cache_key = f"enrich:{sorted(codes)}"
    df = _cached_response(cache_key, get_fund_snapshot, scheme_codes=codes)
    records = _df_to_records(df)
    return {"count": len(records), "data": records}


@app.get("/mf/funds/search", tags=["funds"], summary="Search funds by name or AMC")
def api_search_funds(
    q      : str = Query(..., description="Search query"),
    limit  : int = Query(20, ge=1, le=200),
    offset : int = Query(0,  ge=0),
):
    df      = _cached_response(f"search:{q}", search_funds, query=q, limit=limit + offset)
    records = _df_to_records(df)
    return JSONResponse(_safe_json(_paginate(records, limit, offset)))


@app.get("/mf/funds/{scheme_code}", tags=["funds"], summary="Get a single fund")
def api_get_fund(scheme_code: int):
    fund = _cached_response(f"fund:{scheme_code}", get_fund, scheme_code)
    if fund is None:
        raise HTTPException(status_code=404, detail=f"Scheme {scheme_code} not found.")
    return JSONResponse(_safe_json(fund))


@app.get("/mf/funds/{scheme_code}/nav", tags=["funds"], summary="NAV history")
def api_get_nav(
    scheme_code: int,
    from_date  : Optional[str] = Query(None, description="YYYY-MM-DD"),
    to_date    : Optional[str] = Query(None, description="YYYY-MM-DD"),
):
    cache_key = f"nav:{scheme_code}:{from_date}:{to_date}"
    df        = _cached_response(cache_key, get_nav_history,
                                  scheme_code, from_date, to_date)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No NAV data for {scheme_code}.")
    records = _df_to_records(df)
    return JSONResponse(_safe_json({"scheme_code": scheme_code, "count": len(records), "nav": records}))


@app.get("/mf/funds/{scheme_code}/returns", tags=["funds"], summary="Trailing returns")
def api_get_returns(scheme_code: int):
    returns = _cached_response(f"returns:{scheme_code}", get_fund_returns, scheme_code)
    if returns is None:
        raise HTTPException(status_code=404, detail=f"Scheme {scheme_code} not found.")
    return JSONResponse(_safe_json({"scheme_code": scheme_code, "returns": returns}))


@app.get("/mf/amcs", tags=["meta"], summary="List all AMCs / fund houses")
def api_list_amcs():
    def _build():
        cat = build_scheme_catalogue()
        return sorted(cat["fund_house"].dropna().unique().tolist())
    amcs = _cached_response("amcs", _build)
    return {"count": len(amcs), "amcs": amcs}


@app.get("/mf/categories", tags=["meta"], summary="List all scheme categories")
def api_list_categories():
    def _build():
        schemes = fetch_all_schemes()
        # We only have category from the NAV endpoint — use scheme name heuristics
        # to give a quick response without fetching all NAVs
        cats = set()
        for s in schemes:
            name = s.get("schemeName", "")
            for kw in ["Equity", "Debt", "Hybrid", "Index", "ETF", "Liquid", "ELSS",
                       "Gilt", "Credit", "Overnight", "Arbitrage", "Fund of Fund"]:
                if kw.lower() in name.lower():
                    cats.add(kw)
        return sorted(cats)
    categories = _cached_response("categories", _build)
    return {"count": len(categories), "categories": categories}


@app.get("/mf/cache/stats", tags=["admin"], summary="Cache diagnostics")
def api_cache_stats():
    return get_cache_info()


@app.post("/mf/cache/clear", tags=["admin"], summary="Clear all cache")
def api_cache_clear():
    _RESP_CACHE.clear()
    n = clear_all_cache()
    return {"message": f"Cache cleared. {n} disk entries removed."}


# ── DB routes ─────────────────────────────────────────────────────────────────

@app.get("/mf/db/stats", tags=["db"], summary="NAV database statistics")
def api_db_stats():
    return JSONResponse(_safe_json(db_stats()))


@app.get("/mf/db/schemes", tags=["db"], summary="Schemes stored in NAV database")
def api_db_schemes(
    limit : int = Query(100, ge=1, le=1000),
    offset: int = Query(0,   ge=0),
):
    df      = list_schemes_in_db()
    records = _df_to_records(df)
    return JSONResponse(_safe_json(_paginate(records, limit, offset)))


@app.post("/mf/db/seed", tags=["db"], summary="Seed NAV history for scheme codes")
def api_db_seed(
    scheme_codes: list[int],
    workers: int = Query(4, ge=1, le=8),
):
    """
    Trigger full NAV history download for a list of scheme codes.
    Idempotent — skips schemes already in DB.
    """
    from bloom_india.mf.db.ingest import seed_all_schemes, summary as ingest_summary
    if len(scheme_codes) > 200:
        raise HTTPException(status_code=400, detail="Max 200 scheme codes per call")
    results = seed_all_schemes(scheme_codes=scheme_codes, workers=workers)
    return _safe_json(ingest_summary(results))


@app.post("/mf/db/update", tags=["db"], summary="Update NAV DB with latest dates")
def api_db_update(
    scheme_codes: Optional[list[int]] = None,
    workers: int = Query(4, ge=1, le=8),
):
    """
    Incremental update — only fetches dates after what's already in DB.
    Designed for the daily cron job. If scheme_codes is null, updates all DB schemes.
    """
    from bloom_india.mf.db.ingest import update_all_schemes, summary as ingest_summary
    results = update_all_schemes(scheme_codes=scheme_codes, workers=workers)
    return _safe_json(ingest_summary(results))


# ── P&L routes ────────────────────────────────────────────────────────────────

@app.get("/mf/pnl/lumpsum", tags=["pnl"], summary="Lump sum P&L for a fund")
def api_lumpsum_pnl(
    scheme_code: int   = Query(..., description="mfapi.in scheme code"),
    buy_date   : str   = Query(..., description="Investment date YYYY-MM-DD"),
    sell_date  : str   = Query(..., description="Redemption date YYYY-MM-DD"),
    amount     : float = Query(10000.0, gt=0, description="Investment amount in INR"),
):
    try:
        result = lumpsum_pnl(scheme_code, buy_date, sell_date, amount)
        return JSONResponse(_safe_json(result))
    except (ValueError, PnLError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error(f"[api] lumpsum_pnl error: {e}")
        raise HTTPException(status_code=500, detail="Internal error computing P&L")


@app.get("/mf/pnl/sip", tags=["pnl"], summary="SIP P&L for a fund")
def api_sip_pnl(
    scheme_code    : int   = Query(..., description="mfapi.in scheme code"),
    start_date     : str   = Query(..., description="First SIP date YYYY-MM-DD"),
    end_date       : str   = Query(..., description="Redemption date YYYY-MM-DD"),
    monthly_amount : float = Query(5000.0, gt=0, description="Amount per instalment in INR"),
    frequency_days : int   = Query(30, ge=1, le=365, description="Days between instalments"),
):
    try:
        result = sip_pnl(scheme_code, start_date, end_date, monthly_amount, frequency_days)
        return JSONResponse(_safe_json(result))
    except (ValueError, PnLError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error(f"[api] sip_pnl error: {e}")
        raise HTTPException(status_code=500, detail="Internal error computing P&L")


@app.post("/mf/pnl/compare", tags=["pnl"], summary="Compare P&L across multiple funds")
def api_compare_pnl(
    scheme_codes: list[int],
    buy_date    : str   = Query(..., description="Investment date YYYY-MM-DD"),
    sell_date   : str   = Query(..., description="Redemption date YYYY-MM-DD"),
    amount      : float = Query(10000.0, gt=0),
    mode        : str   = Query("lumpsum", description="lumpsum | sip"),
):
    if len(scheme_codes) > 20:
        raise HTTPException(status_code=400, detail="Max 20 funds per comparison")
    results = compare_pnl(scheme_codes, buy_date, sell_date, amount, mode)
    return JSONResponse(_safe_json({"count": len(results), "results": results}))
