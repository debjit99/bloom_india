"""
bloom_india/mf/fetch/mfapi.py
==============================
Fetches mutual fund data from mfapi.in — free, no API key required.

Endpoints used
--------------
  https://api.mfapi.in/mf               → list of all schemes
  https://api.mfapi.in/mf/<scheme_code> → NAV history for a scheme

All responses are cached on disk (see bloom_india/mf/cache/).
"""

import json
import time
import logging
import requests
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from bloom_india.mf.cache.disk_cache import get_cache, set_cache

log = logging.getLogger(__name__)

BASE_URL = "https://api.mfapi.in/mf"
REQUEST_TIMEOUT = 15          # seconds
RATE_DELAY      = 0.1         # seconds between requests (polite)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(url: str, cache_ttl_hours: int = 6) -> Optional[dict]:
    """GET with disk cache. Returns parsed JSON or None on failure."""
    cached = get_cache(url)
    if cached is not None:
        return cached

    try:
        time.sleep(RATE_DELAY)
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        set_cache(url, data, ttl_hours=cache_ttl_hours)
        return data
    except requests.RequestException as e:
        log.error(f"[mfapi] Request failed: {url} — {e}")
        return None


# ── Public fetchers ───────────────────────────────────────────────────────────

def fetch_all_schemes() -> list[dict]:
    """
    Fetch the master list of all MF schemes from mfapi.in.

    Returns:
        list of dicts with keys: schemeCode, schemeName
        Example: [{"schemeCode": 100033, "schemeName": "Aditya Birla Sun Life ..."}]
    """
    data = _get(BASE_URL, cache_ttl_hours=24)
    if data is None:
        return []
    return data if isinstance(data, list) else []


def fetch_scheme_nav(scheme_code: int) -> Optional[dict]:
    """
    Fetch full NAV history for a single scheme.

    Args:
        scheme_code: integer scheme code from mfapi.in

    Returns:
        dict with keys:
            meta: { fund_house, scheme_type, scheme_category, scheme_code, scheme_name }
            data: [ { date: "31-12-2024", nav: "123.456" }, ... ]  (newest first)
        or None if fetch failed.
    """
    url  = f"{BASE_URL}/{scheme_code}"
    data = _get(url, cache_ttl_hours=6)
    return data


def fetch_latest_nav(scheme_code: int) -> Optional[float]:
    """Return the latest NAV for a scheme as a float, or None."""
    info = fetch_scheme_nav(scheme_code)
    if info and info.get("data"):
        try:
            return float(info["data"][0]["nav"])
        except (KeyError, ValueError):
            return None
    return None
