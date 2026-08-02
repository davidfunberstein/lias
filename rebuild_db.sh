#!/bin/sh
# Wrapper — run rebuild_db.py with python3
exec python3 "$(dirname "$0")/rebuild_db.py" "$@"
