"""
bloom_india/mf/cache/disk_cache.py
===================================
Simple file-based cache with TTL for MF API responses.

Cache files are stored under:
    <bloom_india_data>/mf_cache/<hash>.json

The cache avoids hammering mfapi.in on every run.
TTL defaults to 6 hours for NAV data; 24 hours for scheme list.
"""

import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)

# Default cache directory — can be overridden by config
_DEFAULT_CACHE_DIR = Path.home() / "bloom_india_data" / "mf_cache"
_CACHE_DIR: Optional[Path] = None


def _cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is not None:
        return _CACHE_DIR
    # Try to load from bloom_india config, fall back to default
    try:
        from bloom_india.config import CONFIG
        base = Path(CONFIG.storage.base_dir).expanduser()
        _CACHE_DIR = base / "mf_cache"
    except Exception:
        _CACHE_DIR = _DEFAULT_CACHE_DIR
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def _key_to_path(key: str) -> Path:
    """Hash the cache key to a filename."""
    h = hashlib.sha256(key.encode()).hexdigest()[:24]
    return _cache_dir() / f"{h}.json"


def get_cache(key: str) -> Optional[Any]:
    """
    Retrieve cached value for key.

    Returns:
        Deserialized value, or None if not found / expired.
    """
    path = _key_to_path(key)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            envelope = json.load(f)
        expires_at = datetime.fromisoformat(envelope["expires_at"])
        if datetime.utcnow() > expires_at:
            path.unlink(missing_ok=True)
            return None
        return envelope["value"]
    except Exception as e:
        log.debug(f"[cache] Read error for {key}: {e}")
        return None


def set_cache(key: str, value: Any, ttl_hours: int = 6) -> None:
    """
    Store value in cache with a TTL.

    Args:
        key       : cache key (URL or logical key)
        value     : JSON-serialisable value
        ttl_hours : time-to-live in hours
    """
    path = _key_to_path(key)
    expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)
    envelope = {
        "key"       : key,
        "expires_at": expires_at.isoformat(),
        "value"     : value,
    }
    try:
        with open(path, "w") as f:
            json.dump(envelope, f)
    except Exception as e:
        log.warning(f"[cache] Write error for {key}: {e}")


def invalidate_cache(key: str) -> None:
    """Force expire a cache entry."""
    _key_to_path(key).unlink(missing_ok=True)


def clear_all_cache() -> int:
    """Delete all cache files. Returns number of files deleted."""
    d = _cache_dir()
    count = 0
    for f in d.glob("*.json"):
        f.unlink()
        count += 1
    log.info(f"[cache] Cleared {count} entries.")
    return count


def cache_stats() -> dict:
    """Return basic stats about the cache directory."""
    d     = _cache_dir()
    files = list(d.glob("*.json"))
    valid = 0
    now   = datetime.utcnow()
    for f in files:
        try:
            env = json.loads(f.read_text())
            if datetime.fromisoformat(env["expires_at"]) > now:
                valid += 1
        except Exception:
            pass
    return {
        "cache_dir"    : str(d),
        "total_entries": len(files),
        "valid_entries": valid,
        "expired"      : len(files) - valid,
    }
