#!/usr/bin/env python3
"""2027 F1日本GP 宿監視 CLI。

  python monitor.py run                 # 取得 → 表出力 → 差分保存（通知なし）
  python monitor.py run --notify        # 差分があれば通知（cron 用）
  python monitor.py run --only-if-dense --notify   # 開放直後期のみ実行（毎時 cron 用）
  python monitor.py areas --middle mie  # 楽天のエリアコード一覧（config の keyword 調整用）
  python monitor.py toyoko-dump         # 東横INN のページを保存（セレクタ調整用）
  python monitor.py test-notify         # 通知チャネルの疎通確認
  python monitor.py schedule            # 内蔵スケジューラ（Docker 用。cron 不要）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from f1hotel.config import Config, load_config
from f1hotel.models import Offer, SourceResult
from f1hotel.notify import (
    Notifier,
    build_diff_message,
    due_reminders,
    mark_error_notified,
    mark_reminder_sent,
    should_notify_error,
)
from f1hotel.rakuten import (
    RakutenClient,
    RakutenError,
    describe_tree,
    dump_tree,
    fetch_rakuten,
    load_area_tree,
    resolve_targets,
)
from f1hotel.report import offers_table, summary_line
from f1hotel.state import append_history, compute_diff, load_state, merge_for_save, save_state
from f1hotel.toyoko import fetch_toyoko

JST = ZoneInfo("Asia/Tokyo")
log = logging.getLogger("monitor")

ALL_SOURCES = ["rakuten", "toyoko"]


def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def collect(cfg: Config, sources: list[str]) -> list[SourceResult]:
    results: list[SourceResult] = []
    if "rakuten" in sources:
        try:
            client = RakutenClient(
                cfg.rakuten_app_id or "",
                cfg.rakuten_access_key or "",
                interval_sec=float(cfg.rakuten.get("request_interval_sec", 1.05)),
                timeout_sec=float(cfg.rakuten.get("timeout_sec", 20)),
            )
            tree = load_area_tree(client, cfg.data_dir / "rakuten_areas.json")
            try:
                (cfg.data_dir / "rakuten_areas.txt").write_text(dump_tree(tree), encoding="utf-8")
            except OSError as e:
                log.warning("エリア一覧の書き出しに失敗: %s", e)
            targets, warns = resolve_targets(tree, cfg.rakuten_areas)
            for w in warns:
                log.warning("rakuten area: %s", w)
            if not targets:
                msg = "検索対象エリアが 0 件。" + describe_tree(tree)
                if not tree:
                    cache = cfg.data_dir / "rakuten_areas.json"
                    try:
                        msg += " 応答の冒頭: " + cache.read_text(encoding="utf-8")[:300]
                    except OSError:
                        pass
                elif warns:
                    msg += " / " + " / ".join(warns[:3])
                results.append(SourceResult("rakuten", [], [msg]))
            else:
                log.info("rakuten: %d targets × %d stays", len(targets), len(cfg.stays))
                for t in targets:
                    log.info("  target tier%d %s/%s/%s %s", t.tier, t.middle, t.small, t.detail or "-", t.name)
                results.append(fetch_rakuten(cfg, client, targets))
        except (RakutenError, Exception) as e:  # noqa: BLE001
            log.exception("rakuten failed")
            results.append(SourceResult("rakuten", [], [f"rakuten: {e}"]))
    if "toyoko" in sources:
        try:
            results.append(fetch_toyoko(cfg))
        except Exception as e:  # noqa: BLE001
            log.exception("toyoko failed")
            results.append(SourceResult("toyoko", [], [f"toyoko: {e}"]))
    return results


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    today = now_jst().date()
    if args.only_if_dense and not cfg.is_dense_day(today):
        log.info("%s は密度アップ期間外のためスキップ", today)
        return 0

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in ALL_SOURCES]
    if unknown:
        print(f"unknown source(s): {unknown}  (choose from {ALL_SOURCES})", file=sys.stderr)
        return 2

    results = collect(cfg, sources)
    offers: list[Offer] = [o for r in results for o in r.offers]
    errors: list[str] = [e for r in results for e in r.errors]
    sources_ok = {r.source for r in results if r.ok}

    # --- 表出力 ---------------------------------------------------------
    print(f"# {now_jst():%Y-%m-%d %H:%M} JST  stays={[s.label for s in cfg.stays]}  sources={sources}")
    print(summary_line(offers))
    print(offers_table(offers, cfg))
    if errors:
        print("\n# errors")
        for e in errors:
            print(f"- {e}")
    if args.json:
        Path(args.json).write_text(json.dumps([o.to_dict() for o in offers], ensure_ascii=False, indent=1), encoding="utf-8")

    # --- 差分・保存 -----------------------------------------------------
    notifier = Notifier(dry_run=args.dry_run_notify)
    if not args.no_save:
        prev = load_state(cfg.data_dir)
        diff = compute_diff(prev, offers, sources_ok)
        print(f"\n# diff: new={len(diff.new)} price={len(diff.price_changed)} gone={len(diff.gone)} unchanged={diff.unchanged}")
        if prev and not diff.empty:
            title, body, click = build_diff_message(diff, cfg)
            print(body)
            if args.notify:
                fails = notifier.send(title, body, click=click, priority=4 if diff.new else 3)
                errors.extend(f"notify {f}" for f in fails)
        elif not prev:
            print("(初回実行: 基準となる状態を保存。通知はしない)")
        append_history(cfg.data_dir, diff)
        save_state(cfg.data_dir, merge_for_save(prev, offers, sources_ok), meta={"sources": sources, "errors": errors})

    # --- リマインド --------------------------------------------------------
    if args.notify:
        for r in due_reminders(cfg, cfg.data_dir, today):
            fails = notifier.send("📅 F1鈴鹿 宿: リマインド", r["message"], priority=4)
            if not fails:
                mark_reminder_sent(cfg.data_dir, r)

    # --- エラー通知（1 日 1 回） ------------------------------------------
    if errors and args.notify:
        if should_notify_error(cfg.data_dir, today):
            fails = notifier.send("⚠ F1鈴鹿 宿監視: エラー", "\n".join(errors)[:3500], error=True)
            if not fails and not args.dry_run_notify:
                mark_error_notified(cfg.data_dir, today)
        else:
            log.info("エラー通知は本日送信済みのため抑制")
    return 1 if errors and not offers else 0


# ---------------------------------------------------------------------------
# areas
# ---------------------------------------------------------------------------
def cmd_areas(args: argparse.Namespace, cfg: Config) -> int:
    client = RakutenClient(cfg.rakuten_app_id or "", cfg.rakuten_access_key or "") if cfg.rakuten_app_id else None
    tree = load_area_tree(client, cfg.data_dir / "rakuten_areas.json", force=args.refresh)
    print(describe_tree(tree, limit=60))
    middles = [m for m in tree if not args.middle or m.code == args.middle]
    if not middles:
        print(f"middleClassCode '{args.middle}' なし。候補: {', '.join(m.code for m in tree)}")
        return 1
    for m in middles:
        print(f"{m.code}  {m.name}")
        for s in m.smalls:
            print(f"  {s.code:<14} {s.name}")
            for code, name in s.details:
                print(f"      {code:<10} {name}")
    targets, warns = resolve_targets(tree, cfg.rakuten_areas)
    print("\n# config.toml の解決結果（tier / middle / small / detail / 名称）")
    for t in targets:
        print(f"  tier{t.tier}  {t.middle}/{t.small}/{t.detail or '-':<8}  {t.name}  ({t.label})")
    for w in warns:
        print(f"  ! {w}")
    return 0


# ---------------------------------------------------------------------------
# toyoko-dump
# ---------------------------------------------------------------------------
def cmd_toyoko_dump(args: argparse.Namespace, cfg: Config) -> int:
    res = fetch_toyoko(cfg, dump_all=True)
    print(summary_line(res.offers))
    print(offers_table(res.offers, cfg))
    for e in res.errors:
        print(f"- {e}")
    print(f"\nHTML / スクショ: {cfg.data_dir / 'debug'}")
    return 0 if not res.errors else 1


# ---------------------------------------------------------------------------
# test-notify
# ---------------------------------------------------------------------------
def cmd_test_notify(args: argparse.Namespace, cfg: Config) -> int:
    n = Notifier()
    if not n.channels:
        print("NOTIFY_CHANNELS が未設定です（.env）")
        return 1
    fails = n.send(
        "🏨 F1鈴鹿 宿監視: テスト",
        f"通知テスト {now_jst():%Y-%m-%d %H:%M} JST\nチャネル: {', '.join(n.channels)}",
        error=args.error,
    )
    if fails:
        print("失敗:", *fails, sep="\n  ")
        return 1
    print("送信 OK:", ", ".join(n.channels))
    return 0


# ---------------------------------------------------------------------------
# schedule（内蔵スケジューラ）
# ---------------------------------------------------------------------------
def next_run_time(cfg: Config, after: dt.datetime) -> dt.datetime:
    """after より後の直近の実行時刻（JST）。密度アップ期間は毎正時も対象。"""
    candidates: list[dt.datetime] = []
    for day_offset in range(0, 3):
        day = (after + dt.timedelta(days=day_offset)).date()
        for hhmm in cfg.daily_times:
            h, m = (int(x) for x in hhmm.split(":"))
            candidates.append(dt.datetime.combine(day, dt.time(h, m), tzinfo=JST))
        if cfg.is_dense_day(day):
            candidates.extend(dt.datetime.combine(day, dt.time(h, 0), tzinfo=JST) for h in range(24))
    future = sorted(c for c in candidates if c > after)
    return future[0]


def cmd_schedule(args: argparse.Namespace, cfg: Config) -> int:
    log.info("scheduler start: daily=%s dense=%s", cfg.daily_times, [(a.isoformat(), b.isoformat()) for a, b in cfg.dense_windows])
    run_args = argparse.Namespace(
        sources=args.sources, notify=True, no_save=False, only_if_dense=False, dry_run_notify=False, json=None
    )
    if args.run_now:
        cmd_run(run_args, cfg)
    while True:
        nxt = next_run_time(cfg, now_jst())
        wait = (nxt - now_jst()).total_seconds()
        log.info("next run at %s (in %.0f min)", nxt.strftime("%m/%d %H:%M"), wait / 60)
        time.sleep(max(1.0, wait))
        try:
            cfg = load_config(cfg.config_path)  # 設定変更を毎回反映
            cmd_run(run_args, cfg)
        except Exception:  # noqa: BLE001
            log.exception("scheduled run failed")


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None, help="config.toml のパス")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="取得・表出力・差分保存")
    r.add_argument("--sources", default=",".join(ALL_SOURCES), help="rakuten,toyoko")
    r.add_argument("--notify", action="store_true", help="差分があれば通知する")
    r.add_argument("--no-save", action="store_true", help="状態を保存せず表示のみ")
    r.add_argument("--only-if-dense", action="store_true", help="密度アップ期間のみ実行")
    r.add_argument("--dry-run-notify", action="store_true", help="通知を送らずログに出す")
    r.add_argument("--json", default=None, help="結果を JSON ファイルにも書く")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("areas", help="楽天エリアコード一覧")
    a.add_argument("--middle", default="mie", help="middleClassCode（mie / aichi / gifu …。空で全件）")
    a.add_argument("--refresh", action="store_true", help="キャッシュを無視して再取得")
    a.set_defaults(func=cmd_areas)

    d = sub.add_parser("toyoko-dump", help="東横INN ページを保存（セレクタ調整用）")
    d.set_defaults(func=cmd_toyoko_dump)

    t = sub.add_parser("test-notify", help="通知テスト")
    t.add_argument("--error", action="store_true", help="エラーチャネルに送る")
    t.set_defaults(func=cmd_test_notify)

    s = sub.add_parser("schedule", help="内蔵スケジューラ（常駐）")
    s.add_argument("--sources", default=",".join(ALL_SOURCES))
    s.add_argument("--run-now", action="store_true", help="起動直後にも 1 回実行")
    s.set_defaults(func=cmd_schedule)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    cfg = load_config(args.config)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
