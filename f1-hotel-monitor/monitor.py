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
import dataclasses
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from f1hotel.config import Config, load_config
from f1hotel.models import Offer, SourceResult
from f1hotel.notify import (
    Notifier,
    clip_bytes,
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
    watch_hotel_targets,
)
from f1hotel.report import links_list, offers_table, summary_line
from f1hotel.state import append_history, compute_diff, load_state, merge_for_save, save_state
from f1hotel.toyoko import discover_codes, fetch_toyoko
from f1hotel.websource import fetch_web_source

JST = ZoneInfo("Asia/Tokyo")
log = logging.getLogger("monitor")

ALL_SOURCES = ["rakuten", "toyoko", "superhotel", "jalan"]
# config の [<名前>] セクションだけで足せる、ブラウザ経由のソース
WEB_SOURCES = ["superhotel", "jalan"]


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
            # 名指しの宿はエリア検索に出てこない（満室だと返らない）ので別枠で足す
            watched = watch_hotel_targets(cfg)
            for t in watched:
                log.info("  watch  tier%d %s (hotelNo=%s)", t.tier, t.name, t.hotel_no)
            targets = targets + watched
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
    for name in WEB_SOURCES:
        if name not in sources:
            continue
        try:
            results.append(fetch_web_source(cfg, name))
        except Exception as e:  # noqa: BLE001
            log.exception("%s failed", name)
            results.append(SourceResult(name, [], [f"{name}: {e}"]))
    if "toyoko" in sources:
        try:
            if any(h.needs_code for h in cfg.toyoko_hotels):
                out = cfg.data_dir / "toyoko_candidates.txt"
                if not out.exists():
                    log.info("東横INN の施設コード未設定 → 候補を %s に書き出します", out)
                    try:
                        text = discover_codes(cfg)
                        if text:
                            out.write_text(text, encoding="utf-8")
                    except Exception as e:  # noqa: BLE001
                        log.warning("施設コードの探索に失敗: %s", e)
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

    # 張り込み中は対象日程・人数を絞る（サイト負荷と 1 巡の所要時間を下げる）
    def _as_list(v: Any) -> list[str]:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return list(v or [])

    checkins = _as_list(getattr(args, "checkins", None))
    parties = _as_list(getattr(args, "parties", None))
    if checkins:
        cfg = dataclasses.replace(cfg, stays=[s for s in cfg.stays if s.checkin.isoformat() in checkins] or cfg.stays)
    if parties:
        cfg = dataclasses.replace(cfg, parties=[p for p in cfg.parties if p.label in parties] or cfg.parties)

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
    # NAS では run を手で叩けないので、既定でログにリンクを載せる（[report] links で切れる）
    rep = cfg.raw.get("report", {})
    if getattr(args, "links", False) or rep.get("links", True):
        limit = int(rep.get("links_limit", 20))
        print("\n# リンク（URL は楽天 API が返した値そのまま）")
        print(links_list(offers, cfg, limit=limit))
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
            if body:
                print(body)
                if args.notify:
                    fails = notifier.send(title, body, click=click, priority=4 if diff.new else 3)
                    errors.extend(f"notify {f}" for f in fails)
            else:
                # 差分はあるが [notify.filter] で全部落ちた。送ると ntfy が
                # 本文なしの「Triggered」を表示してしまうので送らない
                print("(差分はあるが通知条件に合うものが無いので送信しない)")
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
            fails = notifier.send("⚠ F1鈴鹿 宿監視: エラー", clip_bytes("\n".join(errors), 3000), error=True)
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
def cmd_web_dump(args: argparse.Namespace, cfg: Config) -> int:
    source = args.web_source
    if getattr(args, "url", None):
        # 設定前のサイトでも HTML を取れるようにする（セレクタを決めるため）
        from f1hotel.toyoko import fetch_pages

        page = fetch_pages([args.url], cfg.raw.get(source, {}), with_screenshot=True)[0]
        if isinstance(page, Exception):
            print(f"取得に失敗: {page}", file=sys.stderr)
            return 1
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        out = cfg.data_dir / f"{source}_sample.html"
        out.write_text(page.html, encoding="utf-8")
        (cfg.data_dir / f"{source}_sample_url.txt").write_text(args.url + "\n", encoding="utf-8")
        print(f"保存: {out} ({len(page.html)} bytes)")
        return 0
    res = fetch_web_source(cfg, source, dump_all=True)
    print(summary_line(res.offers))
    print(offers_table(res.offers, cfg))
    for e in res.errors:
        print(f"- {e}")
    print(f"\nHTML / スクショ: {cfg.data_dir / 'debug'}")
    return 0 if not res.errors else 1


def cmd_toyoko_discover(args: argparse.Namespace, cfg: Config) -> int:
    text = discover_codes(cfg)
    if not text:
        print("コード未設定のホテルがない、または discover.url_templates が未設定です")
        return 1
    out = cfg.data_dir / "toyoko_candidates.txt"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n保存: {out}  （HTML/スクショ: {cfg.data_dir / 'debug'}）")
    return 0


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
def next_run_time(cfg: Config, after: dt.datetime) -> tuple[dt.datetime, str, list[str], list[str]]:
    """after より後の直近の実行時刻と、その回で見るソース。

    - daily_times: 全ソース
    - dense_windows 内の毎正時: 全ソース
    - schedule.hourly_sources: 毎時 :20 にそのソースだけ（東横INN のキャンセル拾い用）
    """
    all_sources = ",".join(ALL_SOURCES)
    sched = cfg.raw.get("schedule", {})
    hourly = str(sched.get("hourly_sources", "")).strip()
    # (時刻, ソース, 絞り込むチェックイン日, 絞り込む人数パターン)
    candidates: list[tuple[dt.datetime, str, list[str], list[str]]] = []

    # 開放日の張り込み: 期間中は interval_seconds ごとにそのソースだけ見る
    for w in sched.get("watch_windows", []):
        try:
            start = dt.datetime.fromisoformat(str(w["start"])).replace(tzinfo=JST)
            end = dt.datetime.fromisoformat(str(w["end"])).replace(tzinfo=JST)
        except (KeyError, ValueError):
            continue
        if after >= end:
            continue
        step = dt.timedelta(seconds=max(10, int(w.get("interval_seconds", 60))))
        nxt = start if after < start else start + step * (int((after - start) / step) + 1)
        if nxt < end:
            candidates.append((
                nxt,
                str(w.get("sources", all_sources)),
                [str(x) for x in w.get("checkins", [])],
                [str(x) for x in w.get("parties", [])],
            ))
    for day_offset in range(0, 3):
        day = (after + dt.timedelta(days=day_offset)).date()
        for hhmm in cfg.daily_times:
            h, m = (int(x) for x in hhmm.split(":"))
            candidates.append((dt.datetime.combine(day, dt.time(h, m), tzinfo=JST), all_sources, [], []))
        if cfg.is_dense_day(day):
            candidates.extend(
                (dt.datetime.combine(day, dt.time(h, 0), tzinfo=JST), all_sources, [], []) for h in range(24)
            )
        if hourly:
            candidates.extend(
                (dt.datetime.combine(day, dt.time(h, 20), tzinfo=JST), hourly, [], []) for h in range(24)
            )
    future = [c for c in candidates if c[0] > after]
    if not future:
        raise RuntimeError("next_run_time: 候補がありません")
    when = min(c[0] for c in future)
    return (when, *_merge_candidates([c for c in future if c[0] == when]))


def _merge_candidates(
    same_time: list[tuple[dt.datetime, str, list[str], list[str]]],
) -> tuple[str, list[str], list[str]]:
    """同じ時刻に複数の予定が重なったら 1 回にまとめる。

    張り込み窓どうしが重なると片方が落ちてしまうため、ソースは和集合にする。
    絞り込み（チェックイン日・人数）は空リストが「全部」の意味なので、
    1 つでも空があれば絞り込まない。
    """
    sources: list[str] = []
    for _t, src, _ci, _pt in same_time:
        for s in (x.strip() for x in src.split(",")):
            if s and s not in sources:
                sources.append(s)
    sources.sort(key=lambda s: ALL_SOURCES.index(s) if s in ALL_SOURCES else len(ALL_SOURCES))

    def union(idx: int) -> list[str]:
        merged: list[str] = []
        for cand in same_time:
            if not cand[idx]:
                return []
            for v in cand[idx]:
                if v not in merged:
                    merged.append(v)
        return merged

    return ",".join(sources), union(2), union(3)


def cmd_schedule(args: argparse.Namespace, cfg: Config) -> int:
    log.info("scheduler start: daily=%s dense=%s", cfg.daily_times, [(a.isoformat(), b.isoformat()) for a, b in cfg.dense_windows])
    run_args = argparse.Namespace(
        sources=args.sources, notify=True, no_save=False, only_if_dense=False,
        dry_run_notify=False, json=None, checkins=[], parties=[], links=False,
    )
    if args.run_now:
        cmd_run(run_args, cfg)
    while True:
        nxt, sources, checkins, parties = next_run_time(cfg, now_jst())
        wait = (nxt - now_jst()).total_seconds()
        narrow = f" ci={checkins} party={parties}" if checkins or parties else ""
        log.info("next run at %s [%s]%s (in %.1f min)", nxt.strftime("%m/%d %H:%M:%S"), sources, narrow, wait / 60)
        time.sleep(max(1.0, wait))
        try:
            cfg = load_config(cfg.config_path)  # 設定変更を毎回反映
            run_args.sources = sources
            run_args.checkins = checkins
            run_args.parties = parties
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
    r.add_argument("--links", action="store_true", help="宿ごとの予約 URL も出す")
    r.add_argument("--notify", action="store_true", help="差分があれば通知する")
    r.add_argument("--no-save", action="store_true", help="状態を保存せず表示のみ")
    r.add_argument("--only-if-dense", action="store_true", help="密度アップ期間のみ実行")
    r.add_argument("--dry-run-notify", action="store_true", help="通知を送らずログに出す")
    r.add_argument("--json", default=None, help="結果を JSON ファイルにも書く")
    r.add_argument("--checkins", default="", help="この日付のチェックインだけ見る（カンマ区切り）")
    r.add_argument("--parties", default="", help="この人数パターンだけ見る（カンマ区切り）")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("areas", help="楽天エリアコード一覧")
    a.add_argument("--middle", default="mie", help="middleClassCode（mie / aichi / gifu …。空で全件）")
    a.add_argument("--refresh", action="store_true", help="キャッシュを無視して再取得")
    a.set_defaults(func=cmd_areas)

    sh = sub.add_parser("superhotel-dump", help="スーパーホテルのページを保存（URL/セレクタ調整用）")
    sh.add_argument("--url", help="設定を使わずこの URL を保存する")
    sh.set_defaults(func=cmd_web_dump, web_source="superhotel")

    jd = sub.add_parser("jalan-dump", help="じゃらんのページを保存（URL/セレクタ調整用）")
    jd.add_argument("--url", help="設定を使わずこの URL を保存する")
    jd.set_defaults(func=cmd_web_dump, web_source="jalan")

    d = sub.add_parser("toyoko-dump", help="東横INN ページを保存（セレクタ調整用）")
    d.set_defaults(func=cmd_toyoko_dump)

    dc = sub.add_parser("toyoko-discover", help="東横INN の施設コード候補を探す")
    dc.set_defaults(func=cmd_toyoko_discover)

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
