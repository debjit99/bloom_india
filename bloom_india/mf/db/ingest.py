"""
bloom_india/mf/db/ingest.py
============================
Seeds and incrementally updates the NAV SQLite database.

Two modes
---------
  seed_scheme(scheme_code)
      First-time full history download for one fund.
      Skips silently if the scheme already has data in DB.

  update_scheme(scheme_code)
      Fetches only dates AFTER the latest stored nav_date.
      Safe to call on a schedule (cron / launchd).

  update_all_schemes(scheme_codes, workers)
      Threaded bulk update of many schemes.

  seed_all_schemes(scheme_codes, workers)
      First-time bulk seed (slow — fetches full history per fund).

Cron usage
----------
  # Update every day at 11 PM IST (after NSE close + mfapi update lag)
  0 17 * * 1-5  cd /path/to/bloom_india && python -m bloom_india.mf.db.ingest

Corner cases handled
--------------------
  - Network failure: logs warning, skips scheme, continues.
  - Partial data: insert_navs uses INSERT OR IGNORE so partial runs are safe.
  - Empty response: skipped silently.
  - Duplicate dates: INSERT OR IGNORE prevents double-writes.
  - Invalid NAVs (0, negative, NaN): filtered in insert_navs.
  - DB locked: sqlite WAL mode + retry timeout (30s).
  - Missing scheme metadata: uses fallback values.
"""

from __future__ import annotations

import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from bloom_india.mf.fetch.mfapi import fetch_scheme_nav, fetch_all_schemes
from bloom_india.mf.process.enrich import enrich_nav_history, _normalise_category
from bloom_india.mf.db.nav_db import (
    upsert_scheme,
    insert_navs,
    get_latest_nav_date,
    scheme_exists,
    ensure_schema,
    db_stats,
)

log = logging.getLogger(__name__)

_RATE_DELAY = 0.12   # seconds between API calls per thread


# ── Single-scheme operations ──────────────────────────────────────────────────

def seed_scheme(scheme_code: int, force: bool = False) -> dict:
    """
    Download full NAV history for one scheme and store in DB.

    Args:
        scheme_code : mfapi.in scheme code
        force       : if True, seed even if scheme already exists

    Returns:
        dict with keys: scheme_code, status, inserted, message
    """
    if not force and scheme_exists(scheme_code):
        latest = get_latest_nav_date(scheme_code)
        return {
            "scheme_code": scheme_code,
            "status"     : "skipped",
            "inserted"   : 0,
            "message"    : f"Already seeded. Latest date: {latest}",
        }

    try:
        time.sleep(_RATE_DELAY)
        info = fetch_scheme_nav(scheme_code)
        if not info:
            return {"scheme_code": scheme_code, "status": "error",
                    "inserted": 0, "message": "No data from API"}

        meta = info.get("meta", {})
        upsert_scheme(
            scheme_code     = scheme_code,
            scheme_name     = meta.get("scheme_name", f"Scheme {scheme_code}"),
            fund_house      = meta.get("fund_house", ""),
            scheme_type     = meta.get("scheme_type", ""),
            scheme_category = meta.get("scheme_category", ""),
            category_clean  = _normalise_category(meta.get("scheme_category", "")),
        )

        nav_df   = enrich_nav_history(info)
        inserted = insert_navs(scheme_code, nav_df)

        return {
            "scheme_code": scheme_code,
            "status"     : "seeded",
            "inserted"   : inserted,
            "message"    : f"Full history: {len(nav_df)} rows, {inserted} new",
        }

    except Exception as e:
        log.warning(f"[ingest] seed_scheme({scheme_code}) failed: {e}")
        return {"scheme_code": scheme_code, "status": "error",
                "inserted": 0, "message": str(e)}


def update_scheme(scheme_code: int) -> dict:
    """
    Fetch only NAV dates after the latest stored date.

    If scheme is not in DB at all, falls back to full seed.

    Returns:
        dict with keys: scheme_code, status, inserted, message
    """
    if not scheme_exists(scheme_code):
        return seed_scheme(scheme_code)

    latest_stored = get_latest_nav_date(scheme_code)

    try:
        time.sleep(_RATE_DELAY)
        info = fetch_scheme_nav(scheme_code)
        if not info or not info.get("data"):
            return {"scheme_code": scheme_code, "status": "error",
                    "inserted": 0, "message": "No data from API"}

        nav_df = enrich_nav_history(info)

        # Filter to only rows newer than latest stored date
        if latest_stored and not nav_df.empty:
            nav_df = nav_df[nav_df["date"].dt.strftime("%Y-%m-%d") > latest_stored]

        if nav_df.empty:
            return {"scheme_code": scheme_code, "status": "up_to_date",
                    "inserted": 0, "message": f"Already current as of {latest_stored}"}

        inserted = insert_navs(scheme_code, nav_df)
        return {
            "scheme_code": scheme_code,
            "status"     : "updated",
            "inserted"   : inserted,
            "message"    : f"+{inserted} new rows (latest was {latest_stored})",
        }

    except Exception as e:
        log.warning(f"[ingest] update_scheme({scheme_code}) failed: {e}")
        return {"scheme_code": scheme_code, "status": "error",
                "inserted": 0, "message": str(e)}


# ── Bulk operations ───────────────────────────────────────────────────────────

def _run_bulk(
    fn,
    scheme_codes: list[int],
    workers     : int = 4,
    progress_every: int = 50,
) -> list[dict]:
    """Generic threaded runner for seed/update across many schemes."""
    ensure_schema()
    results  = []
    lock     = threading.Lock()
    total    = len(scheme_codes)
    done     = [0]

    def _task(code):
        result = fn(code)
        with lock:
            done[0] += 1
            results.append(result)
            if done[0] % progress_every == 0 or done[0] == total:
                counts = {
                    s: sum(1 for r in results if r["status"] == s)
                    for s in ["seeded", "updated", "skipped", "up_to_date", "error"]
                }
                log.info(
                    f"[ingest] {done[0]}/{total} — "
                    f"seeded={counts['seeded']} updated={counts['updated']} "
                    f"skipped={counts['skipped']+counts['up_to_date']} err={counts['error']}"
                )
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_task, code): code for code in scheme_codes}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                code = futures[fut]
                log.warning(f"[ingest] Unhandled error for {code}: {e}")

    return results


def seed_all_schemes(
    scheme_codes: Optional[list[int]] = None,
    workers     : int = 4,
) -> list[dict]:
    """
    First-time bulk seed. Fetches full NAV history for every scheme.

    Args:
        scheme_codes : list of codes, or None to fetch all from mfapi.in
        workers      : thread pool size (keep ≤ 5 to be polite to mfapi.in)

    Returns:
        List of per-scheme result dicts.
    """
    if scheme_codes is None:
        all_schemes  = fetch_all_schemes()
        scheme_codes = [s["schemeCode"] for s in all_schemes]
        log.info(f"[ingest] Seeding {len(scheme_codes):,} schemes with {workers} workers")

    return _run_bulk(seed_scheme, scheme_codes, workers)


def update_all_schemes(
    scheme_codes: Optional[list[int]] = None,
    workers     : int = 4,
) -> list[dict]:
    """
    Incremental update — only fetches dates after what's already stored.
    Designed to be run daily by cron.

    Args:
        scheme_codes : specific codes to update, or None for all in DB
        workers      : thread pool size

    Returns:
        List of per-scheme result dicts.
    """
    if scheme_codes is None:
        # Only update schemes already in DB
        from bloom_india.mf.db.nav_db import list_schemes_in_db
        df = list_schemes_in_db()
        if df.empty:
            log.warning("[ingest] No schemes in DB yet. Run seed_all_schemes() first.")
            return []
        scheme_codes = df["scheme_code"].tolist()
        log.info(f"[ingest] Updating {len(scheme_codes):,} schemes from DB")

    return _run_bulk(update_scheme, scheme_codes, workers)


def summary(results: list[dict]) -> dict:
    """Summarise a bulk run result list."""
    total    = len(results)
    by_status = {}
    inserted  = 0
    errors    = []
    for r in results:
        s = r.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
        inserted += r.get("inserted", 0)
        if s == "error":
            errors.append({"scheme_code": r["scheme_code"], "message": r["message"]})
    return {
        "total"    : total,
        "by_status": by_status,
        "inserted" : inserted,
        "errors"   : errors[:20],   # cap error list
    }


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s %(levelname)s %(message)s",
    )

    mode = sys.argv[1] if len(sys.argv) > 1 else "update"

    if mode == "seed":
        codes = [int(x) for x in sys.argv[2:]] if len(sys.argv) > 2 else None
        log.info("Mode: seed")
        results = seed_all_schemes(scheme_codes=codes, workers=4)
    elif mode == "update":
        codes = [int(x) for x in sys.argv[2:]] if len(sys.argv) > 2 else None
        log.info("Mode: update")
        results = update_all_schemes(scheme_codes=codes, workers=4)
    elif mode == "stats":
        import json
        print(json.dumps(db_stats(), indent=2))
        sys.exit(0)
    else:
        print(f"Usage: python -m bloom_india.mf.db.ingest [seed|update|stats] [scheme_codes...]")
        sys.exit(1)

    s = summary(results)
    log.info(f"Done — {s}")
