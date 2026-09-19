"""自動予約（スーパーホテル等）。開放と同時に取りに行く。

安全ガード（ひとつでも外れたら実行しない）:
  1. config の booking.enabled = true
  2. 環境変数 F1HOTEL_BOOKING=1（NAS 側で明示的に有効化）
  3. 予約対象が「設定した日程・人数」と完全一致
  4. 合計金額が max_total_price 以下
  5. まだ 1 件も予約していない（data/booking_state.json）
実行後は成功・失敗いずれも通知し、各ステップのスクショを data/debug/booking に残す。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .flow import FlowError, FlowResult, run_flow
from .models import Offer

log = logging.getLogger(__name__)

STATE_FILE = "booking_state.json"


class BookingBlocked(RuntimeError):
    """ガードに引っかかって実行しなかった。"""


@dataclass
class BookingRecord:
    key: str
    hotel_name: str
    party: str
    checkin: str
    checkout: str
    total_price: int | None
    booked_at: str
    submitted: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def load_booking_state(data_dir: Path) -> list[BookingRecord]:
    path = data_dir / STATE_FILE
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [BookingRecord(**r) for r in raw.get("bookings", [])]


def save_booking(data_dir: Path, rec: BookingRecord) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / STATE_FILE
    existing = load_booking_state(data_dir)
    existing.append(rec)
    path.write_text(
        json.dumps({"bookings": [r.to_dict() for r in existing]}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def guest_context(cfg: Config, offer: Offer) -> dict[str, str]:
    """フロー内で使える変数。個人情報は .env から読む。"""
    party = cfg.party(offer.party)
    ctx = {
        "url": offer.url,
        "checkin": offer.checkin,
        "checkout": offer.checkout,
        "checkin_slash": offer.checkin.replace("-", "/"),
        "checkout_slash": offer.checkout.replace("-", "/"),
        "nights": str(offer.nights),
        "adults": str(party.adults if party else 1),
        "rooms": str(party.rooms if party else 1),
        "hotel_id": offer.hotel_id,
        "plan_id": offer.plan_id,
    }
    for key, env in (
        ("name", "GUEST_NAME"),
        ("kana", "GUEST_KANA"),
        ("phone", "GUEST_PHONE"),
        ("email", "GUEST_EMAIL"),
        ("postal", "GUEST_POSTAL"),
        ("address", "GUEST_ADDRESS"),
        ("member_id", "SUPERHOTEL_MEMBER_ID"),
        ("member_pw", "SUPERHOTEL_PASSWORD"),
    ):
        v = os.environ.get(env)
        if v:
            ctx[key] = v
    return ctx


def check_guards(cfg: Config, offer: Offer) -> None:
    """実行してよいか検査。だめなら BookingBlocked。"""
    bcfg = cfg.raw.get("booking", {})
    if not bcfg.get("enabled"):
        raise BookingBlocked("config の booking.enabled が false")
    if os.environ.get("F1HOTEL_BOOKING") != "1":
        raise BookingBlocked("環境変数 F1HOTEL_BOOKING=1 が未設定")

    allowed_parties = [str(p) for p in bcfg.get("parties", [])]
    if allowed_parties and offer.party not in allowed_parties:
        raise BookingBlocked(f"人数パターン {offer.party} は対象外")

    stays = {(s.checkin.isoformat(), s.checkout.isoformat()) for s in cfg.stays}
    allowed_stays = bcfg.get("stays")
    if allowed_stays:
        stays = {(s["checkin"], s["checkout"]) for s in allowed_stays}
    if (offer.checkin, offer.checkout) not in stays:
        raise BookingBlocked(f"日程 {offer.checkin}〜{offer.checkout} は対象外")

    cap = bcfg.get("max_total_price")
    if cap is not None:
        if offer.total_price is None:
            raise BookingBlocked("料金が取得できていないため実行しない")
        if offer.total_price > int(cap):
            raise BookingBlocked(f"合計 {offer.total_price:,}円 が上限 {int(cap):,}円 を超過")

    done = load_booking_state(cfg.data_dir)
    submitted = [r for r in done if r.submitted]
    if submitted:
        raise BookingBlocked(f"すでに予約済み（{submitted[0].hotel_name} {submitted[0].booked_at}）")

    missing = [k for k in bcfg.get("required_guest_fields", []) if k not in guest_context(cfg, offer)]
    if missing:
        raise BookingBlocked(f"宿泊者情報が未設定: {', '.join(missing)}")

    if bcfg.get("require_free_cancel"):
        check_cancel_policy(bcfg, offer)


def check_cancel_policy(bcfg: dict[str, Any], offer: Offer) -> None:
    """無料キャンセルが確認できるプランだけを許可する。

    - プラン説明に「返金不可」「事前決済」などがあれば常に拒否
    - free_cancel_sources に入れたソースは、規定を人が確認済みとして本文一致を省く
    - それ以外は無料キャンセルを示す語が無ければ拒否（判断できないものは実行しない）
    """
    text = " ".join([offer.plan_name, offer.room_name, offer.plan_text, offer.hotel_text])
    ng = [k for k in bcfg.get("no_cancel_keywords", []) if k in text]
    if ng:
        raise BookingBlocked(f"取消不可・事前決済の疑いがあるプラン（{ng[0]}）")
    if offer.source in [str(x) for x in bcfg.get("free_cancel_sources", [])]:
        return
    ok = [k for k in bcfg.get("free_cancel_keywords", []) if k in text]
    if not ok:
        raise BookingBlocked("無料キャンセルの記載を確認できないプラン")


def pick_offer(cfg: Config, offers: list[Offer]) -> Offer | None:
    """予約対象を 1 件選ぶ。ガードを通るもののうち、優先度→安い順。"""
    bcfg = cfg.raw.get("booking", {})
    sources = [str(s) for s in bcfg.get("sources", [])]
    prefer = [str(p) for p in bcfg.get("prefer_parties", [])]
    ok: list[Offer] = []
    for o in offers:
        if sources and o.source not in sources:
            continue
        try:
            check_guards(cfg, o)
        except BookingBlocked:
            continue
        ok.append(o)
    if not ok:
        return None

    def rank(o: Offer) -> tuple:
        pri = prefer.index(o.party) if o.party in prefer else len(prefer)
        return (pri, o.tier, o.total_price or 10**9)

    return sorted(ok, key=rank)[0]


def book(
    cfg: Config,
    offer: Offer,
    page_factory: Callable[[], Any],
    *,
    confirm: bool | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> tuple[FlowResult, BookingRecord]:
    """1 件を予約する。confirm=None なら config の dry_run から決める。"""
    check_guards(cfg, offer)
    bcfg = cfg.raw.get("booking", {})
    flows = bcfg.get("flows", {})
    steps = steps if steps is not None else flows.get(offer.source)
    if not steps:
        raise BookingBlocked(f"{offer.source} の予約フローが未設定")
    if confirm is None:
        confirm = not bool(bcfg.get("dry_run", True))

    ctx = guest_context(cfg, offer)
    page = page_factory()
    shots_dir = cfg.data_dir / "debug" / "booking"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        result = run_flow(
            page,
            steps,
            ctx,
            confirm=bool(confirm),
            timeout_ms=int(float(bcfg.get("timeout_sec", 15)) * 1000),
            on_step=lambda s: log.info("booking step %s", s),
        )
    except FlowError as e:
        try:
            shots_dir.mkdir(parents=True, exist_ok=True)
            (shots_dir / f"{stamp}_error.png").write_bytes(page.screenshot(full_page=True))
            (shots_dir / f"{stamp}_error.html").write_text(page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        raise

    if result.shots:
        shots_dir.mkdir(parents=True, exist_ok=True)
        for name, data in result.shots.items():
            (shots_dir / f"{stamp}_{name}.png").write_bytes(data)

    rec = BookingRecord(
        key=offer.key,
        hotel_name=offer.hotel_name,
        party=offer.party,
        checkin=offer.checkin,
        checkout=offer.checkout,
        total_price=offer.total_price,
        booked_at=dt.datetime.now().isoformat(timespec="seconds"),
        submitted=result.submitted,
        note="確定" if result.submitted else f"確定前で停止（{result.stopped_at}）",
    )
    save_booking(cfg.data_dir, rec)
    return result, rec
