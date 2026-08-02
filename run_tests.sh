#!/bin/sh
# Run all LIAS tests before committing changes.
# Usage: sh run_tests.sh   (or ./run_tests.sh)
set -e
cd "$(dirname "$0")"
echo "▶ Running all tests…"
python3 -m unittest discover -s tests -v
echo ""
echo "✅ All tests passed."
