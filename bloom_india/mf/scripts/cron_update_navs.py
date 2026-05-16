#!/usr/bin/env python3
"""
bloom_india/mf/scripts/cron_update_navs.py
==========================================
Daily cron job to update the NAV database with new dates.

Cron setup (macOS / Linux):
---------------------------
  # Edit crontab:
  crontab -e

  # Run every weekday at 11 PM IST (5:30 PM UTC):
  30 17 * * 1-5 /path/to/venv/bin/python /path/to/bloom_india/bloom_india/mf/scripts/cron_update_navs.py >> /tmp/bloom_india_nav_update.log 2>&1

  # Or use launchd on macOS — see the .plist file below.

launchd plist (macOS):
----------------------
  Save as ~/Library/LaunchAgents/com.bloom_india.nav_update.plist

  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.bloom_india.nav_update</string>
    <key>ProgramArguments</key>
    <array>
      <string>/path/to/venv/bin/python</string>
      <string>/path/to/bloom_india/bloom_india/mf/scripts/cron_update_navs.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key><integer>23</integer>
      <key>Minute</key><integer>0</integer>
      <key>Weekday</key><integer>1</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/bloom_india_nav_update.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/bloom_india_nav_update.err</string>
  </dict>
  </plist>

  Then: launchctl load ~/Library/LaunchAgents/com.bloom_india.nav_update.plist

Usage:
------
  python bloom_india/mf/scripts/cron_update_navs.py          # update all DB schemes
  python bloom_india/mf/scripts/cron_update_navs.py seed     # first-time seed all schemes
  python bloom_india/mf/scripts/cron_update_navs.py 100033 119551  # update specific codes
"""

import sys
import json
import logging
from datetime import datetime

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(levelname)s %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cron_update_navs")

log.info("=" * 60)
log.info(f"bloom_india NAV cron started at {datetime.now().isoformat()}")


def main():
    from bloom_india.mf.db.nav_db import db_stats, ensure_schema
    from bloom_india.mf.db.ingest import (
        update_all_schemes, seed_all_schemes, summary,
    )

    ensure_schema()
    before = db_stats()
    log.info(f"DB before: {before['nav_rows']:,} rows across {before['schemes']} schemes")

    args = sys.argv[1:]

    if args and args[0] == "seed":
        codes = [int(a) for a in args[1:]] if len(args) > 1 else None
        log.info(f"Mode: SEED {'all' if codes is None else codes}")
        results = seed_all_schemes(scheme_codes=codes, workers=4)
    else:
        # Positional ints = specific codes to update
        codes = [int(a) for a in args if a.isdigit()] if args else None
        log.info(f"Mode: UPDATE {'all DB schemes' if codes is None else codes}")
        results = update_all_schemes(scheme_codes=codes, workers=4)

    s = summary(results)
    log.info(f"Run summary: {json.dumps(s, indent=2)}")

    after = db_stats()
    log.info(
        f"DB after: {after['nav_rows']:,} rows "
        f"(+{after['nav_rows'] - before['nav_rows']:,} new) "
        f"| {after['size_mb']} MB"
    )

    if s["errors"]:
        log.warning(f"{len(s['errors'])} errors — see above for details")

    log.info("Cron finished.")
    return 0 if not s["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
