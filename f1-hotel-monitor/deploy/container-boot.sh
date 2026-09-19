#!/bin/bash
# 互換用ラッパー。実体は deploy/boot.py（python だけで動く）。
set -e
BRANCH="${F1HOTEL_BRANCH:-main}"
exec python -c "import urllib.request as u;exec(u.urlopen('https://raw.githubusercontent.com/hir0hir0/general/${BRANCH}/f1-hotel-monitor/deploy/boot.py').read())"
