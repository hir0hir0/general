import datetime as dt
import json

import pytest

from f1hotel.config import RakutenArea
from f1hotel.models import Party, Stay
from f1hotel.rakuten import (
    RakutenClient,
    RakutenError,
    SearchTarget,
    build_vacant_params,
    describe_tree,
    fetch_rakuten,
    load_area_tree,
    parse_area_tree,
    parse_vacant_response,
    resolve_targets,
)

AREA_RAW = {
    "areaClasses": {
        "largeClasses": [
            {
                "largeClass": [
                    {"largeClassCode": "japan", "largeClassName": "日本"},
                    {
                        "middleClasses": [
                            {
                                "middleClass": [
                                    {"middleClassCode": "mie", "middleClassName": "三重県"},
                                    {
                                        "smallClasses": [
                                            {
                                                "smallClass": [
                                                    {"smallClassCode": "kuwana", "smallClassName": "桑名・四日市・鈴鹿・湯の山"},
                                                    {
                                                        "detailClasses": [
                                                            {"detailClass": {"detailClassCode": "A", "detailClassName": "鈴鹿・白子"}},
                                                            {"detailClass": {"detailClassCode": "B", "detailClassName": "四日市"}},
                                                            {"detailClass": {"detailClassCode": "C", "detailClassName": "桑名・長島"}},
                                                            {"detailClass": {"detailClassCode": "D", "detailClassName": "湯の山温泉"}},
                                                        ]
                                                    },
                                                ]
                                            },
                                            {"smallClass": [{"smallClassCode": "tsu", "smallClassName": "津・久居・美杉"}]},
                                            {"smallClass": [{"smallClassCode": "iga", "smallClassName": "伊賀・名張"}]},
                                        ]
                                    },
                                ]
                            },
                            {
                                "middleClass": [
                                    {"middleClassCode": "aichi", "middleClassName": "愛知県"},
                                    {
                                        "smallClasses": [
                                            {
                                                "smallClass": [
                                                    {"smallClassCode": "nagoya", "smallClassName": "名古屋"},
                                                    {
                                                        "detailClasses": [
                                                            {"detailClass": {"detailClassCode": "A", "detailClassName": "名古屋駅周辺"}},
                                                            {"detailClass": {"detailClassCode": "B", "detailClassName": "栄・伏見"}},
                                                            {"detailClass": {"detailClassCode": "C", "detailClassName": "金山"}},
                                                            {"detailClass": {"detailClassCode": "D", "detailClassName": "名古屋東部"}},
                                                        ]
                                                    },
                                                ]
                                            }
                                        ]
                                    },
                                ]
                            },
                        ]
                    },
                ]
            }
        ]
    }
}


def test_parse_area_tree():
    tree = parse_area_tree(AREA_RAW)
    assert [m.code for m in tree] == ["mie", "aichi"]
    mie = tree[0]
    assert [s.code for s in mie.smalls] == ["kuwana", "tsu", "iga"]
    assert mie.smalls[0].details[0] == ("A", "鈴鹿・白子")
    assert mie.smalls[1].details == []


def test_parse_area_tree_alternative_structures():
    """ラッパーが変わっても（dict 形式・余分な入れ子）解析できること。"""
    flat = {
        "result": {
            "areaClasses": {
                "largeClasses": [
                    {
                        "largeClassCode": "japan",
                        "middleClasses": [
                            {
                                "middleClassCode": "mie",
                                "middleClassName": "三重県",
                                "smallClasses": [
                                    {
                                        "smallClassCode": "kuwana",
                                        "smallClassName": "桑名・四日市",
                                        "detailClasses": [
                                            {"detailClassCode": "A", "detailClassName": "鈴鹿・白子"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    }
    tree = parse_area_tree(flat)
    assert [m.code for m in tree] == ["mie"]
    assert tree[0].smalls[0].code == "kuwana"
    assert tree[0].smalls[0].details == [("A", "鈴鹿・白子")]
    assert parse_area_tree({}) == []
    assert "0 件" in describe_tree([])
    assert "mie" in describe_tree(tree)


def test_resolve_targets_detail_split_and_tier_dedupe():
    tree = parse_area_tree(AREA_RAW)
    areas = [
        RakutenArea(1, "鈴鹿・四日市", "mie", ["鈴鹿", "四日市"], ["鈴鹿", "白子", "四日市"]),
        RakutenArea(1, "津", "mie", ["津"], ["津"]),
        RakutenArea(2, "桑名・湯の山", "mie", ["桑名", "湯の山"], ["桑名", "湯の山"]),
        RakutenArea(3, "名古屋", "aichi", ["名古屋"], ["名古屋駅", "金山", "栄"]),
        RakutenArea(4, "岐阜", "gifu", ["岐阜"], []),
        RakutenArea(4, "桑名全部", "mie", ["桑名"], ["該当なし"]),  # detail 不一致 → 全 detail
    ]
    targets, warns = resolve_targets(tree, areas)
    keys = [(t.tier, t.middle, t.small, t.detail) for t in targets]
    assert (1, "mie", "kuwana", "A") in keys
    assert (1, "mie", "kuwana", "B") in keys
    assert (1, "mie", "tsu", None) in keys  # detail なし → small 全体
    assert (2, "mie", "kuwana", "C") in keys
    assert (2, "mie", "kuwana", "D") in keys
    assert (3, "aichi", "nagoya", "A") in keys
    assert (3, "aichi", "nagoya", "B") in keys
    assert (3, "aichi", "nagoya", "C") in keys
    assert (3, "aichi", "nagoya", "D") not in keys
    assert len(warns) == 1 and "gifu" in warns[0]
    # 「桑名全部」は detail 不一致で kuwana の全 detail を候補にするが、既に tier1/2 で選ばれているので tier は変わらない
    assert sum(1 for t in targets if t.small == "kuwana") == 4
    assert all(t.tier <= 2 for t in targets if t.small == "kuwana")
    # 「津」keyword は「津・久居・美杉」のみ（他 small に津を含む名称がない）
    assert sum(1 for t in targets if t.small == "tsu") == 1


def test_build_vacant_params(cfg):
    stay = Stay(dt.date(2027, 4, 9), dt.date(2027, 4, 11))
    t = SearchTarget(1, "鈴鹿", "mie", "kuwana", "A", "鈴鹿・白子")
    p = build_vacant_params(cfg, stay, t, 2, cfg.parties[0])
    assert p["checkinDate"] == "2027-04-09" and p["checkoutDate"] == "2027-04-11"
    assert p["adultNum"] == 1 and p["infantWithoutMBNum"] == 1 and p["roomNum"] == 1
    assert p["middleClassCode"] == "mie" and p["smallClassCode"] == "kuwana" and p["detailClassCode"] == "A"
    assert p["page"] == 2 and p["hits"] == 30 and p["responseType"] == "large"


def test_config_has_four_person_parties(cfg):
    labels = [p.label for p in cfg.parties]
    assert labels == ["親子2人1室", "4人1室", "4人2室"]
    four1, four2 = cfg.parties[1], cfg.parties[2]
    assert four1.adults == 3 and four1.infants_no_meal_no_bed == 1 and four1.rooms == 1
    assert four2.rooms == 2 and four2.people == 4


def test_build_vacant_params_four_people_two_rooms(cfg):
    stay = Stay(dt.date(2027, 4, 8), dt.date(2027, 4, 11))
    t = SearchTarget(1, "鈴鹿", "mie", "kuwana", "A", "鈴鹿・白子")
    p = build_vacant_params(cfg, stay, t, 1, cfg.parties[2])
    assert p["adultNum"] == 3 and p["roomNum"] == 2 and p["infantWithoutMBNum"] == 1


def vacant_payload(hotel_no=1234, total=36000, page=1, page_count=1):
    return {
        "pagingInfo": {"recordCount": 1, "pageCount": page_count, "page": page, "first": 1, "last": 1},
        "hotels": [
            {
                "hotel": [
                    {
                        "hotelBasicInfo": {
                            "hotelNo": hotel_no,
                            "hotelName": "テストホテル鈴鹿",
                            "hotelInformationUrl": "https://travel.rakuten.co.jp/HOTEL/1234/",
                            "planListUrl": "https://hotel.travel.rakuten.co.jp/hotelinfo/plan/1234",
                            "hotelSpecial": "大浴場あり。朝食は6:30から。",
                            "access": "近鉄白子駅より徒歩3分",
                            "nearestStation": "白子",
                        }
                    },
                    {"hotelRatingInfo": {"serviceAverage": 4.0}},
                    {
                        "roomInfo": [
                            {
                                "roomBasicInfo": {
                                    "roomClass": "twn",
                                    "roomName": "ツイン",
                                    "planId": 555,
                                    "planName": "素泊まりプラン",
                                    "withBreakfastFlag": 0,
                                    "reserveUrl": "https://hotel.travel.rakuten.co.jp/reserve/1234",
                                    "planContents": "添い寝無料",
                                }
                            },
                            {"dailyCharge": {"stayDate": "2027-04-09", "rakutenCharge": 18000, "total": 18000, "chargeFlag": 0}},
                            {"dailyCharge": {"stayDate": "2027-04-10", "rakutenCharge": 18000, "total": total, "chargeFlag": 0}},
                        ]
                    },
                    {
                        "roomInfo": [
                            {"roomBasicInfo": {"roomClass": "sgl", "roomName": "シングル", "planId": 556, "planName": "朝食付", "withBreakfastFlag": 1}},
                            {"dailyCharge": {"stayDate": "2027-04-09", "rakutenCharge": 12000, "total": 0}},
                            {"dailyCharge": {"stayDate": "2027-04-10", "rakutenCharge": 12000, "total": 0}},
                        ]
                    },
                ]
            }
        ],
    }


def test_parse_vacant_response():
    stay = Stay(dt.date(2027, 4, 9), dt.date(2027, 4, 11))
    t = SearchTarget(1, "鈴鹿・四日市", "mie", "kuwana", "A", "鈴鹿・白子")
    party = Party("親子2人1室", adults=1, infants_no_meal_no_bed=1, rooms=1)
    offers = parse_vacant_response(vacant_payload(), stay, t, party)
    assert len(offers) == 2
    o = offers[0]
    assert o.hotel_id == "1234" and o.plan_id == "555:twn" and o.total_price == 36000 and o.price_per_night == 18000
    assert o.url.endswith("/reserve/1234") and o.tier == 1 and "鈴鹿・白子" in o.area_label
    assert o.key == "rakuten:1234:555:twn:親子2人1室:2027-04-09:2027-04-11"
    assert o.party == "親子2人1室"
    # total が 0 の場合は rakutenCharge の合計にフォールバック
    assert offers[1].total_price == 24000 and offers[1].url.endswith("/plan/1234")
    assert offers[1].extra["breakfast"] is True


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        self.headers = headers
        item = self.script.pop(0)
        return FakeResp(*item)


def test_client_not_found_and_retry():
    sleeps = []
    sess = FakeSession([(429, {"error": "too_many_requests"}), (404, {"error": "not_found", "error_description": "x"})])
    c = RakutenClient("id", "pk_x", interval_sec=0, session=sess, sleep=sleeps.append)
    assert c.get("http://x", {"a": 1}) is None
    assert sess.calls[0][1]["applicationId"] == "id" and sess.calls[0][1]["accessKey"] == "pk_x"
    assert sess.calls[0][1]["format"] == "json"
    assert sess.headers["Origin"] == "https://github.com"
    assert c.request_count == 2 and sleeps == [2.0]
    assert sess.headers["Referer"].startswith("https://github.com/")


def test_client_raises_on_wrong_parameter():
    sess = FakeSession([(400, {"error": "wrong_parameter", "error_description": "checkinDate"})])
    c = RakutenClient("id", "pk_x", interval_sec=0, session=sess, sleep=lambda s: None)
    with pytest.raises(RakutenError, match="wrong_parameter"):
        c.get("http://x", {})


def test_client_requires_app_id_and_access_key():
    with pytest.raises(RakutenError):
        RakutenClient("")
    with pytest.raises(RakutenError, match="ACCESS_KEY"):
        RakutenClient("id", "")


def test_fetch_rakuten_pagination_and_dedupe(cfg):
    stay = Stay(dt.date(2027, 4, 9), dt.date(2027, 4, 11))
    t = SearchTarget(1, "鈴鹿", "mie", "kuwana", "A", "鈴鹿・白子")
    sess = FakeSession(
        [
            (200, vacant_payload(hotel_no=1, page=1, page_count=2)),
            (200, vacant_payload(hotel_no=1, page=2, page_count=2)),  # 重複 → dedupe
        ]
    )
    c = RakutenClient("id", "pk_x", interval_sec=0, session=sess, sleep=lambda s: None)
    res = fetch_rakuten(cfg, c, [t], [stay], [cfg.parties[0]])
    assert res.ok and len(res.offers) == 2
    assert [p["page"] for _, p in sess.calls] == [1, 2]


def test_fetch_rakuten_records_error_and_continues(cfg):
    stays = [Stay(dt.date(2027, 4, 8), dt.date(2027, 4, 11)), Stay(dt.date(2027, 4, 9), dt.date(2027, 4, 11))]
    t = SearchTarget(1, "鈴鹿", "mie", "kuwana", None, "桑名・四日市")
    sess = FakeSession([(400, {"error": "wrong_parameter"}), (200, vacant_payload())])
    c = RakutenClient("id", "pk_x", interval_sec=0, session=sess, sleep=lambda s: None)
    res = fetch_rakuten(cfg, c, [t], stays, [cfg.parties[0]])
    assert len(res.errors) == 1 and len(res.offers) == 2 and not res.ok


def test_fetch_rakuten_covers_every_party(cfg):
    stay = Stay(dt.date(2027, 4, 9), dt.date(2027, 4, 11))
    t = SearchTarget(1, "鈴鹿", "mie", "kuwana", "A", "鈴鹿・白子")
    sess = FakeSession([(200, vacant_payload())] * 3)
    c = RakutenClient("id", "pk_x", interval_sec=0, session=sess, sleep=lambda s: None)
    res = fetch_rakuten(cfg, c, [t], [stay])
    assert {o.party for o in res.offers} == {"親子2人1室", "4人1室", "4人2室"}
    assert [(p["adultNum"], p["roomNum"]) for _, p in sess.calls] == [(1, 1), (3, 1), (3, 2)]
    assert len(res.offers) == 6  # 3 パターン × 2 プラン


def test_area_cache(tmp_path):
    sess = FakeSession([(200, AREA_RAW)])
    c = RakutenClient("id", "pk_x", interval_sec=0, session=sess, sleep=lambda s: None)
    path = tmp_path / "areas.json"
    tree = load_area_tree(c, path)
    assert tree[0].code == "mie" and path.exists()
    # 2 回目はキャッシュから（API 呼び出しなし）
    tree2 = load_area_tree(None, path)
    assert [m.code for m in tree2] == ["mie", "aichi"]
    assert c.request_count == 1
