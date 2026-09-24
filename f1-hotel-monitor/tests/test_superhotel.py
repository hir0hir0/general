import datetime as dt

from conftest import FIXTURES
from f1hotel.models import Party, Stay
from f1hotel.superhotel import build_url, fetch_superhotel, offers_from_rows
from f1hotel.toyoko import FetchedPage, page_status, parse_plans

SAMPLE = (FIXTURES / "superhotel_sample.html").read_text(encoding="utf-8")
STAY = Stay(dt.date(2027, 4, 9), dt.date(2027, 4, 12))
PARTY = Party("親子2人1室", adults=1, infants_no_meal_no_bed=1, rooms=1)
HOTEL = {"code": "sh-suzuka", "name": "スーパーホテル鈴鹿", "area_label": "鈴鹿", "tier": 1}


def test_build_url_matches_reservation_engine(cfg):
    url = build_url(cfg, HOTEL, STAY, PARTY)
    assert url.startswith("https://go-superhotel.reservation.jp/ja/hotels/sh-suzuka/plans?")
    assert "checkin_date=20270409" in url and "checkout_date=20270412" in url
    assert "adults=1" in url and "rooms=1" in url and "child1=1" in url


def test_parse_marks_disabled_cards_soldout_and_ignores_points(cfg):
    rows = parse_plans(SAMPLE, cfg.raw["superhotel"])
    assert len(rows) == 3
    # ポイント（1,000pt）を料金と誤認しない
    points_row = next(r for r in rows if "Membership" in r.name)
    assert points_row.soldout and points_row.price is None
    available = [r for r in rows if not r.soldout]
    assert len(available) == 1 and available[0].price == 8700
    assert page_status(SAMPLE, rows, cfg.raw["superhotel"]) == "ok"


def test_sold_out_page_is_full_not_error(cfg):
    html = SAMPLE.replace('<li class="c-listRoom-item">', '<li class="c-listRoom-item c-listRoom-item-disabled">')
    rows = parse_plans(html, cfg.raw["superhotel"])
    assert all(r.soldout for r in rows)
    assert page_status(html, rows, cfg.raw["superhotel"]) == "full"


def test_offer_total_uses_nights_and_rooms(cfg):
    rows = parse_plans(SAMPLE, cfg.raw["superhotel"])
    offers = offers_from_rows(rows, HOTEL, STAY, "https://example/x", PARTY)
    assert len(offers) == 1
    assert offers[0].total_price == 8700 * 3
    assert offers[0].source == "superhotel" and offers[0].tier == 1
    assert offers[0].key.startswith("superhotel:sh-suzuka:")


def test_fetch_superhotel_uses_every_stay(cfg):
    calls = []

    def fake(urls, scfg):
        calls.append(urls)
        return [FetchedPage(u, SAMPLE) for u in urls]

    res = fetch_superhotel(cfg, fetcher=fake, parties=[PARTY])
    assert len(calls[0]) == len(cfg.stays)
    assert len(res.offers) == len(cfg.stays) and not res.errors
