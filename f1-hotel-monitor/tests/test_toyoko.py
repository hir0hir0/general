import datetime as dt
import os
from pathlib import Path

import pytest

from conftest import FIXTURES
from f1hotel.config import ToyokoHotel
from f1hotel.models import Party, Stay
from f1hotel.toyoko import (
    FetchedPage,
    discover_codes,
    extract_hotel_links,
    ToyokoFetchError,
    build_url,
    fetch_pages,
    fetch_toyoko,
    offers_from_rows,
    page_status,
    parse_plans,
)

SAMPLE = (FIXTURES / "toyoko_sample.html").read_text(encoding="utf-8")
STAY = Stay(dt.date(2027, 4, 9), dt.date(2027, 4, 11))
HOTEL = ToyokoHotel("00246", "東横INN津駅西口", "津", 1)
PARTY = Party("親子2人1室", adults=1, infants_no_meal_no_bed=1, rooms=1)


def test_build_url(cfg):
    url = build_url(cfg, HOTEL, STAY, PARTY)
    assert "hotel=00246" in url and "chkin=2027/04/09" in url and "chkout=2027/04/11" in url and "adult=1" in url


def test_parse_plans_with_selectors(cfg):
    rows = parse_plans(SAMPLE, cfg.toyoko)
    assert [r.name for r in rows] == ["シングル", "エコノミーダブル", "ツイン"]
    assert [r.price for r in rows] == [9800, None, 12600]
    assert [r.soldout for r in rows] == [False, True, False]
    assert page_status(SAMPLE, rows, cfg.toyoko) == "ok"


def test_parse_plans_fallback_text(cfg):
    # セレクタが一切当たらない DOM でもテキスト走査で拾える
    html = "<div><p>ダブル</p><p>¥ 11,000 (税込)</p><p>ツイン</p><p>満室</p></div>"
    tcfg = dict(cfg.toyoko, plan_selectors=[".nope"])
    rows = parse_plans(html, tcfg)
    assert [(r.name, r.price, r.soldout) for r in rows] == [("ダブル", 11000, False), ("ツイン", None, True)]


def test_page_status_full_and_unparsed(cfg):
    tcfg = dict(cfg.toyoko, plan_selectors=[".nope"])
    assert page_status("<p>ご指定の日程は満室です</p>", [], tcfg) == "full"
    assert page_status("<p>Loading...</p>", [], tcfg) == "unparsed"


def test_offers_from_rows_multiplies_nights(cfg):
    rows = parse_plans(SAMPLE, cfg.toyoko)
    offers = offers_from_rows(rows, HOTEL, STAY, "https://example/x", PARTY)
    assert len(offers) == 2
    assert offers[0].total_price == 9800 * 2 and offers[0].price_per_night == 9800
    assert offers[0].key == "toyoko:00246:シングル:親子2人1室:2027-04-09:2027-04-11"
    assert offers[1].room_name == "ツイン"


def test_fetch_toyoko_with_fake_fetcher(cfg):
    cfg.toyoko_hotels = [HOTEL, ToyokoHotel("00000", "未設定", "x", 1)]
    calls = []

    def fake(urls, tcfg):
        calls.append(urls)
        out = []
        for u in urls:
            if "chkin=2027/04/08" in u:
                out.append(ToyokoFetchError(u, "timeout", None))
            else:
                out.append(FetchedPage(u, SAMPLE))
        return out

    res = fetch_toyoko(cfg, fetcher=fake, parties=[PARTY], stays=[STAY, Stay(dt.date(2027, 4, 8), dt.date(2027, 4, 11))])
    assert len(calls[0]) == 2  # code 未設定ホテルは除外、2 日程分
    assert len(res.offers) == 2 and len(res.errors) == 1 and "timeout" in res.errors[0]


def test_fetch_toyoko_covers_every_party(cfg):
    cfg.toyoko_hotels = [HOTEL]
    calls = []

    def fake(urls, tcfg):
        calls.append(urls)
        return [FetchedPage(u, SAMPLE) for u in urls]

    res = fetch_toyoko(cfg, fetcher=fake)
    # 4人1室（大人3名/1室）は客室定員を超えるので検索しない
    assert len(calls[0]) == 2 * len(cfg.stays)
    assert {o.party for o in res.offers} == {"親子2人1室", "4人2室"}
    assert any("room=2" in u for u in calls[0])
    assert not any("adult=3&room=1" in u for u in calls[0])


def test_fetch_toyoko_unparsed_dumps_html(cfg):
    cfg.toyoko_hotels = [HOTEL]
    stays = [STAY, Stay(dt.date(2027, 4, 8), dt.date(2027, 4, 11))]
    res = fetch_toyoko(
        cfg,
        fetcher=lambda urls, c: [FetchedPage(u, "<p>Loading</p>") for u in urls],
        parties=[PARTY],
        stays=stays,
    )
    assert not res.offers and len(res.errors) == 2
    dumps = list((cfg.data_dir / "debug").glob("toyoko_00246_*.html"))
    assert len(dumps) == 2


@pytest.mark.skipif(
    not (os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or os.environ.get("RUN_BROWSER_TESTS")),
    reason="ブラウザテストは PLAYWRIGHT_CHROMIUM_EXECUTABLE か RUN_BROWSER_TESTS=1 で有効化",
)
def test_fetch_pages_real_browser(cfg):
    url = Path(FIXTURES / "toyoko_sample.html").resolve().as_uri()
    pages = fetch_pages([url], dict(cfg.toyoko, request_interval_sec=0), with_screenshot=True)
    assert len(pages) == 1 and isinstance(pages[0], FetchedPage)
    assert pages[0].screenshot and "シングル" in pages[0].html
    rows = parse_plans(pages[0].html, cfg.toyoko)
    assert page_status(pages[0].html, rows, cfg.toyoko) == "ok"


DISCOVER_HTML = """
<html><body>
 <ul class="hotelList">
  <li><a href="/search/detail/00169/">東横INN津駅西口</a></li>
  <li><a href="https://www.toyoko-inn.com/search/detail/00246/?x=1">東横INN近鉄四日市駅北口</a></li>
  <li><a href="/hotel/00169/plan">同じ施設の別リンク</a></li>
  <li><a href="/about">会社情報</a></li>
 </ul>
</body></html>
"""


def test_extract_hotel_links():
    links = extract_hotel_links(DISCOVER_HTML)
    codes = [c for c, _, _ in links]
    assert "00169" in codes and "00246" in codes
    assert all(not u.startswith("/") for _, _, u in links)  # 絶対 URL になる
    names = dict((c, n) for c, n, _ in links)
    assert names["00246"] == "東横INN近鉄四日市駅北口"


def test_discover_codes_writes_candidates(cfg):
    # 本番設定はコード設定済みなので、未設定ホテルを 1 件足して確認する
    cfg.toyoko_hotels = [ToyokoHotel("00000", "東横INN未設定", "津", 1, search_name="津駅西口")]
    calls = []

    def fake(urls, tcfg):
        calls.append(urls)
        return [FetchedPage(u, DISCOVER_HTML) for u in urls]

    text = discover_codes(cfg, fetcher=fake)
    assert "00169" in text and "00246" in text
    assert "東横INN津駅西口" in text
    # 未設定 1 ホテル × テンプレート 3 件
    assert len(calls[0]) == 3
    assert any("keyword=" in u for u in calls[0])
