"""コンテナ起動ブートストラップ（python:3.x-slim / Playwright 公式イメージ どちらでも可）。

DSM の Docker GUI「実行コマンド」に次の 1 行を入れる（curl 不要、python だけで動く）:
  python -c "import urllib.request as u;exec(u.urlopen('https://raw.githubusercontent.com/hir0hir0/general/main/f1-hotel-monitor/deploy/boot.py').read())"

やること:
  1. GitHub から F1HOTEL_BRANCH のコードを取得して /app に展開
  2. pip で依存を導入
  3. Chromium が無ければ /data/pw-browsers に導入（apt の依存も入れる。失敗しても楽天のみで続行）
  4. /data/config.toml が無ければ作成
  5. monitor.py schedule --run-now を起動
環境変数: F1HOTEL_BRANCH, F1HOTEL_DATA_DIR(/data), F1HOTEL_SKIP_BROWSER=1 でブラウザ導入を省略
"""
import io
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

BRANCH = os.environ.get("F1HOTEL_BRANCH", "main")
DATA = os.environ.get("F1HOTEL_DATA_DIR", "/data")
APP = "/app"
BROWSERS = os.path.join(DATA, "pw-browsers")


def log(msg):
    print(f"[boot] {msg}", flush=True)


def run(cmd, check=True, **kw):
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=check, **kw)


# 1. コード取得
log(f"fetching branch {BRANCH}")
url = f"https://github.com/hir0hir0/general/archive/refs/heads/{BRANCH}.tar.gz"
data = urllib.request.urlopen(url, timeout=60).read()
shutil.rmtree("/src", ignore_errors=True)
tarfile.open(fileobj=io.BytesIO(data)).extractall("/src")
root = next(p for p in os.listdir("/src"))
shutil.rmtree(APP, ignore_errors=True)
shutil.copytree(f"/src/{root}/f1-hotel-monitor", APP)

# 2. 依存
run([sys.executable, "-m", "pip", "install", "-q", "--root-user-action=ignore", "-r", f"{APP}/requirements.txt"])

# 3. ブラウザ（東横INN 用）。/data に置いて再起動後も再利用
os.makedirs(DATA, exist_ok=True)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS
if os.environ.get("F1HOTEL_SKIP_BROWSER") == "1":
    log("F1HOTEL_SKIP_BROWSER=1: ブラウザ導入を省略（楽天のみ）")
else:
    have_chromium = os.path.isdir(BROWSERS) and any(n.startswith("chromium") for n in os.listdir(BROWSERS))
    try:
        if shutil.which("apt-get"):
            # Playwright 公式イメージでは既に入っているので速い。python:slim では数分かかる
            run([sys.executable, "-m", "playwright", "install-deps", "chromium"], check=False)
        if not have_chromium:
            run([sys.executable, "-m", "playwright", "install", "chromium"])
        log(f"chromium ready at {BROWSERS}")
    except Exception as e:  # noqa: BLE001
        log(f"ブラウザ導入に失敗（東横INN は取得できません。楽天のみ続行）: {e}")

# 4. 設定
#    ユーザーが編集していなければ最新版に自動更新する。編集済みなら残して .new を置く。
cfg = os.path.join(DATA, "config.toml")
shipped_repo = f"{APP}/config.toml"
shipped_marker = os.path.join(DATA, ".config.shipped.toml")  # 前回配布した内容
new_text = open(shipped_repo, encoding="utf-8").read()

if not os.path.exists(cfg):
    shutil.copy(shipped_repo, cfg)
    log(f"{cfg} を作成しました（File Station で編集可）")
    shutil.copy(shipped_repo, shipped_marker)
else:
    current = ""
    try:
        current = open(cfg, encoding="utf-8").read()
    except OSError:
        pass
    previous = ""
    if os.path.exists(shipped_marker):
        try:
            previous = open(shipped_marker, encoding="utf-8").read()
        except OSError:
            pass
    if current == new_text:
        shutil.copy(shipped_repo, shipped_marker)
    elif current == previous or not os.path.exists(shipped_marker) or "stay.parties" not in current:
        # 目印が無い＝以前の版から上げた直後。バックアップを取って最新版にする
        import datetime

        bak = cfg + "." + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + ".bak"
        shutil.copy(cfg, bak)
        shutil.copy(shipped_repo, cfg)
        shutil.copy(shipped_repo, shipped_marker)
        log(f"config.toml を最新版に更新しました（旧ファイル: {bak}）")
    else:
        shutil.copy(shipped_repo, cfg + ".new")
        log("config.toml は編集済みのため維持しました。新しい既定は config.toml.new を参照")

# 5. 起動
os.chdir(APP)
os.execv(sys.executable, [sys.executable, f"{APP}/monitor.py", "--config", cfg, "schedule", "--run-now"])
