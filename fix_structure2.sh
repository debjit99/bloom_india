#!/bin/bash
# Run from bloom_india/ repo root (where README.md is)
set -e

echo "=== Creating new directories ==="
mkdir -p bloom_india/mf/db
mkdir -p bloom_india/mf/analytics
mkdir -p bloom_india/mf/scripts

echo "=== Moving files ==="
mv nav_db.py          bloom_india/mf/db/nav_db.py
mv ingest.py          bloom_india/mf/db/ingest.py
mv pnl.py             bloom_india/mf/analytics/pnl.py
mv cron_update_navs.py bloom_india/mf/scripts/cron_update_navs.py

# These two replace existing files
mv server.py          bloom_india/mf_rest/server.py
mv app.py             bloom_india/mf_terminal/app.py

echo "=== Creating __init__.py files ==="
touch bloom_india/mf/db/__init__.py
touch bloom_india/mf/analytics/__init__.py
touch bloom_india/mf/scripts/__init__.py

echo "=== Cleaning up ==="
rm -rf mnt/ fix_structure.sh 2>/dev/null || true

echo ""
echo "=== Final structure ==="
find bloom_india/mf bloom_india/mf_rest bloom_india/mf_terminal -name "*.py" | sort

echo ""
echo "=== Done! Now run: ==="
echo "  python test_mf_pipeline.py"