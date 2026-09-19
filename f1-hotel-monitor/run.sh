#!/usr/bin/env bash
# cron / 手動実行用ラッパー。venv があれば使い、なければ python3 をそのまま使う。
set -euo pipefail
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi
exec "$PY" monitor.py "$@"
