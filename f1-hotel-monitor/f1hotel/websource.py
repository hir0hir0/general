"""ブラウザで宿ページを開いて空室を拾う、設定駆動の共通ソース。

スーパーホテル公式（reservation.jp）も、じゃらんなどの OTA も、やることは同じ:

  1. 設定の url_template に日付・人数を埋めて開く
  2. 設定のセレクタ候補でプラン行を拾う（toyoko.parse_plans）
  3. 売り切れ・価格なしを捨てて Offer にする

違うのは URL の形とセレクタだけなので、config の [<ソース名>] セクションを
差し替えれば新しいサイトを足せる。selector や url_template が空のソースは
「まだ設定できていない」とみなして黙ってスキップする。
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

# 1 泊単価か滞在合計かをページから判別できないときの境目。
# これ未満なら 1 泊単価とみなして泊数を掛ける。
PER_NIGHT_GUESS_BELOW = 60_000


def build_url(cfg: Config, source: str, hotel: dict[str, Any], stay: Stay, party: Party) -> str:
    scfg = cfg.raw.get(source, {})
    fmt = scfg.get("date_format", "%Y-%m-%d")
    return str(scfg["url_template"]).format(
        code=hotel.get("code", ""),
        ci=stay.checkin.strftime(fmt),
        co=stay.checkout.strftime(fmt),
        ci_y=stay.checkin.year,
        ci_m=f"{stay.checkin.month:02d}",
        ci_d=f"{stay.checkin.day:02d}",
        adults=party.adults,
        rooms=party.rooms,
        nights=stay.nights,
        infants=party.infants_no_meal_no_bed,
    )


def _total_price(price: int, stay: Stay, party: Party, basis: str) -> int:
    """表示価格を「滞在合計・全室分」に揃える。"""
    if basis == "total":
        per_stay = price
    elif basis == "per_night":
        per_stay = price * stay.nights
    else:  # auto
        per_stay = price * stay.nights if price < PER_NIGHT_GUESS_BELOW else price
    return per_stay * party.rooms


def offers_from_rows(
    rows: list[PlanRow],
    hotel: dict[str, Any],
    stay: Stay,
    url: str,
    party: Party,
    source: str = "superhotel",
    price_basis: str = "auto",
) -> list[Offer]:
    out: list[Offer] = []
    fetched = dt.datetime.now().isoformat(timespec="seconds")
    for i, r in enumerate(rows):
        if r.soldout or r.price is None:
            continue
        out.append(
            Offer(
                source=source,
                hotel_id=str(hotel.get("code", "")),
                hotel_name=str(hotel.get("name", source)),
                area_label=str(hotel.get("area_label", "")),
                tier=int(hotel.get("tier", 1)),
                party=party.label,
                checkin=stay.checkin.isoformat(),
                checkout=stay.checkout.isoformat(),
                nights=stay.nights,
                plan_id=re.sub(r"\s+", "_", r.name)[:40] or f"row{i}",
                plan_name=r.name,
                room_name=r.name,
                total_price=_total_price(r.price, stay, party, price_basis),
                url=url,
                access=str(hotel.get("access", "")),
                plan_text=r.text,
                hotel_text=str(hotel.get("name", "")),
                extra={"fetched_at": fetched, "price_display": r.price, "price_basis": "daily_sum"},
            )
        )
    return out


def fetch_web_source(
    cfg: Config,
    source: str,
    stays: list[Stay] | None = None,
    parties: list[Party] | None = None,
    fetcher: Callable[[list[str], dict[str, Any]], list[FetchedPage | Exception]] | None = None,
    dump_all: bool = False,
) -> SourceResult:
    scfg = cfg.raw.get(source, {})
    result = SourceResult(source=source, offers=[])
    hotels = [h for h in scfg.get("hotels", []) if h.get("code")]
    template = str(scfg.get("url_template", ""))
    # セレクタ未設定（TODO のまま）のサイトは解析できないので触らない。
    # ただし dump は「セレクタを決めるために HTML が欲しい」場面なので通す。
    ready = bool(hotels and template and not template.startswith("TODO"))
    if ready and not dump_all and not scfg.get("plan_selectors"):
        ready = False
    if not ready:
        log.info("%s: 設定が未完成のためスキップ", source)
        return result

    stays = stays or cfg.stays
    parties = parties or cfg.parties
    max_adults = int(scfg.get("max_adults_per_room", 2))
    usable = [p for p in parties if p.adults <= max_adults * p.rooms]
    if not usable:
        return result
    price_basis = str(scfg.get("price_basis", "auto"))

    jobs = [(h, s, p, build_url(cfg, source, h, s, p)) for p in usable for s in stays for h in hotels]
    fetch = fetcher or (lambda us, c: fetch_pages(us, c, with_screenshot=dump_all))
    pages = fetch([u for _, _, _, u in jobs], scfg)
    debug_dir = cfg.data_dir / "debug"

    saved_sample = False
    for (hotel, stay, party, url), page in zip(jobs, pages):
        name = f"{source}_{hotel.get('code')}_{party.label}_{stay.checkin.isoformat()}"
        # 成否にかかわらず最初の 1 ページは必ず保存する（セレクタ調整用）
        if not saved_sample and not isinstance(page, Exception):
            try:
                cfg.data_dir.mkdir(parents=True, exist_ok=True)
                (cfg.data_dir / f"{source}_sample.html").write_text(page.html, encoding="utf-8")
                (cfg.data_dir / f"{source}_sample_url.txt").write_text(url + "\n", encoding="utf-8")
                saved_sample = True
            except OSError as e:
                log.warning("サンプル保存に失敗: %s", e)
        if isinstance(page, Exception):
            msg = f"{source} [{party.label}] {hotel.get('name')} {stay.label}: {page}"
            log.error(msg)
            result.errors.append(msg)
            _dump(debug_dir, name, None, getattr(page, "screenshot", None))
            continue
        rows = parse_plans(page.html, scfg)
        status = page_status(page.html, rows, scfg)
        if dump_all:
            _dump(debug_dir, name, page.html, page.screenshot)
        if status == "unparsed":
            msg = f"{source} [{party.label}] {hotel.get('name')} {stay.label}: 解析できません url={url}"
            log.error(msg)
            result.errors.append(msg)
            if not dump_all:
                _dump(debug_dir, name, page.html, page.screenshot)
            continue
        offers = offers_from_rows(rows, hotel, stay, url, party, source=source, price_basis=price_basis)
        log.info(
            "%s [%s] %s %s: %s (%d rows, %d offers)",
            source, party.label, hotel.get("name"), stay.label, status, len(rows), len(offers),
        )
        result.offers.extend(offers)
    return result
