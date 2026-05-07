"""
bloom_india/config.py
=====================
Loads config.yaml and exposes a CONFIG object with resolved paths.

Usage:
    from bloom_india.config import CONFIG, get_path

    print(CONFIG.storage.price_db)      # resolved absolute path
    print(get_path("price_db"))         # same, shorthand
"""

import os
import sys
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("[bloom_india] Install pyyaml:  pip install pyyaml")
    sys.exit(1)


# ── Locate config.yaml ────────────────────────────────────────────────────────
# Search order:
#   1. BLOOM_INDIA_CONFIG env var (explicit override)
#   2. Current working directory / config.yaml
#   3. This file's parent directory / config.yaml (repo root)

def _find_config() -> Path:
    env_path = os.environ.get("BLOOM_INDIA_CONFIG")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.exists():
            return p
        raise FileNotFoundError(
            f"BLOOM_INDIA_CONFIG points to missing file: {p}"
        )

    for candidate in [
        Path.cwd() / "config.yaml",
        Path(__file__).parent.parent / "config.yaml",
        Path(__file__).parent / "config.yaml",
    ]:
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(
        "config.yaml not found. Either:\n"
        "  1. Run from the bloom_india repo root, or\n"
        "  2. Set BLOOM_INDIA_CONFIG=/path/to/config.yaml, or\n"
        "  3. Run:  python -m bloom_india.setup"
    )


# ── Path resolution helpers ───────────────────────────────────────────────────

def _resolve(path: Optional[str], default: Path) -> Path:
    """Expand ~ and env vars, fall back to default if None."""
    if path:
        return Path(os.path.expandvars(path)).expanduser().resolve()
    return default


def _resolve_paths(raw: dict) -> dict:
    """
    Resolve all storage paths relative to base_dir.
    Explicit paths in config override the defaults.
    """
    base = Path(os.path.expandvars(
        raw.get("base_dir", "~/bloom_india_data")
    )).expanduser().resolve()

    raw_dir  = _resolve(raw.get("raw_dir"),  base / "raw")
    db_dir   = _resolve(raw.get("db_dir"),   base / "db")

    return {
        "base_dir":            base,
        "raw_dir":             raw_dir,
        "xbrl_dir":            _resolve(raw.get("xbrl_dir"),            raw_dir / "xbrl"),
        "bhavcopy_dir":        _resolve(raw.get("bhavcopy_dir"),         raw_dir / "bhavcopy"),
        "announce_dates_file": _resolve(raw.get("announce_dates_file"),  raw_dir / "announce_dates.json"),
        "db_dir":              db_dir,
        "price_db":            _resolve(raw.get("price_db"),             db_dir / "prices.parquet"),
        "fundamental_db":      _resolve(raw.get("fundamental_db"),       db_dir / "fundamentals.parquet"),
        "corp_actions_db":     _resolve(raw.get("corp_actions_db"),      db_dir / "corp_actions.parquet"),
    }


# ── Simple namespace ──────────────────────────────────────────────────────────

class _Namespace:
    """Dot-access wrapper for nested dicts."""
    def __init__(self, d: dict):
        for k, v in d.items():
            setattr(self, k, _Namespace(v) if isinstance(v, dict) else v)

    def __repr__(self):
        return str(vars(self))


# ── Load ──────────────────────────────────────────────────────────────────────

def _load_config() -> _Namespace:
    config_path = _find_config()

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    # Resolve storage paths
    raw["storage"] = _resolve_paths(raw.get("storage", {}))

    return _Namespace(raw)


CONFIG: _Namespace = _load_config()


# ── Public helpers ────────────────────────────────────────────────────────────

def get_path(key: str) -> Path:
    """
    Shorthand to get a resolved storage path.

    Args:
        key: one of price_db, fundamental_db, corp_actions_db,
             raw_dir, xbrl_dir, bhavcopy_dir, announce_dates_file,
             base_dir, db_dir

    Returns:
        Resolved absolute Path object.

    Example:
        from bloom_india.config import get_path
        print(get_path("fundamental_db"))
    """
    return getattr(CONFIG.storage, key)


def ensure_dirs() -> None:
    """Create all storage directories if they don't exist."""
    dirs = [
        CONFIG.storage.base_dir,
        CONFIG.storage.raw_dir,
        CONFIG.storage.xbrl_dir,
        CONFIG.storage.bhavcopy_dir,
        CONFIG.storage.db_dir,
    ]
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)


def print_config() -> None:
    """Print resolved config for debugging."""
    print("\n── bloom_india config ──────────────────────────────")
    print(f"  config file  : {_find_config()}")
    print(f"\n  Storage paths:")
    for k, v in vars(CONFIG.storage).items():
        print(f"    {k:<22} {v}")
    print(f"\n  Data floors:")
    print(f"    price       : {CONFIG.data.price_floor}")
    print(f"    fundamental : {CONFIG.data.fundamental_floor}")
    print(f"\n  Backtest defaults:")
    print(f"    hold_days   : {CONFIG.backtest.default_hold_days}")
    print(f"    n_long      : {CONFIG.backtest.default_n_long}")
    print(f"    n_short     : {CONFIG.backtest.default_n_short}")
    print("────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    print_config()
