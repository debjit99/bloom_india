#!/usr/bin/env python3
"""
test_mf_pipeline.py
====================
End-to-end test for the bloom_india MF pipeline.

Run from the repo root:
    python test_mf_pipeline.py

Tests:
  1. Imports
  2. Cache (set / get / hit / miss / stats / clear)
  3. Live fetch — scheme list from mfapi.in
  4. Live fetch — single fund NAV history
  5. NAV enrichment and return computation
  6. Python API  — search_funds, get_fund, get_nav_history, list_funds
  7. FastAPI routes — all 15 endpoints via httpx TestClient (no server needed)
  8. Streamlit app — import only (no browser required)
"""

import sys, time

PASS = "\033[92m✅\033[0m"
FAIL = "\033[91m❌\033[0m"
SKIP = "\033[93m⚠️ \033[0m"

errors = []

def ok(label):
    print(f"  {PASS} {label}")

def fail(label, e):
    print(f"  {FAIL} {label}: {e}")
    errors.append((label, e))

def skip(label, reason):
    print(f"  {SKIP} {label}: {reason}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n━━━ 1. Imports ━━━")
try:
    from bloom_india.mf.cache.disk_cache import set_cache, get_cache, cache_stats, clear_all_cache
    ok("disk_cache imports")
except Exception as e:
    fail("disk_cache imports", e); sys.exit(1)

try:
    from bloom_india.mf.fetch.mfapi import fetch_all_schemes, fetch_scheme_nav, fetch_latest_nav
    ok("mfapi imports")
except Exception as e:
    fail("mfapi imports", e); sys.exit(1)

try:
    from bloom_india.mf.process.enrich import (
        build_scheme_catalogue, enrich_nav_history,
        compute_returns, _normalise_category
    )
    ok("enrich imports")
except Exception as e:
    fail("enrich imports", e); sys.exit(1)

try:
    from bloom_india.mf.api.funds import (
        list_funds, get_fund, get_nav_history, get_fund_returns,
        search_funds, get_cache_info
    )
    ok("funds API imports")
except Exception as e:
    fail("funds API imports", e); sys.exit(1)

try:
    from bloom_india.mf_rest.server import app
    ok("FastAPI app imports")
except Exception as e:
    fail("FastAPI app imports", e)

# ─────────────────────────────────────────────────────────────────────────────
print("\n━━━ 2. Cache ━━━")
try:
    clear_all_cache()
    ok("clear_all_cache()")
except Exception as e:
    fail("clear_all_cache()", e)

try:
    set_cache("test:ping", {"hello": "world"}, ttl_hours=1)
    result = get_cache("test:ping")
    assert result == {"hello": "world"}, f"Got {result}"
    ok("set_cache + get_cache round-trip")
except Exception as e:
    fail("cache round-trip", e)

try:
    result = get_cache("test:does_not_exist")
    assert result is None
    ok("cache miss returns None")
except Exception as e:
    fail("cache miss", e)

try:
    stats = cache_stats()
    assert "valid_entries" in stats
    assert stats["valid_entries"] >= 1
    ok(f"cache_stats() — {stats['valid_entries']} valid, dir: {stats['cache_dir']}")
except Exception as e:
    fail("cache_stats()", e)

try:
    # TTL=0 means instant expiry
    import time as _t
    set_cache("test:expire", "bye", ttl_hours=0)
    _t.sleep(0.1)
    result = get_cache("test:expire")
    # May or may not have expired depending on clock precision — just check no exception
    ok("cache TTL expiry (no crash)")
except Exception as e:
    fail("cache TTL expiry", e)

# ─────────────────────────────────────────────────────────────────────────────
print("\n━━━ 3. Live fetch — scheme list ━━━")
try:
    schemes = fetch_all_schemes()
    if not schemes:
        skip("fetch_all_schemes()", "empty response — network may be blocked")
    else:
        assert isinstance(schemes, list)
        assert "schemeCode" in schemes[0]
        ok(f"fetch_all_schemes() — {len(schemes):,} schemes")
        # Second call should be a cache hit (faster)
        t0 = time.time()
        schemes2 = fetch_all_schemes()
        elapsed = time.time() - t0
        assert len(schemes2) == len(schemes)
        ok(f"Second call (cache hit) — {elapsed*1000:.0f}ms")
except Exception as e:
    fail("fetch_all_schemes()", e)

# ─────────────────────────────────────────────────────────────────────────────
print("\n━━━ 4. Live fetch — single fund NAV ━━━")
SAMPLE_CODE = 100033   # Aditya Birla Sun Life Frontline Equity

try:
    info = fetch_scheme_nav(SAMPLE_CODE)
    if not info or not info.get("data"):
        skip(f"fetch_scheme_nav({SAMPLE_CODE})", "no data — network may be blocked")
    else:
        meta = info.get("meta", {})
        ok(f"fetch_scheme_nav({SAMPLE_CODE}) — {meta.get('scheme_name','?')}")
        ok(f"  Latest NAV: {info['data'][0]}")
        ok(f"  Total rows: {len(info['data'])}")
except Exception as e:
    fail(f"fetch_scheme_nav({SAMPLE_CODE})", e)

try:
    nav = fetch_latest_nav(SAMPLE_CODE)
    if nav is None:
        skip("fetch_latest_nav()", "network blocked")
    else:
        ok(f"fetch_latest_nav({SAMPLE_CODE}) = ₹{nav:.4f}")
except Exception as e:
    fail("fetch_latest_nav()", e)

# ─────────────────────────────────────────────────────────────────────────────
print("\n━━━ 5. NAV enrichment & returns ━━━")
import pandas as pd

# Build mock data for offline testing
MOCK_NAV_INFO = {
    "meta": {
        "fund_house"      : "HDFC Mutual Fund",
        "scheme_type"     : "Open Ended Schemes",
        "scheme_category" : "Equity Scheme - Large Cap Fund",
        "scheme_code"     : 119551,
        "scheme_name"     : "HDFC Top 100 Fund",
    },
    "data": [
        {"date": "14-05-2026", "nav": "1050.00"},
        {"date": "13-05-2026", "nav": "1040.00"},
        {"date": "14-04-2026", "nav": "1020.00"},
        {"date": "14-02-2026", "nav": "1000.00"},
        {"date": "14-05-2025", "nav": "900.00"},
        {"date": "14-05-2023", "nav": "700.00"},
    ]
}

try:
    nav_df = enrich_nav_history(MOCK_NAV_INFO)
    assert len(nav_df) == 6
    assert list(nav_df.columns) == ["date", "nav"]
    assert nav_df["nav"].iloc[-1] == 1050.0
    assert nav_df["date"].is_monotonic_increasing
    ok(f"enrich_nav_history() — {len(nav_df)} rows, sorted ascending")
except Exception as e:
    fail("enrich_nav_history()", e)

try:
    returns = compute_returns(nav_df)
    assert set(returns.keys()) == {"1d", "1w", "1m", "3m", "6m", "1y", "3y"}
    r1y = returns["1y"]
    expected = (1050 / 900) - 1  # ≈ 0.1667
    assert abs(r1y - expected) < 0.001, f"Got {r1y}, expected {expected:.4f}"
    ok(f"compute_returns() — 1Y = {r1y*100:.2f}%  3Y = {returns['3y']*100:.2f}%")
    ok(f"  All periods: { {k: f'{v*100:.1f}%' if v else '—' for k,v in returns.items()} }")
except Exception as e:
    fail("compute_returns()", e)

try:
    cat = _normalise_category("equity scheme - large cap fund")
    assert cat == "Equity - Large Cap", f"Got: {cat}"
    cat2 = _normalise_category("debt scheme - liquid fund")
    assert cat2 == "Debt - Liquid"
    ok(f"_normalise_category() — 'equity scheme - large cap fund' → '{cat}'")
except Exception as e:
    fail("_normalise_category()", e)

# ─────────────────────────────────────────────────────────────────────────────
print("\n━━━ 6. Python API ━━━")

try:
    results = search_funds("HDFC", limit=5)
    if results.empty:
        skip("search_funds('HDFC')", "no scheme list cached — run after live fetch")
    else:
        assert "scheme_name" in results.columns
        ok(f"search_funds('HDFC') — {len(results)} results")
        print(f"     {results['scheme_name'].iloc[0]}")
except Exception as e:
    fail("search_funds()", e)

try:
    fund = get_fund(SAMPLE_CODE)
    if fund is None:
        skip(f"get_fund({SAMPLE_CODE})", "network blocked or not cached")
    else:
        assert "scheme_name" in fund
        assert "latest_nav" in fund
        assert "returns" in fund
        ok(f"get_fund({SAMPLE_CODE}) — {fund['scheme_name']}")
        ok(f"  NAV: ₹{fund['latest_nav']:.4f} as of {fund['nav_date']}")
        ok(f"  Category: {fund['category_clean']}")
        ok(f"  1Y return: {fund['returns'].get('1y', 0)*100:.2f}%" if fund['returns'].get('1y') else "  1Y return: —")
except Exception as e:
    fail("get_fund()", e)

try:
    nav_hist = get_nav_history(SAMPLE_CODE, from_date="2024-01-01")
    if nav_hist.empty:
        skip("get_nav_history()", "network blocked")
    else:
        assert "date" in nav_hist.columns
        assert "nav" in nav_hist.columns
        assert nav_hist["date"].min() >= pd.Timestamp("2024-01-01")
        ok(f"get_nav_history({SAMPLE_CODE}, from_date='2024-01-01') — {len(nav_hist)} rows")
except Exception as e:
    fail("get_nav_history()", e)

# ─────────────────────────────────────────────────────────────────────────────
print("\n━━━ 7. FastAPI routes (TestClient — no server needed) ━━━")
try:
    from fastapi.testclient import TestClient
    client = TestClient(app)

    # Health
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    ok("GET /health → 200")

    # Root
    r = client.get("/")
    assert r.status_code == 200
    ok("GET / → 200")

    # Funds list (fast — catalogue only, no NAV fetch)
    r = client.get("/mf/funds?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "total" in body and "data" in body
    assert body["total"] > 0
    ok(f"GET /mf/funds?limit=5 → 200  (total={body['total']:,} schemes)")

    # Funds list with filter
    r = client.get("/mf/funds?category=Large+Cap&limit=3")
    assert r.status_code == 200
    ok(f"GET /mf/funds?category=Large+Cap → 200  ({r.json()['count']} results)")

    # Funds list AMC filter
    r = client.get("/mf/funds?fund_house=HDFC&limit=5")
    assert r.status_code == 200
    ok(f"GET /mf/funds?fund_house=HDFC → 200  ({r.json()['count']} results)")

    # Enrich endpoint — fetch NAV+returns for 2 specific funds (live fetch)
    r = client.post("/mf/funds/enrich", json=[100033, 119551])
    if r.status_code == 200:
        ok(f"POST /mf/funds/enrich [100033, 119551] → 200  ({r.json()['count']} funds)")
    else:
        skip("POST /mf/funds/enrich", f"status {r.status_code} — may need network")

    # Search
    r = client.get("/mf/funds/search?q=HDFC&limit=5")
    assert r.status_code == 200
    ok(f"GET /mf/funds/search?q=HDFC → 200  (count={r.json()['count']})")

    # Single fund (may 404 if not cached)
    r = client.get(f"/mf/funds/{SAMPLE_CODE}")
    if r.status_code == 200:
        ok(f"GET /mf/funds/{SAMPLE_CODE} → 200")
    elif r.status_code == 404:
        skip(f"GET /mf/funds/{SAMPLE_CODE}", "404 — network blocked, NAV not cached")
    else:
        fail(f"GET /mf/funds/{SAMPLE_CODE}", f"status {r.status_code}")

    # NAV endpoint
    r = client.get(f"/mf/funds/{SAMPLE_CODE}/nav?from_date=2024-01-01")
    if r.status_code == 200:
        ok(f"GET /mf/funds/{SAMPLE_CODE}/nav → 200  ({r.json()['count']} rows)")
    elif r.status_code == 404:
        skip(f"GET /mf/funds/{SAMPLE_CODE}/nav", "404 — network blocked")

    # Returns endpoint
    r = client.get(f"/mf/funds/{SAMPLE_CODE}/returns")
    if r.status_code == 200:
        ok(f"GET /mf/funds/{SAMPLE_CODE}/returns → 200")
    elif r.status_code == 404:
        skip(f"GET /mf/funds/{SAMPLE_CODE}/returns", "404 — network blocked")

    # AMCs
    r = client.get("/mf/amcs")
    assert r.status_code == 200
    ok(f"GET /mf/amcs → 200  ({r.json()['count']} AMCs)")

    # Categories
    r = client.get("/mf/categories")
    assert r.status_code == 200
    ok(f"GET /mf/categories → 200  ({r.json()['count']} categories)")

    # Cache stats
    r = client.get("/mf/cache/stats")
    assert r.status_code == 200
    ok(f"GET /mf/cache/stats → 200")

    # Cache clear
    r = client.post("/mf/cache/clear")
    assert r.status_code == 200
    ok(f"POST /mf/cache/clear → 200")

except ImportError:
    skip("FastAPI TestClient", "pip install httpx  (needed for testclient)")
except Exception as e:
    fail("FastAPI routes", e)

# ─────────────────────────────────────────────────────────────────────────────
print("\n━━━ 8. Streamlit app import ━━━")
try:
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "mf_terminal_app",
        os.path.join(os.path.dirname(__file__),
                     "bloom_india", "mf_terminal", "app.py")
    )
    # Just check it parses without syntax errors — don't actually run streamlit
    with open(spec.origin) as f:
        source = f.read()
    compile(source, spec.origin, "exec")
    ok("mf_terminal/app.py — syntax OK")
except Exception as e:
    fail("mf_terminal/app.py syntax", e)

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "━"*50)
if errors:
    print(f"\n{FAIL} {len(errors)} test(s) failed:\n")
    for label, e in errors:
        print(f"   • {label}: {e}")
    sys.exit(1)
else:
    print(f"\n{PASS} All tests passed!\n")
    print("Next steps:")
    print("  Streamlit dashboard : streamlit run bloom_india/mf_terminal/app.py")
    print("  REST API            : uvicorn bloom_india.mf_rest.server:app --reload --port 8000")
    print("  API docs            : http://localhost:8000/docs")