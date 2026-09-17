# 2027 F1日本GP 宿監視（鈴鹿 4/9〜11）

大人1＋4歳児1（添い寝・1室）で泊まれる宿の空室を定期取得し、**新規に空きが出た宿だけ**通知する。
対象日程は 4/8〜11（3泊）と 4/9〜11（2泊）の両方。

| ソース | 手段 | 状態 |
|---|---|---|
| 楽天トラベル | VacantHotelSearch API | 実装済み |
| 東横INN 津駅西口／近鉄四日市駅北口 | Playwright | 実装済み（初回に URL/セレクタ確認が必要。下記） |
| じゃらん／ルートイン／スーパーホテル | Playwright | 未実装（`f1hotel/` にソースを追加する構成） |
| 鈴鹿サーキットホテル／JTB／湯の山 | Cowork 側のスケジュールタスクで告知監視 | 本ツール対象外 |

## 1. セットアップ

```bash
cd f1-hotel-monitor
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # 東横INN 取得に必要（楽天のみなら不要）
cp .env.example .env                 # 秘密情報はここに
chmod +x run.sh                      # cron 用ラッパー
```

### 楽天トラベル API キー（Application ID と Access Key）の取得

2026/2 に楽天ウェブサービスの仕様が変わり、**Application ID と Access Key の両方**が必要になった（旧 `app.rakuten.co.jp` は 2026/5/14 停止）。

1. https://webservice.rakuten.co.jp/ を開き、楽天 ID でログイン → 「New App」
2. フォーム入力
   - Application name: 英数字のみ（例 `f1hotelmonitor`。ハイフン不可）
   - Application URL: `https://github.com/hir0hir0/general`
   - Allowed websites: 既定の 3 行に `github.com` を追加（ツールは Referer/Origin にこの URL を付けて呼ぶ）
   - Purpose: 個人用の空室監視である旨を英語で。Expected QPS: `1`
   - API Access Scopes: **Rakuten Travel API** のみ
3. 「Your Applications」に出る **Application ID**（UUID 形式）と **Access Key**（目のアイコンで表示、`pk_` で始まる）を控える
4. `.env` に `RAKUTEN_APP_ID=` と `RAKUTEN_ACCESS_KEY=` として設定（Docker の場合は環境変数）。Affiliate ID は不要
5. 無料。レート制限は 1 req/sec（本ツールは 1.05 秒間隔で守る）

### 通知チャネル（`.env` の `NOTIFY_CHANNELS`）

- **ntfy**（推奨・最短）: スマホに ntfy アプリを入れ、任意のトピック名を購読。`NTFY_TOPIC` に同じ名前を書く。認証不要
- **line**: LINE Developers で Messaging API チャネルを作り、長期チャネルアクセストークンと自分のユーザー ID を設定
- **gmail**: 2 段階認証を有効にしてアプリパスワードを発行

エラー通知は別チャネル（ntfy は `<topic>-errors`、LINE/Gmail は件名に ⚠）へ **1 日 1 回まで**。

```bash
python monitor.py test-notify          # 通常チャネルの疎通
python monitor.py test-notify --error  # エラーチャネルの疎通
```

## 2. 初回の確認手順

```bash
# (1) 楽天のエリアコードを確認し、config.toml の keywords が意図どおり解決されるか見る
python monitor.py areas --middle mie
python monitor.py areas --middle aichi
#   → 末尾の「解決結果」に tier ごとの検索対象が並ぶ。「!」が出た項目は keywords を調整

# (2) 楽天のみで取得して表を見る（状態は保存しない）
python monitor.py run --sources rakuten --no-save

# (3) 東横INN のページを実際に開いて HTML/スクショを保存し、URL とセレクタを合わせる
#     config.toml [toyoko] の url_template と hotels[].code を実サイトの URL から埋めてから:
python monitor.py toyoko-dump
#   → data/debug/ の HTML を見て plan_selectors / name_selectors / price_selectors を調整
#     セレクタが当たらなくても本文テキスト走査（部屋名→価格）でフォールバックする

# (4) 全ソースで実行。初回は基準状態を保存するだけで通知しない
python monitor.py run
# 2 回目以降、差分（新規空き・料金変動・消滅）があれば --notify で通知
python monitor.py run --notify
```

東横INN は小学生以下 1 名の添い寝が無料なので大人 1 名で検索する。
非会員でも 9 か月前から予約可能なため、津駅西口・近鉄四日市駅北口は最優先。

## 3. 定期実行（NAS）

### A. cron

`deploy/crontab.example` を参照。9:00 / 21:00 に `run --notify`、毎時 5 分に `run --notify --only-if-dense`。
`--only-if-dense` は `config.toml` の `dense_windows`（スーパーホテル開放 11/1、ルートイン特別販売の 1 月中旬）に該当する日だけ動く。

### B. Synology NAS・ブラウザだけで導入（SSH・PC 不要）

QuickConnect で DSM に入り、Container Manager の「プロジェクト」に compose を貼り付ける方式。
コードはコンテナ起動時に GitHub から取得するので、NAS にファイルを置く必要がない。

1. パッケージセンターで **Container Manager** をインストール
2. Container Manager > プロジェクト > **作成**
   - プロジェクト名: `f1-hotel-monitor`
   - パス: `/docker/f1-hotel-monitor`（新規作成）
   - ソース: 「docker-compose.yml を作成」を選び、[`deploy/docker-compose.synology.yml`](deploy/docker-compose.synology.yml) の内容を貼り付ける
3. 貼り付けた中の `RAKUTEN_APP_ID`・`RAKUTEN_ACCESS_KEY`・`NTFY_TOPIC` を自分の値にして「次へ」→「完了」
4. 起動後、コンテナ `f1-hotel-monitor` の **ログ** で「[boot] fetching branch」→ 表出力が出れば OK
5. 設定変更は File Station で `/docker/f1-hotel-monitor/data/config.toml` を編集（次回実行時に反映）
6. コード更新はコンテナを再起動するだけ（起動時に再取得）。`F1HOTEL_BRANCH` で取得ブランチを指定

コンテナの「ターミナル」タブから `python /app/monitor.py --config /data/config.toml areas --middle mie`
のように手動コマンドも実行できる。

### B-2. DSM 7.1 の「Docker」パッケージで導入（DS418play など、Container Manager が出ない DSM 7.1 機）

DSM 7.1 ではパッケージ名が **Docker**（7.2 から Container Manager）。プロジェクト（compose 貼り付け）機能がなく、
レジストリ検索は Docker Hub のみなので、Docker Hub 公式の `python` イメージを使い、起動時に Chromium を自動導入する。

1. パッケージセンターで「Docker」を検索してインストール
2. File Station で `docker` 共有フォルダ内に `f1-hotel-monitor/data` フォルダを作る
3. Docker > レジストリ で `python` を検索し、**Docker Official Image の python** を選んで「ダウンロード」。タグは **3.11-slim-bookworm**（Debian 12。Playwright の依存導入が確実）
4. Docker > イメージ でそのイメージを選び「起動」
   - コンテナ名: `f1-hotel-monitor`、「自動再起動を有効にする」に✓
   - **詳細設定 > 環境**: 次を追加
     - `TZ` = `Asia/Tokyo`
     - `F1HOTEL_BRANCH` = `main`（未マージの間は `claude/quirky-ride-2rg4ew`）
     - `RAKUTEN_APP_ID` = 楽天の Application ID
     - `RAKUTEN_ACCESS_KEY` = 楽天の Access Key
     - `NOTIFY_CHANNELS` = `ntfy`
     - `NTFY_TOPIC` = ntfy のトピック名
     - （楽天のみでよければ `F1HOTEL_SKIP_BROWSER` = `1` を追加すると起動が速い）
   - **詳細設定 > 実行コマンド**:
     `python -c "import os,urllib.request as u;exec(u.urlopen('https://raw.githubusercontent.com/hir0hir0/general/'+os.environ.get('F1HOTEL_BRANCH','main')+'/f1-hotel-monitor/deploy/boot.py').read())"`
   - **ボリューム**: 手順 2 のフォルダをマウントパス `/data` で追加
   - ポート・ネットワークは既定のまま
5. 起動後、コンテナの「詳細 > ログ」に `[boot] fetching branch` → `chromium ready` → 空室の表が出れば OK。
   初回は Chromium と依存ライブラリの導入で 3〜5 分かかる（`/data/pw-browsers` に保存され、次回以降は速い）
6. 設定変更は File Station で `docker/f1-hotel-monitor/data/config.toml` を編集。コード更新はコンテナ再起動

### B-3. DSM のタスクスケジューラから導入（SSH も PC も不要・一番確実）

Docker GUI のレジストリ検索が「レジストリをクエリできませんでした」で失敗する場合はこれが早い。
タスクスケジューラは root でシェルを実行できるので、ブラウザだけで `docker pull` と `docker run` を流せる。

1. パッケージセンターで **Docker** をインストールしておく
2. コントロールパネル > **タスクスケジューラ** > 作成 > **予約タスク** > **ユーザー指定のスクリプト**
   - 全般: タスク名 `f1hotel-setup`、ユーザー **root**
   - スケジュール: 何でもよい（後で手動実行するだけ。「毎日」のままで可）
   - タスク設定 > 「実行結果の詳細を通知する」または出力保存を有効にしておくとログが見える
   - 実行コマンド に次を貼り、値を自分のものに置き換える:

```bash
export RAKUTEN_APP_ID=楽天のApplicationID
export RAKUTEN_ACCESS_KEY=楽天のAccessKey
export NTFY_TOPIC=ntfyのトピック名
export F1HOTEL_BRANCH=claude/quirky-ride-2rg4ew
curl -fsSL https://raw.githubusercontent.com/hir0hir0/general/${F1HOTEL_BRANCH}/f1-hotel-monitor/deploy/synology-docker-run.sh | bash
```

3. 保存 → 一覧でタスクを選び **「実行」** を押す（初回はイメージ取得で数分）
4. Docker > コンテナ に `f1-hotel-monitor` が現れ、ログに `[boot] fetching branch` → 空室の表が出れば完了
5. 以後は Docker GUI から停止・再起動できる。セットアップ用タスクは無効化か削除してよい
6. 設定変更は File Station で `docker/f1-hotel-monitor/data/config.toml` を編集

### B-4. SSH から 1 行導入

DSM 7.1 の Docker GUI は古い Docker Hub API を使うため「レジストリをクエリできませんでした」が出ることがある。
その場合は SSH から `docker run` で直接作るのが確実（compose 不要）。

1. コントロールパネル > 端末と SNMP > **SSH サービスを有効にする**
2. PC かスマホの SSH アプリで NAS に管理者ユーザーでログイン
3. 次を 1 行で実行（値は自分のものに置き換え）

```bash
sudo RAKUTEN_APP_ID=xxx RAKUTEN_ACCESS_KEY=pk_xxx NTFY_TOPIC=yyy F1HOTEL_BRANCH=claude/quirky-ride-2rg4ew \
  bash -c 'curl -fsSL https://raw.githubusercontent.com/hir0hir0/general/claude/quirky-ride-2rg4ew/f1-hotel-monitor/deploy/synology-docker-run.sh | bash'
```

4. `docker logs -f f1-hotel-monitor` で確認。以後は DSM の Docker GUI からも停止・再起動できる
5. データは `/volume1/docker/f1-hotel-monitor/data`。終わったら SSH は無効に戻してよい

### B''. Synology NAS・Docker 非対応機種（タスクスケジューラ＋公式 Python）

パッケージセンターに Container Manager が出ない機種（J シリーズなど）向け。楽天のみ（東横INN は対象外）。

1. パッケージセンターで Synology 公式の **Python 3.9 以上** をインストール
2. コントロールパネル > タスクスケジューラ > 作成 > 予約タスク > **ユーザー指定のスクリプト**
   - ユーザー: root、スケジュール: 毎日 09:00（同じものを 21:00 にももう 1 つ）
   - 実行コマンドに次を貼り、値を自分のものにする:

```bash
export RAKUTEN_APP_ID=楽天のApplication ID
export RAKUTEN_ACCESS_KEY=楽天のAccess Key
export NTFY_TOPIC=ntfyのトピック名
export NOTIFY_CHANNELS=ntfy
export F1HOTEL_BRANCH=claude/quirky-ride-2rg4ew
curl -fsSL https://raw.githubusercontent.com/hir0hir0/general/${F1HOTEL_BRANCH}/f1-hotel-monitor/deploy/synology-taskscheduler.sh | bash
```

3. タスクを選んで「実行」し、「タスク設定 > 出力結果を保存」のログで表が出ていることを確認
4. 配置先は `/volume1/f1-hotel-monitor`。設定は File Station で `config.toml` を編集（`data/` と `config.toml` は更新時も保持）

### B'. Synology NAS・SSH で導入（Container Manager）

DSM 7 で「Container Manager」をインストールし、SSH を有効にして（コントロールパネル > 端末と SNMP）、
管理者ユーザーで SSH ログイン後に 1 行で導入できる:

```bash
curl -fsSL https://raw.githubusercontent.com/hir0hir0/general/main/f1-hotel-monitor/deploy/synology-setup.sh | sudo bash
```

- 配置先は `/volume1/docker/f1-hotel-monitor`（`INSTALL_DIR=... sudo -E bash` で変更可）
- 初回は `.env` を作って止まるので、`RAKUTEN_APP_ID`・`RAKUTEN_ACCESS_KEY`・`NTFY_TOPIC` を書いて同じコマンドを再実行する
- 2 回目以降は通知テスト → 楽天エリア一覧の表示 → 常駐開始まで自動で進む
- 更新時も同じコマンドでよい（`.env` / `config.toml` / `data/` は保持される）
- ログ: `docker compose -f /volume1/docker/f1-hotel-monitor/deploy/docker-compose.yml logs -f`
- Container Manager の GUI からも `f1-hotel-monitor` コンテナとして停止・再起動できる

### C. Docker（内蔵スケジューラ常駐）

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

`monitor.py schedule` が JST で `daily_times`（9:00/21:00）と `dense_windows` 内の毎正時に実行する。
`config.toml` は実行のたびに再読込されるので、期間や閾値の変更にコンテナ再起動は不要。

## 4. 通知判定

- **新規空き**: 前回なし → 今回あり。**料金変動**・**消滅**も本文に含める
- **即／参考**: 1 泊あたり `thresholds.instant_price_per_night`（25,000 円）以下は「即」、超えたら「参考」
- **子連れ加点**: 駅徒歩 5 分以内／大浴場／朝食 6:30 以前開始／ファミリー・ツイン・和室。加点の多い順・単価の安い順に並べる
- ソースの取得に失敗した回は、そのソースの前回分を「消滅」扱いにしない（一時的な失敗で通知が乱れない）
- 差分は `data/history/YYYY-MM-DD.jsonl` に追記される

## 5. ファイル構成

```
monitor.py            CLI（run / areas / toyoko-dump / test-notify / schedule）
config.toml           日程・エリア・閾値・東横INN 設定（秘密情報は置かない）
.env                  API キー・通知トークン（git 管理外）
f1hotel/
  config.py           設定読込
  models.py           Offer / Stay
  rakuten.py          楽天 API（エリア解決・空室検索・レート制限・リトライ）
  toyoko.py           東横INN Playwright（取得・解析・失敗時ダンプ）
  state.py            前回結果 JSON と差分
  scoring.py          即／参考判定と子連れ加点
  notify.py           ntfy / LINE / Gmail、エラー通知の 1 日 1 回制限、リマインド
  report.py           標準出力の表
deploy/               crontab.example / Dockerfile / docker-compose.yml
tests/                pytest（API・DOM はフィクスチャで再現）
data/                 state.json / history / rakuten_areas.json / debug（git 管理外）
```

## 6. テスト

```bash
python -m pytest -q
# 実ブラウザでの取得経路も含める場合
RUN_BROWSER_TESTS=1 python -m pytest -q
```

## 7. フェーズ2（予約自動化）に向けて

- `Offer.url` は楽天の `reserveUrl`（プラン直リンク）／東横INN の検索ページを保持
- `.env` に `TOYOKO_MEMBER_ID` 等のプレースホルダを用意済み。フォーム操作は `f1hotel/toyoko.py` の `fetch_pages` と同じ Playwright コンテキストを再利用する想定

## 補足

- 楽天のリクエスト数はエリア数 × 日程 × ページ数（既定 30 件/ページ）。tier1〜4 で 9〜12 エリア × 2 日程 ≒ 20〜25 req/回
- ブロックや DOM 変更で東横INN が解析できない場合、`data/debug/` に HTML とスクショを残し、エラー通知を送る
