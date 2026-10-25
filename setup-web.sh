#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=""
for candidate in python3.12 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Python 3.12 or newer is required."
  exit 1
fi

exec "$PYTHON" -m k2y setup-web "$@"
