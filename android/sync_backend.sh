#!/bin/sh
# Re-copy the desktop backend into android/backend/ after backend changes.
# Usage: sh android/sync_backend.sh
set -e
ROOT=$(dirname "$(dirname "$0")")
for f in config.py todoist.py sync.py sessions.py actions.py; do
  cp "$ROOT/$f" "$ROOT/android/backend/$f"
done
echo "backend synced"
