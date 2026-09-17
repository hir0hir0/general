#!/bin/bash
# DSM 7.1 の Docker パッケージ向け。compose を使わず docker run だけでコンテナを作る。
# SSH でログインして実行する（レジストリ GUI の「タグ取得失敗」を回避できる）。
#
# DSM の「タスクスケジューラ > ユーザー指定のスクリプト」(ユーザー: root) に貼ってもよい。
#
#   sudo RAKUTEN_APP_ID=xxx RAKUTEN_ACCESS_KEY=pk_xxx NTFY_TOPIC=yyy \
#     F1HOTEL_BRANCH=claude/quirky-ride-2rg4ew \
#     bash -c 'curl -fsSL https://raw.githubusercontent.com/hir0hir0/general/claude/quirky-ride-2rg4ew/f1-hotel-monitor/deploy/synology-docker-run.sh | bash'
#
# 環境変数（任意）: IMAGE, DATA_DIR, NAME, F1HOTEL_SKIP_BROWSER, NOTIFY_CHANNELS, NTFY_SERVER
set -euo pipefail

# DSM のタスクスケジューラは PATH が狭いので docker / curl の場所を足す
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

IMAGE="${IMAGE:-python:3.11-slim-bookworm}"
NAME="${NAME:-f1-hotel-monitor}"
DATA_DIR="${DATA_DIR:-/volume1/docker/f1-hotel-monitor/data}"
BRANCH="${F1HOTEL_BRANCH:-main}"

log() { echo "[setup] $*"; }

if ! command -v docker >/dev/null 2>&1; then
  for p in /usr/local/bin/docker /var/packages/Docker/target/usr/bin/docker /var/packages/ContainerManager/target/usr/bin/docker; do
    [ -x "$p" ] && { export PATH="$(dirname "$p"):$PATH"; break; }
  done
fi
command -v docker >/dev/null 2>&1 || { echo "docker が見つかりません（パッケージセンターで Docker をインストール）" >&2; exit 1; }
[ -n "${RAKUTEN_APP_ID:-}" ] || { echo "RAKUTEN_APP_ID を指定してください" >&2; exit 1; }
[ -n "${RAKUTEN_ACCESS_KEY:-}" ] || { echo "RAKUTEN_ACCESS_KEY を指定してください" >&2; exit 1; }

mkdir -p "$DATA_DIR"
log "pulling $IMAGE"
docker pull "$IMAGE"

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  log "既存コンテナ $NAME を削除（/data のデータは残ります）"
  docker rm -f "$NAME" >/dev/null
fi

BOOT_URL="https://raw.githubusercontent.com/hir0hir0/general/${BRANCH}/f1-hotel-monitor/deploy/boot.py"
log "creating container $NAME"
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  -e TZ=Asia/Tokyo \
  -e PYTHONUNBUFFERED=1 \
  -e F1HOTEL_DATA_DIR=/data \
  -e F1HOTEL_BRANCH="$BRANCH" \
  -e F1HOTEL_SKIP_BROWSER="${F1HOTEL_SKIP_BROWSER:-}" \
  -e RAKUTEN_APP_ID="$RAKUTEN_APP_ID" \
  -e RAKUTEN_ACCESS_KEY="$RAKUTEN_ACCESS_KEY" \
  -e RAKUTEN_REFERER="${RAKUTEN_REFERER:-https://github.com/hir0hir0/general}" \
  -e NOTIFY_CHANNELS="${NOTIFY_CHANNELS:-ntfy}" \
  -e NTFY_SERVER="${NTFY_SERVER:-https://ntfy.sh}" \
  -e NTFY_TOPIC="${NTFY_TOPIC:-}" \
  -e LINE_CHANNEL_ACCESS_TOKEN="${LINE_CHANNEL_ACCESS_TOKEN:-}" \
  -e LINE_USER_ID="${LINE_USER_ID:-}" \
  -e GMAIL_USER="${GMAIL_USER:-}" \
  -e GMAIL_APP_PASSWORD="${GMAIL_APP_PASSWORD:-}" \
  -e GMAIL_TO="${GMAIL_TO:-}" \
  -v "$DATA_DIR":/data \
  "$IMAGE" \
  python -c "import urllib.request as u;exec(u.urlopen('${BOOT_URL}').read())"

log "起動しました。ログ: docker logs -f $NAME"
log "初回は依存導入と Chromium 取得で数分かかります"
sleep 5
docker logs --tail 20 "$NAME" || true
