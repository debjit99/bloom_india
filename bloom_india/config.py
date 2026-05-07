"""
bloom_india/config.py
=====================
Loads config.yaml and derives all paths from base_dir.

Only base_dir needs to be set. Everything else is automatic:

    base_dir/
        raw/
            xbrl/
            bhavcopy/
            announce_dates.json
        db/
            prices.parquet
            fundamentals.parquet
            corp_actions.parquet

Usage:
    from bloom_india.config import CONFIG, get_path, ensure_dirs

    print(CONFIG.storage.fundamental_db)
    print(get_path("fundamental_db"))
"""

import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("[bloom_india] pip install pyyaml")
    sys.exit(1)


# ── Find config.yaml ──────────────────────────────────────────────────────────

def _find_config() -> Path:
    """
    Search order:
      1. BLOOM_INDIA_CONFIG env var
      2. cwd/config.yaml
      3. repo root (parent of this file's parent)
    """
    env = os.environ.get("BLOOM_INDIA_CONFIG")
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"BLOOM_INDIA_CONFIG points to missing file: {p}")

    candidates = [
        Path.cwd() / "config.yaml",
        Path(__file__).parent.parent / "config.yaml",
        Path(__file__).parent / "config.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p.resolve()

    raise FileNotFoundError(
        "config.yaml not found.\n"
        "Run:  cp config.yaml.example config.yaml\n"
        "Then edit base_dir and set:\n"
        "  export BLOOM_INDIA_CONFIG=/path/to/config.yaml"
    )


# ── Simple namespace ──────────────────────────────────────────────────────────

class _NS:
    def __init__(self, d: dict):
        for k, v in d.items():
            setattr(self, k, _NS(v) if isinstance(v, dict) else v)
    def __repr__(self):
        return str(vars(self))


# ── Build all paths from base_dir ─────────────────────────────────────────────

def _build_storage(raw: dict) -> dict:
    base    = Path(os.path.expandvars(
                raw.get("base_dir", "~/bloom_india_data")
              )).expanduser().resolve()

    raw_dir = base / "raw"
    db_dir  = base / "db"

    return {
        "base_dir":            base,
        "raw_dir":             raw_dir,
        "xbrl_dir":            raw_dir / "xbrl",
        "bhavcopy_dir":        raw_dir / "bhavcopy",
        "announce_dates_file": raw_dir / "announce_dates.json",
        "db_dir":              db_dir,
        "price_db":            db_dir / "prices.parquet",
        "fundamental_db":      db_dir / "fundamentals.parquet",
        "corp_actions_db":     db_dir / "corp_actions.parquet",
    }


# ── Load ──────────────────────────────────────────────────────────────────────

def _load() -> _NS:
    path = _find_config()
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    raw["storage"] = _build_storage(raw.get("storage", {}))
    return _NS(raw)


CONFIG = _load()


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_path(key: str) -> Path:
    """Get a resolved storage path by name."""
    return getattr(CONFIG.storage, key)


def ensure_dirs() -> None:
    """Create all storage directories."""
    for attr in ["base_dir", "raw_dir", "xbrl_dir", "bhavcopy_dir", "db_dir"]:
        Path(getattr(CONFIG.storage, attr)).mkdir(parents=True, exist_ok=True)


def print_config() -> None:
    print("\n── bloom_india config ──────────────────────────────")
    print(f"  config file : {_find_config()}")
    print(f"\n  Paths:")
    for k, v in vars(CONFIG.storage).items():
        print(f"    {k:<22} {v}")
    print("────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    print_config()