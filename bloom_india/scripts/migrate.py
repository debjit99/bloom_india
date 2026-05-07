"""
bloom_india/scripts/migrate.py
================================
Migrate existing data from your private cache into bloom_india's db layout.

If you already have:
  - fundamental_db.parquet  from your cryptobot project
  - panel_nifty50.parquet   from your cryptobot project
  - _xbrl_cache/            from your cryptobot project
  - announce_dates.json     from your cryptobot project

This script copies/links them into the bloom_india db layout
so you don't have to rebuild from scratch.

Run:
    python -m bloom_india.scripts.migrate \\
        --fundamental /path/to/fundamental_db.parquet \\
        --prices      /path/to/panel_nifty50.parquet \\
        --xbrl-cache  /path/to/_xbrl_cache \\
        --announce    /path/to/announce_dates.json
"""

import argparse
import shutil
import pandas as pd
from pathlib import Path

from bloom_india.config import CONFIG, ensure_dirs, print_config


def migrate(
    fundamental: str = None,
    prices:      str = None,
    xbrl_cache:  str = None,
    announce:    str = None,
    copy:        bool = False,   # copy vs symlink
) -> None:
    ensure_dirs()
    print_config()

    action = "Copying" if copy else "Symlinking"

    def link_or_copy(src: str, dst: Path):
        src_path = Path(src).expanduser().resolve()
        if not src_path.exists():
            print(f"  ✗ Source not found: {src_path}")
            return

        dst.parent.mkdir(parents=True, exist_ok=True)

        if dst.exists() or dst.is_symlink():
            dst.unlink() if dst.is_file() or dst.is_symlink() else shutil.rmtree(dst)

        if copy:
            if src_path.is_dir():
                shutil.copytree(src_path, dst)
            else:
                shutil.copy2(src_path, dst)
        else:
            dst.symlink_to(src_path)

        print(f"  ✓ {action}: {src_path.name} → {dst}")

    print(f"\n{action} data into bloom_india db layout...\n")

    if fundamental:
        link_or_copy(fundamental, Path(CONFIG.storage.fundamental_db))

    if prices:
        # Panel parquet may have different column names — normalise
        src = Path(prices).expanduser().resolve()
        if src.exists():
            df = pd.read_parquet(src)
            dst = Path(CONFIG.storage.price_db)
            dst.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(dst, index=False)
            print(f"  ✓ Copied + normalised prices → {dst}")
            print(f"    Shape: {df.shape}  Symbols: {df['Symbol'].nunique()}")

    if xbrl_cache:
        link_or_copy(xbrl_cache, Path(CONFIG.storage.xbrl_dir))

    if announce:
        link_or_copy(announce, Path(CONFIG.storage.announce_dates_file))

    print("\nDone. Run `from bloom_india.api import get_fundamentals` to verify.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate existing data into bloom_india db layout"
    )
    parser.add_argument("--fundamental", default="",
                        help="Path to existing fundamental_db.parquet")
    parser.add_argument("--prices",      default="",
                        help="Path to existing price panel parquet")
    parser.add_argument("--xbrl-cache",  default="",
                        help="Path to existing _xbrl_cache directory")
    parser.add_argument("--announce",    default="",
                        help="Path to existing announce_dates.json")
    parser.add_argument("--copy",        action="store_true",
                        help="Copy files instead of symlinking (uses more disk)")
    args = parser.parse_args()

    migrate(
        fundamental = args.fundamental or None,
        prices      = args.prices      or None,
        xbrl_cache  = args.xbrl_cache  or None,
        announce    = args.announce    or None,
        copy        = args.copy,
    )
