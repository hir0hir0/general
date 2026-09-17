#!/bin/bash
# Synology NAS (DSM 7, Container Manager 導入済み) 向けセットアップ。
# NAS に SSH でログインして実行する:
#   curl -fsSL https://raw.githubusercontent.com/hir0hir0/general/main/f1-hotel-monitor/deploy/synology-setup.sh | sudo bash
# もしくはリポジトリを取得済みなら:
#   sudo bash f1-hotel-monitor/deploy/synology-setup.sh
#
# 環境変数で調整可:
#   INSTALL_DIR  配置先（既定 /volume1/docker/f1-hotel-monitor）
#   BRANCH       取得するブランチ（既定 main）
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/volume1/docker/f1-hotel-monitor}"
BRANCH="${BRANCH:-main}"
REPO="https://github.com/hir0hir0/general.git"
SUBDIR="f1-hotel-monitor"
WORK="$(mktemp -d)"

log() { echo "[setup] $*"; }

# --- 前提チェック --------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "docker が見つかりません。DSM のパッケージセンターで Container Manager をインストールしてください。" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose が使えません。Container Manager を最新にしてください。" >&2
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  log "git が無いので tarball で取得します"
  USE_TAR=1
else
  USE_TAR=0
fi

# --- 取得 ----------------------------------------------------------------
mkdir -p "$(dirname "$INSTALL_DIR")"
if [ "$USE_TAR" = 1 ]; then
  curl -fsSL "https://github.com/hir0hir0/general/archive/refs/heads/${BRANCH}.tar.gz" | tar -xz -C "$WORK"
  SRC="$WORK/general-${BRANCH}/$SUBDIR"
else
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$WORK/repo" >/dev/null
  SRC="$WORK/repo/$SUBDIR"
fi

# 既存の .env / config.toml / data は残して上書き
mkdir -p "$INSTALL_DIR"
if [ -f "$INSTALL_DIR/config.toml" ]; then
  cp "$INSTALL_DIR/config.toml" "$WORK/config.toml.keep"
fi
cp -r "$SRC"/. "$INSTALL_DIR"/
if [ -f "$WORK/config.toml.keep" ]; then
  cp "$WORK/config.toml.keep" "$INSTALL_DIR/config.toml"
  log "既存の config.toml を維持しました（新しい既定は config.toml.new を参照）"
  cp "$SRC/config.toml" "$INSTALL_DIR/config.toml.new"
fi
chmod +x "$INSTALL_DIR/run.sh"
mkdir -p "$INSTALL_DIR/data"
rm -rf "$WORK"

# --- .env ------------------------------------------------------------------
if [ ! -f "$INSTALL_DIR/.env" ]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
  log ".env を作成しました。RAKUTEN_APP_ID と NTFY_TOPIC を設定してください:"
  log "  vi $INSTALL_DIR/.env"
  NEED_ENV=1
else
  NEED_ENV=0
fi

# --- Docker --------------------------------------------------------------
cd "$INSTALL_DIR"
log "イメージをビルドします（初回は数分）"
docker compose -f deploy/docker-compose.yml build

if [ "$NEED_ENV" = 1 ] || ! grep -Eq '^RAKUTEN_APP_ID=.+' .env; then
  log ".env が未設定のため常駐は開始しません。設定後に次を実行:"
  log "  cd $INSTALL_DIR && docker compose -f deploy/docker-compose.yml up -d"
  exit 0
fi

log "動作確認: 通知テストと楽天エリア一覧"
docker compose -f deploy/docker-compose.yml run --rm monitor python monitor.py test-notify || true
docker compose -f deploy/docker-compose.yml run --rm monitor python monitor.py areas --middle mie | tail -25 || true

log "常駐を開始します（9:00/21:00 JST ＋ 開放直後期は毎時）"
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps
log "ログ: docker compose -f $INSTALL_DIR/deploy/docker-compose.yml logs -f"
