"""東横INN 空室取得（Playwright）。

- URL テンプレート（config.toml [toyoko].url_template）でホテル×日程のページを開く
- DOM は変わり得るので、セレクタは設定で候補を並べ、最後はテキスト走査でフォールバック
- 解析できない／エラー時は data/debug/ にスクショと HTML を保存する
- `monitor.py toyoko-dump` で初回のセレクタ調整用ダンプを取れる
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from bs4 import BeautifulSoup

from .config import Config, ToyokoHotel
from .models import Offer, Party, SourceResult, Stay

log = logging.getLogger(__name__)

PRICE_RE = re.compile(r"(?:¥|￥)?\s*([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{3,6})\s*円?")
ROOM_WORDS = ("シングル", "ダブル", "ツイン", "エコノミー", "デラックス", "ハートフル", "和室", "トリプル", "ファミリー")


class ToyokoParseError(RuntimeError):
    pass


@dataclass
class PlanRow:
    name: str
    price: int | None
    soldout: bool
    text: str


# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------
def build_url(cfg: Config, hotel: ToyokoHotel, stay: Stay, party: Party) -> str:
    t = cfg.toyoko
    fmt = t.get("date_format", "%Y/%m/%d")
    return t["url_template"].format(
        code=hotel.code,
        ci=stay.checkin.strftime(fmt),
        co=stay.checkout.strftime(fmt),
        adults=party.adults,
        rooms=party.rooms,
    )


# ---------------------------------------------------------------------------
# 解析（ブラウザ非依存・テスト可能）
# ---------------------------------------------------------------------------
def _first_price(text: str) -> int | None:
    for m in PRICE_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        if raw.isdigit():
            v = int(raw)
            if 1000 <= v <= 500_000:
                return v
    return None


def _select_first(soup_or_tag: Any, selectors: Iterable[str]):
    for sel in selectors:
        try:
            found = soup_or_tag.select(sel)
        except Exception:  # 不正セレクタは無視
            continue
        if found:
            return found
    return []


def parse_plans(html: str, toyoko_cfg: dict[str, Any]) -> list[PlanRow]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    soldout_kw = toyoko_cfg.get("soldout_keywords", ["満室"])
    rows: list[PlanRow] = []

    elements = _select_first(soup, toyoko_cfg.get("plan_selectors", []))
    for el in elements:
        text = " ".join(el.get_text(" ", strip=True).split())
        if not text:
            continue
        name_el = _select_first(el, toyoko_cfg.get("name_selectors", []))
        name = name_el[0].get_text(" ", strip=True) if name_el else text.split(" ")[0]
        price_el = _select_first(el, toyoko_cfg.get("price_selectors", []))
        price = _first_price(price_el[0].get_text(" ", strip=True)) if price_el else _first_price(text)
        soldout = any(k in text for k in soldout_kw)
        rows.append(PlanRow(name=name[:80], price=price, soldout=soldout and price is None, text=text[:300]))

    if rows:
        return rows

    # フォールバック: 本文テキストを行単位で走査し、「部屋名らしい行」+「価格らしい行」を組にする
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    lines = [ln for ln in lines if ln]
    last_name: str | None = None
    for ln in lines:
        if any(w in ln for w in ROOM_WORDS) and len(ln) <= 60:
            last_name = ln
            if any(k in ln for k in soldout_kw):
                rows.append(PlanRow(name=ln, price=None, soldout=True, text=ln))
                last_name = None
            continue
        price = _first_price(ln) if "円" in ln or "¥" in ln or "￥" in ln else None
        if price and last_name:
            rows.append(PlanRow(name=last_name, price=price, soldout=False, text=f"{last_name} {ln}"))
            last_name = None
        elif last_name and any(k in ln for k in soldout_kw):
            rows.append(PlanRow(name=last_name, price=None, soldout=True, text=f"{last_name} {ln}"))
            last_name = None
    return rows


def page_status(html: str, rows: list[PlanRow], toyoko_cfg: dict[str, Any]) -> str:
    """'ok' | 'full' | 'unparsed'"""
    if any(r.price is not None and not r.soldout for r in rows):
        return "ok"
    if rows and all(r.soldout for r in rows):
        return "full"
    soldout_kw = toyoko_cfg.get("soldout_keywords", ["満室"])
    if any(k in html for k in soldout_kw):
        return "full"
    return "unparsed"


def offers_from_rows(rows: list[PlanRow], hotel: ToyokoHotel, stay: Stay, url: str, party: Party) -> list[Offer]:
    offers: list[Offer] = []
    fetched = dt.datetime.now().isoformat(timespec="seconds")
    for i, r in enumerate(rows):
        if r.soldout or r.price is None:
            continue
        # 東横INN のページは 1 泊あたり表示が普通なので、泊数を掛けて合計にする
        total = (r.price * stay.nights if r.price < 60_000 else r.price) * party.rooms
        offers.append(
            Offer(
                source="toyoko",
                hotel_id=hotel.code,
                hotel_name=hotel.name,
                area_label=hotel.area_label,
                tier=hotel.tier,
                party=party.label,
                checkin=stay.checkin.isoformat(),
                checkout=stay.checkout.isoformat(),
                nights=stay.nights,
                plan_id=re.sub(r"\s+", "_", r.name)[:40] or f"row{i}",
                plan_name=r.name,
                room_name=r.name,
                total_price=total,
                url=url,
                access="駅前",  # 津駅西口 / 近鉄四日市駅北口 いずれも駅直近
                plan_text=r.text,
                hotel_text=hotel.name,
                extra={"fetched_at": fetched, "price_display": r.price},
            )
        )
    return offers


# ---------------------------------------------------------------------------
# 施設コードの発見（公式サイトのリンクから拾う）
# ---------------------------------------------------------------------------
CODE_RE = re.compile(r"/(?:search/)?(?:detail|hotel)/(?:[a-z\-]+/)?(\d{3,6})")


def extract_hotel_links(html: str, base: str = "https://www.toyoko-inn.com") -> list[tuple[str, str, str]]:
    """(code, 表示名, URL) の一覧を返す。DOM 構造に依存せず a[href] から拾う。"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = CODE_RE.search(href)
        if not m:
            continue
        code = m.group(1)
        name = " ".join(a.get_text(" ", strip=True).split())
        if not name:
            img = a.find("img")
            name = img.get("alt", "") if img else ""
        url = href if href.startswith("http") else base.rstrip("/") + "/" + href.lstrip("/")
        key = code + name
        if key in seen:
            continue
        seen.add(key)
        out.append((code, name[:60], url))
    return out


def discover_codes(cfg: Config, fetcher: Callable[[list[str], dict[str, Any]], list["FetchedPage | Exception"]] | None = None) -> str:
    """コード未設定のホテルについて候補を集め、結果テキストを返す（/data に保存する想定）。"""
    tcfg = cfg.toyoko
    templates = tcfg.get("discover", {}).get("url_templates", [])
    targets = [h for h in cfg.toyoko_hotels if h.needs_code]
    if not targets or not templates:
        return ""
    urls: list[str] = []
    labels: list[str] = []
    for h in targets:
        q = h.search_name or h.name
        for t in templates:
            urls.append(t.format(q=q))
            labels.append(f"{h.name} / {q}")
    fetch = fetcher or (lambda us, c: fetch_pages(us, c, with_screenshot=True))
    pages = fetch(urls, tcfg)
    debug_dir = cfg.data_dir / "debug"
    lines = ["# 東横INN 施設コード候補（config.toml の toyoko.hotels[].code に転記する）", ""]
    for i, (label, page) in enumerate(zip(labels, pages)):
        lines.append(f"## {label}")
        lines.append(f"   URL: {urls[i]}")
        if isinstance(page, Exception):
            lines.append(f"   取得失敗: {page}")
            lines.append("")
            continue
        _dump(debug_dir, f"toyoko_discover_{i}", page.html, page.screenshot)
        links = extract_hotel_links(page.html)
        if not links:
            lines.append("   リンクから抽出できず（HTML は data/debug に保存）")
        for code, name, url in links[:40]:
            lines.append(f"   {code}\t{name}\t{url}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# ブラウザ取得
# ---------------------------------------------------------------------------
@dataclass
class FetchedPage:
    url: str
    html: str
    screenshot: bytes | None = None


def chromium_launch_kwargs(headless: bool = True) -> dict[str, Any]:
    kw: dict[str, Any] = {"headless": headless}
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if exe:
        kw["executable_path"] = exe
    return kw


def fetch_pages(
    urls: list[str],
    toyoko_cfg: dict[str, Any],
    sleep: Callable[[float], None] = time.sleep,
    with_screenshot: bool = False,
) -> list[FetchedPage | Exception]:
    """Playwright で複数 URL を順に開き、HTML（と任意でスクショ）を返す。"""
    from playwright.sync_api import sync_playwright  # 遅延 import（楽天のみ実行時に不要）

    out: list[FetchedPage | Exception] = []
    interval = float(toyoko_cfg.get("request_interval_sec", 3.0))
    timeout_ms = int(float(toyoko_cfg.get("timeout_sec", 60)) * 1000)
    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_launch_kwargs(bool(toyoko_cfg.get("headless", True))))
        context = browser.new_context(
            user_agent=toyoko_cfg.get("user_agent"),
            locale=toyoko_cfg.get("locale", "ja-JP"),
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 2000},
        )
        page = context.new_page()
        for i, url in enumerate(urls):
            if i:
                sleep(interval)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 20_000))
                except Exception:
                    pass  # SPA で networkidle にならない場合がある
                shot = page.screenshot(full_page=True) if with_screenshot else None
                out.append(FetchedPage(url=url, html=page.content(), screenshot=shot))
            except Exception as e:  # noqa: BLE001 - 1 件失敗しても続ける
                try:
                    shot = page.screenshot(full_page=True)
                except Exception:
                    shot = None
                out.append(ToyokoFetchError(url, str(e), shot))
        browser.close()
    return out


class ToyokoFetchError(Exception):
    def __init__(self, url: str, message: str, screenshot: bytes | None = None):
        super().__init__(message)
        self.url = url
        self.screenshot = screenshot


def _dump(debug_dir: Path, name: str, html: str | None, screenshot: bytes | None) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if html is not None:
        (debug_dir / f"{name}_{stamp}.html").write_text(html, encoding="utf-8")
    if screenshot:
        (debug_dir / f"{name}_{stamp}.png").write_bytes(screenshot)


def fetch_toyoko(
    cfg: Config,
    stays: list[Stay] | None = None,
    parties: list[Party] | None = None,
    fetcher: Callable[[list[str], dict[str, Any]], list[FetchedPage | Exception]] | None = None,
    dump_all: bool = False,
) -> SourceResult:
    stays = stays or cfg.stays
    parties = parties or cfg.parties
    tcfg = cfg.toyoko
    result = SourceResult(source="toyoko", offers=[])
    hotels = [h for h in cfg.toyoko_hotels if h.code and h.code != "00000"]
    skipped = [h.name for h in cfg.toyoko_hotels if h not in hotels]
    if skipped:
        log.warning("toyoko: code 未設定のためスキップ: %s", ", ".join(skipped))
    if not hotels:
        return result

    # 東横INN の客室は 1 室 2 名程度が上限。3 名/室のパターンは検索しても無駄なので外す
    max_adults = int(tcfg.get("max_adults_per_room", 2))
    usable = [pt for pt in parties if pt.adults <= max_adults * pt.rooms]
    skipped_parties = [pt.label for pt in parties if pt not in usable]
    if skipped_parties:
        log.info("toyoko: 1室あたり大人%d名を超えるためスキップ: %s", max_adults, ", ".join(skipped_parties))
    if not usable:
        return result

    jobs: list[tuple[ToyokoHotel, Stay, Party, str]] = [
        (h, s, pt, build_url(cfg, h, s, pt)) for pt in usable for s in stays for h in hotels
    ]
    fetch = fetcher or (lambda urls, c: fetch_pages(urls, c, with_screenshot=dump_all))
    pages = fetch([u for _, _, _, u in jobs], tcfg)
    debug_dir = cfg.data_dir / "debug"

    for (hotel, stay, party, url), page in zip(jobs, pages):
        name = f"toyoko_{hotel.code}_{party.label}_{stay.checkin.isoformat()}"
        if isinstance(page, Exception):
            msg = f"toyoko [{party.label}] {hotel.name} {stay.label}: {page}"
            log.error(msg)
            result.errors.append(msg)
            _dump(debug_dir, name, None, getattr(page, "screenshot", None))
            continue
        rows = parse_plans(page.html, tcfg)
        status = page_status(page.html, rows, tcfg)
        if dump_all:
            _dump(debug_dir, name, page.html, page.screenshot)
        if status == "unparsed":
            msg = f"toyoko [{party.label}] {hotel.name} {stay.label}: ページを解析できません（DOM 変更?） url={url}"
            log.error(msg)
            result.errors.append(msg)
            if not dump_all:
                _dump(debug_dir, name, page.html, page.screenshot)
            continue
        offers = offers_from_rows(rows, hotel, stay, url, party)
        log.info("toyoko [%s] %s %s: %s (%d rows, %d offers)", party.label, hotel.name, stay.label, status, len(rows), len(offers))
        result.offers.extend(offers)
    return result
