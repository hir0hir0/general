"""スーパーホテル 空室取得（Playwright）。

- 2027/4 分は 2026/11/1 に開放される見込み。開放直後は秒単位で埋まるので
  config の watch_windows で短間隔ポーリングし、空きを見つけたら booking へ渡す。
- 解析は東横INN と同じ仕組み（設定のセレクタ候補 → テキスト走査のフォールバック）。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Callable

from .config import Config
from .models import Offer, Party, SourceResult, Stay
from .toyoko import FetchedPage, PlanRow, _dump, fetch_pages, page_status, parse_plans

log = logging.getLogger(__name__)


def build_url(cfg: Config, hotel: dict[str, Any], stay: Stay, party: Party) -> str:
    scfg = cfg.raw.get("superhotel", {})
    fmt = scfg.get("date_format", "%Y-%m-%d")
    return str(scfg["url_template"]).format(
        code=hotel.get("code", ""),
        ci=stay.checkin.strftime(fmt),
        co=stay.checkout.strftime(fmt),
        adults=party.adults,
        rooms=party.rooms,
        nights=stay.nights,
        infants=party.infants_no_meal_no_bed,
    )


def offers_from_rows(
    rows: list[PlanRow], hotel: dict[str, Any], stay: Stay, url: str, party: Party
) -> list[Offer]:
    out: list[Offer] = []
    fetched = dt.datetime.now().isoformat(timespec="seconds")
    for i, r in enumerate(rows):
        if r.soldout or r.price is None:
            continue
        # 表示は 1 泊 1 室あたりが基本
        total = (r.price * stay.nights if r.price < 60_000 else r.price) * party.rooms
        out.append(
            Offer(
                source="superhotel",
                hotel_id=str(hotel.get("code", "")),
                hotel_name=str(hotel.get("name", "スーパーホテル")),
                area_label=str(hotel.get("area_label", "")),
                tier=int(hotel.get("tier", 1)),
                party=party.label,
                checkin=stay.checkin.isoformat(),
                checkout=stay.checkout.isoformat(),
                nights=stay.nights,
                plan_id=re.sub(r"\s+", "_", r.name)[:40] or f"row{i}",
                plan_name=r.name,
                room_name=r.name,
                total_price=total,
                url=url,
                access=str(hotel.get("access", "")),
                plan_text=r.text,
                hotel_text=str(hotel.get("name", "")),
                extra={"fetched_at": fetched, "price_display": r.price, "price_basis": "daily_sum"},
            )
        )
    return out


def fetch_superhotel(
    cfg: Config,
    stays: list[Stay] | None = None,
    parties: list[Party] | None = None,
    fetcher: Callable[[list[str], dict[str, Any]], list[FetchedPage | Exception]] | None = None,
    dump_all: bool = False,
) -> SourceResult:
    scfg = cfg.raw.get("superhotel", {})
    result = SourceResult(source="superhotel", offers=[])
    hotels = [h for h in scfg.get("hotels", []) if h.get("code")]
    if not hotels or not scfg.get("url_template"):
        log.info("superhotel: 設定が未完成のためスキップ")
        return result

    stays = stays or cfg.stays
    parties = parties or cfg.parties
    max_adults = int(scfg.get("max_adults_per_room", 2))
    usable = [p for p in parties if p.adults <= max_adults * p.rooms]
    if not usable:
        return result

    jobs = [(h, s, p, build_url(cfg, h, s, p)) for p in usable for s in stays for h in hotels]
    fetch = fetcher or (lambda us, c: fetch_pages(us, c, with_screenshot=dump_all))
    pages = fetch([u for _, _, _, u in jobs], scfg)
    debug_dir = cfg.data_dir / "debug"

    for (hotel, stay, party, url), page in zip(jobs, pages):
        name = f"superhotel_{hotel.get('code')}_{party.label}_{stay.checkin.isoformat()}"
        if isinstance(page, Exception):
            msg = f"superhotel [{party.label}] {hotel.get('name')} {stay.label}: {page}"
            log.error(msg)
            result.errors.append(msg)
            _dump(debug_dir, name, None, getattr(page, "screenshot", None))
            continue
        rows = parse_plans(page.html, scfg)
        status = page_status(page.html, rows, scfg)
        if dump_all:
            _dump(debug_dir, name, page.html, page.screenshot)
        if status == "unparsed":
            msg = f"superhotel [{party.label}] {hotel.get('name')} {stay.label}: 解析できません url={url}"
            log.error(msg)
            result.errors.append(msg)
            if not dump_all:
                _dump(debug_dir, name, page.html, page.screenshot)
            continue
        offers = offers_from_rows(rows, hotel, stay, url, party)
        log.info(
            "superhotel [%s] %s %s: %s (%d rows, %d offers)",
            party.label, hotel.get("name"), stay.label, status, len(rows), len(offers),
        )
        result.offers.extend(offers)
    return result
