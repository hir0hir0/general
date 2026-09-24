"""通知（ntfy.sh / LINE Messaging API / Gmail SMTP）。

チャネルは .env の NOTIFY_CHANNELS（例: "ntfy,line"）で選ぶ。
エラー通知は別チャネル（ntfy は別トピック、LINE/Gmail は件名に ⚠）で 1 日 1 回まで。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests

from .config import Config
from .models import Offer
from .scoring import Score, score_offer, sort_key
from .state import Diff

log = logging.getLogger(__name__)

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_MAX_TEXT = 4800
NTFY_MAX_BODY_BYTES = 3300  # ntfy は 4096 バイト超で 413。title と JSON の分を残す
NTFY_MAX_TITLE_BYTES = 200
MAX_NEW_LISTED = 20  # 本文に明細を出す件数（残りは「…他 N 件」）
MAX_PRICE_LISTED = 8
MAX_GONE_LISTED = 3


def clip_bytes(text: str, limit: int) -> str:
    """UTF-8 バイト数で切り詰める（文字数だと日本語で 3 倍になり 413 になる）。"""
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    return b[:limit].decode("utf-8", "ignore").rstrip() + "\n…（省略）"


class NotifyError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 本文生成
# ---------------------------------------------------------------------------
def fmt_yen(v: int | None) -> str:
    return f"{v:,}円" if v is not None else "料金不明"


def format_offer(o: Offer, s: Score) -> str:
    stay = f"{o.checkin[5:].replace('-', '/')}-{o.checkout[5:].replace('-', '/')}({o.nights}泊)"
    lines = [
        f"{s.label} {o.hotel_name}",
        f"  [{o.party}] {o.area_label} tier{o.tier} / {stay}",
        f"  {o.room_name or '-'} / {o.plan_name[:50]}",
        f"  合計 {'≈' if o.extra.get('price_basis') == 'estimated' else ''}{fmt_yen(o.total_price)}"
        f"（{fmt_yen(o.price_per_night)}/泊）",
    ]
    if o.url:
        lines.append(f"  {o.url}")
    return "\n".join(lines)


def _wanted(o: Offer, f: dict[str, Any]) -> bool:
    """通知に出す条件。state には全件残すので、ここで絞っても新規判定はずれない。"""
    parties = [str(x) for x in f.get("parties", [])]
    if parties and o.party not in parties:
        return False
    nights = [int(x) for x in f.get("nights", [])]
    if nights and o.nights not in nights:
        return False
    max_tier = f.get("max_tier")
    if max_tier is not None and o.tier > int(max_tier):
        return False
    # 圏ごとの上限があればそちらを優先する（近い圏ほど高くても通す）
    by_tier = f.get("max_total_price_by_tier", {})
    cap = by_tier.get(str(o.tier), f.get("max_total_price"))
    if cap is not None:
        if o.total_price is None:
            return bool(f.get("include_unknown_price", False))
        if o.total_price > int(cap):
            return False
    return True


def filter_diff(diff: Diff, cfg: Config) -> Diff:
    f = cfg.raw.get("notify", {}).get("filter", {})
    if not f:
        return diff
    return Diff(
        new=[o for o in diff.new if _wanted(o, f)],
        price_changed=[(a, b) for a, b in diff.price_changed if _wanted(b, f)],
        gone=[o for o in diff.gone if _wanted(o, f)],
        unchanged=diff.unchanged,
    )


def build_diff_message(diff: Diff, cfg: Config) -> tuple[str, str, str | None]:
    """(title, body, click_url) を返す。通知対象がなければ body は空。"""
    diff = filter_diff(diff, cfg)
    scored_new = [(o, score_offer(o, cfg.scoring, cfg.threshold_for(o))) for o in diff.new]
    scored_new.sort(key=lambda t: (t[0].party, *sort_key(*t)))
    instant = sum(1 for _, s in scored_new if s.priority == "即")

    parts: list[str] = []
    if scored_new:
        by_party: dict[str, int] = {}
        for o, _ in scored_new:
            by_party[o.party] = by_party.get(o.party, 0) + 1
        summary = " / ".join(f"{k} {v}件" for k, v in sorted(by_party.items()))
        parts.append(f"■ 新規空き {len(scored_new)}件（即 {instant}件）\n  {summary}")
        parts.extend(format_offer(o, s) for o, s in scored_new[:MAX_NEW_LISTED])
        if len(scored_new) > MAX_NEW_LISTED:
            parts.append(f"…他 {len(scored_new) - MAX_NEW_LISTED} 件（表は NAS のログ参照）")
    if diff.price_changed:
        parts.append(f"■ 料金変動 {len(diff.price_changed)}件")
        for old, new in sorted(diff.price_changed, key=lambda t: t[1].tier)[:MAX_PRICE_LISTED]:
            parts.append(
                f"・[{new.party}] {new.hotel_name} {new.checkin[5:]}〜 {new.room_name}: "
                f"{fmt_yen(old.total_price)} → {fmt_yen(new.total_price)}\n  {new.url}"
            )
        if len(diff.price_changed) > MAX_PRICE_LISTED:
            parts.append(f"…他 {len(diff.price_changed) - MAX_PRICE_LISTED} 件")
    if diff.gone:
        parts.append(f"■ 消滅 {len(diff.gone)}件")
        parts.extend(f"・[{o.party}] {o.hotel_name} {o.checkin[5:]}〜 {o.room_name}" for o in diff.gone[:MAX_GONE_LISTED])
        if len(diff.gone) > MAX_GONE_LISTED:
            parts.append(f"  …他 {len(diff.gone) - MAX_GONE_LISTED} 件")

    if not parts:
        return "", "", None
    if scored_new:
        title = f"🏨 F1鈴鹿 宿: 新規{len(scored_new)}件" + (f" (即{instant})" if instant else "")
    else:
        title = "🏨 F1鈴鹿 宿: 変動あり"
    click = scored_new[0][0].url if scored_new and scored_new[0][0].url else None
    return title, "\n\n".join(parts), click


# ---------------------------------------------------------------------------
# 送信
# ---------------------------------------------------------------------------
class Notifier:
    def __init__(self, channels: list[str] | None = None, session: requests.Session | None = None, dry_run: bool = False):
        self.channels = channels if channels is not None else [
            c.strip() for c in os.environ.get("NOTIFY_CHANNELS", "").split(",") if c.strip()
        ]
        self.session = session or requests.Session()
        self.dry_run = dry_run

    def send(self, title: str, body: str, *, error: bool = False, click: str | None = None, priority: int = 3) -> list[str]:
        """全チャネルに送る。失敗したチャネルのエラー文字列を返す。"""
        if not body.strip():
            # ntfy は本文が空の POST を「Triggered」とだけ表示する。送らない
            log.info("本文が空のため通知しない (title=%r)", title)
            return []
        if not self.channels:
            log.warning("NOTIFY_CHANNELS 未設定: 通知をスキップ（stdout に出力）\n%s\n%s", title, body)
            return []
        failures: list[str] = []
        for ch in self.channels:
            try:
                if self.dry_run:
                    log.info("[dry-run] %s: %s", ch, title)
                    continue
                if ch == "ntfy":
                    self._ntfy(title, body, error, click, priority)
                elif ch == "line":
                    self._line(title, body, error)
                elif ch == "gmail":
                    self._gmail(title, body, error)
                else:
                    raise NotifyError(f"unknown channel: {ch}")
            except Exception as e:  # noqa: BLE001
                log.error("notify %s failed: %s", ch, e)
                failures.append(f"{ch}: {e}")
        return failures

    # --- ntfy -----------------------------------------------------------
    def _ntfy(self, title: str, body: str, error: bool, click: str | None, priority: int) -> None:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        topic = os.environ.get("NTFY_TOPIC")
        if not topic:
            raise NotifyError("NTFY_TOPIC 未設定")
        if error:
            topic = os.environ.get("NTFY_ERROR_TOPIC") or f"{topic}-errors"
        payload: dict[str, Any] = {
            "topic": topic,
            "title": clip_bytes(title, NTFY_MAX_TITLE_BYTES),
            "message": clip_bytes(body, NTFY_MAX_BODY_BYTES),
            "priority": 4 if error else priority,
            "tags": ["warning"] if error else ["hotel"],
        }
        if click:
            payload["click"] = click
        headers = {}
        token = os.environ.get("NTFY_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")  # \uXXXX 展開を避けて小さくする
        headers["Content-Type"] = "application/json; charset=utf-8"
        r = self.session.post(server, data=body_bytes, headers=headers, timeout=20)
        r.raise_for_status()

    # --- LINE Messaging API ------------------------------------------------
    def _line(self, title: str, body: str, error: bool) -> None:
        token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
        to = os.environ.get("LINE_USER_ID")
        if not token or not to:
            raise NotifyError("LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID 未設定")
        text = ("⚠ " if error else "") + title + "\n" + body
        chunks = [text[i : i + LINE_MAX_TEXT] for i in range(0, len(text), LINE_MAX_TEXT)] or [text]
        for i in range(0, len(chunks), 5):  # 1 push あたり最大 5 メッセージ
            r = self.session.post(
                LINE_PUSH_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"to": to, "messages": [{"type": "text", "text": c} for c in chunks[i : i + 5]]},
                timeout=20,
            )
            if r.status_code >= 400:
                raise NotifyError(f"LINE {r.status_code}: {r.text[:200]}")

    # --- Gmail SMTP ----------------------------------------------------------
    def _gmail(self, title: str, body: str, error: bool) -> None:
        user = os.environ.get("GMAIL_USER")
        pw = os.environ.get("GMAIL_APP_PASSWORD")
        to = os.environ.get("GMAIL_TO") or user
        if not user or not pw:
            raise NotifyError("GMAIL_USER / GMAIL_APP_PASSWORD 未設定")
        msg = EmailMessage()
        msg["Subject"] = ("⚠ " if error else "") + title
        msg["From"] = user
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)


# ---------------------------------------------------------------------------
# エラー通知の 1 日 1 回制限
# ---------------------------------------------------------------------------
ERROR_THROTTLE_FILE = "error_notify.json"


def should_notify_error(data_dir: Path, today: dt.date | None = None) -> bool:
    today = today or dt.date.today()
    path = data_dir / ERROR_THROTTLE_FILE
    if path.exists():
        try:
            last = json.loads(path.read_text(encoding="utf-8")).get("date")
            if last == today.isoformat():
                return False
        except (json.JSONDecodeError, AttributeError):
            pass
    return True


def mark_error_notified(data_dir: Path, today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ERROR_THROTTLE_FILE).write_text(json.dumps({"date": today.isoformat()}), encoding="utf-8")


REMINDER_FILE = "reminders_sent.json"


def due_reminders(cfg: Config, data_dir: Path, today: dt.date | None = None) -> list[dict[str, str]]:
    """今日が date のリマインドで未送信のもの。"""
    today = today or dt.date.today()
    path = data_dir / REMINDER_FILE
    sent: list[str] = []
    if path.exists():
        try:
            sent = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sent = []
    return [r for r in cfg.reminders if r.get("date") == today.isoformat() and r["date"] + r["message"] not in sent]


def mark_reminder_sent(data_dir: Path, reminder: dict[str, str]) -> None:
    path = data_dir / REMINDER_FILE
    sent: list[str] = []
    if path.exists():
        try:
            sent = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sent = []
    sent.append(reminder["date"] + reminder["message"])
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
