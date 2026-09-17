#!/bin/bash
# Docker（Container Manager）が使えない Synology 向け。
# DSM の「コントロールパネル > タスクスケジューラ」に登録して、SSH なしで動かす。
# 楽天トラベル API のみ（東横INN の Playwright はブラウザが必要なため対象外）。
#
# 事前準備（ブラウザのみ）:
#   1. パッケージセンターで「Python 3.9」以上（Synology 公式）をインストール
#   2. タスクスケジューラ > 作成 > 予約タスク > ユーザー指定のスクリプト
#      - ユーザー: root
#      - スケジュール: 毎日 09:00（同じものを 21:00 にももう 1 つ作る）
#      - 「タスク設定 > 実行コマンド」に、この下の【貼り付け用】を貼る
#   3. 「実行」で 1 回試し、「タスク設定 > 出力結果を保存」を有効にしてログを確認
#
# 【貼り付け用】（値を自分のものに置き換える）
#   export RAKUTEN_APP_ID=ここに楽天のApplication ID
#   export RAKUTEN_ACCESS_KEY=ここに楽天のAccess Key
#   export NTFY_TOPIC=ここにntfyトピック名
#   export NOTIFY_CHANNELS=ntfy
#   export F1HOTEL_BRANCH=claude/quirky-ride-2rg4ew
#   curl -fsSL https://raw.githubusercontent.com/hir0hir0/general/${F1HOTEL_BRANCH}/f1-hotel-monitor/deploy/synology-taskscheduler.sh | bash
#
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/volume1/f1-hotel-monitor}"
BRANCH="${F1HOTEL_BRANCH:-main}"
SOURCES="${F1HOTEL_SOURCES:-rakuten}"

log() { echo "[f1hotel] $*"; }

# --- Python を探す ------------------------------------------------------
PY=""
for c in /usr/local/bin/python3 /var/packages/Python3*/target/usr/bin/python3 /var/packages/py3k/target/usr/local/bin/python3 /usr/bin/python3; do
  for p in $c; do
    if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      PY="$p"; break 2
    fi
  done
done
if [ -z "$PY" ]; then
  echo "Python 3.9 以上が見つかりません。パッケージセンターで Synology 公式の Python 3 をインストールしてください。" >&2
  for c in /usr/local/bin/python3 /var/packages/Python3*/target/usr/bin/python3 /usr/bin/python3; do
    for p in $c; do [ -x "$p" ] && echo "  found: $p ($("$p" --version 2>&1))"; done
  done
  exit 1
fi
log "python: $PY ($("$PY" --version 2>&1))"

# --- コード取得（毎回最新に更新。data/ と .env は残す） --------------------
mkdir -p "$INSTALL_DIR"
TMP="$(mktemp -d)"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "https://github.com/hir0hir0/general/archive/refs/heads/${BRANCH}.tar.gz" -o "$TMP/src.tgz"
else
  wget -qO "$TMP/src.tgz" "https://github.com/hir0hir0/general/archive/refs/heads/${BRANCH}.tar.gz"
fi
tar -xzf "$TMP/src.tgz" -C "$TMP"
SRC="$(ls -d "$TMP"/general-*/f1-hotel-monitor)"
if [ -f "$INSTALL_DIR/config.toml" ]; then cp "$INSTALL_DIR/config.toml" "$TMP/config.keep"; fi
cp -r "$SRC"/. "$INSTALL_DIR"/
if [ -f "$TMP/config.keep" ]; then cp "$TMP/config.keep" "$INSTALL_DIR/config.toml"; cp "$SRC/config.toml" "$INSTALL_DIR/config.toml.new"; fi
rm -rf "$TMP"
cd "$INSTALL_DIR"

# --- 依存ライブラリ（venv、だめなら --user） ----------------------------
if [ ! -x .venv/bin/python ]; then
  if "$PY" -m venv .venv 2>/dev/null; then
    log "venv 作成"
  else
    log "venv 不可。--user でインストール"
  fi
fi
if [ -x .venv/bin/python ]; then RUNPY=".venv/bin/python"; PIPOPT=""; else RUNPY="$PY"; PIPOPT="--user"; fi
"$RUNPY" -m pip install -q $PIPOPT requests python-dotenv beautifulsoup4 'tomli; python_version < "3.11"' 2>&1 | grep -v "WARNING: Running pip as" || true

# --- .env（環境変数が渡されていれば毎回書き直す） ------------------------
if [ -n "${RAKUTEN_APP_ID:-}" ]; then
  {
    echo "RAKUTEN_APP_ID=${RAKUTEN_APP_ID}"
    echo "RAKUTEN_ACCESS_KEY=${RAKUTEN_ACCESS_KEY:-}"
    echo "RAKUTEN_REFERER=${RAKUTEN_REFERER:-https://github.com/hir0hir0/general}"
    echo "NOTIFY_CHANNELS=${NOTIFY_CHANNELS:-ntfy}"
    echo "NTFY_SERVER=${NTFY_SERVER:-https://ntfy.sh}"
    echo "NTFY_TOPIC=${NTFY_TOPIC:-}"
    echo "NTFY_ERROR_TOPIC=${NTFY_ERROR_TOPIC:-}"
    echo "LINE_CHANNEL_ACCESS_TOKEN=${LINE_CHANNEL_ACCESS_TOKEN:-}"
    echo "LINE_USER_ID=${LINE_USER_ID:-}"
    echo "GMAIL_USER=${GMAIL_USER:-}"
    echo "GMAIL_APP_PASSWORD=${GMAIL_APP_PASSWORD:-}"
    echo "GMAIL_TO=${GMAIL_TO:-}"
  } > .env
  chmod 600 .env
elif [ ! -f .env ]; then
  echo "RAKUTEN_APP_ID が未設定で .env もありません。タスクの実行コマンド先頭で export してください。" >&2
  exit 1
fi

# --- 実行 --------------------------------------------------------------
log "run --sources $SOURCES --notify"
exec "$RUNPY" monitor.py run --sources "$SOURCES" --notify
