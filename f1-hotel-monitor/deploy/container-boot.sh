#!/bin/bash
# コンテナ起動時のブートストラップ。Playwright 公式イメージ内で実行する想定。
# GitHub からコードを取得 → 依存導入 → /data/config.toml を用意 → 内蔵スケジューラ起動。
# DSM 7.1 の Docker GUI では「実行コマンド」に次の 1 行を入れる:
#   bash -c "curl -fsSL https://raw.githubusercontent.com/hir0hir0/general/main/f1-hotel-monitor/deploy/container-boot.sh | bash"
set -e
BRANCH="${F1HOTEL_BRANCH:-main}"
DATA="${F1HOTEL_DATA_DIR:-/data}"
echo "[boot] fetching branch $BRANCH"
python - <<'PY'
import io, os, shutil, tarfile, urllib.request
br = os.environ.get("F1HOTEL_BRANCH", "main")
url = f"https://github.com/hir0hir0/general/archive/refs/heads/{br}.tar.gz"
data = urllib.request.urlopen(url, timeout=60).read()
shutil.rmtree("/src", ignore_errors=True)
tarfile.open(fileobj=io.BytesIO(data)).extractall("/src")
root = next(p for p in os.listdir("/src"))
shutil.rmtree("/app", ignore_errors=True)
shutil.copytree(f"/src/{root}/f1-hotel-monitor", "/app")
PY
pip install -q -r /app/requirements.txt 2>&1 | grep -v "WARNING: Running pip as" || true
mkdir -p "$DATA"
if [ ! -f "$DATA/config.toml" ]; then
  cp /app/config.toml "$DATA/config.toml"
  echo "[boot] $DATA/config.toml を作成しました（File Station で編集可）"
fi
cd /app
exec python /app/monitor.py --config "$DATA/config.toml" schedule --run-now
