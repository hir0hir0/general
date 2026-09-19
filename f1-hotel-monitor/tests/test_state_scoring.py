import dataclasses
import json

import pytest

from f1hotel.models import Offer
from f1hotel.scoring import score_offer, sort_key
from f1hotel.state import append_history, compute_diff, load_state, merge_for_save, save_state


def mk(key_id="1", price=36000, source="rakuten", **kw):
    base = dict(
        source=source,
        hotel_id=key_id,
        hotel_name=f"宿{key_id}",
        area_label="鈴鹿",
        tier=1,
        party="親子2人1室",
        checkin="2027-04-09",
        checkout="2027-04-11",
        nights=2,
        plan_id="p",
        plan_name="素泊まり",
        room_name="ツイン",
        total_price=price,
        url="https://example/" + key_id,
    )
    base.update(kw)
    return Offer(**base)


def test_diff_new_price_gone():
    prev = {o.key: o for o in [mk("1"), mk("2", 40000), mk("3")]}
    curr = [mk("1"), mk("2", 38000), mk("4")]
    d = compute_diff(prev, curr, {"rakuten", "toyoko"})
    assert [o.hotel_id for o in d.new] == ["4"]
    assert [(a.total_price, b.total_price) for a, b in d.price_changed] == [(40000, 38000)]
    assert [o.hotel_id for o in d.gone] == ["3"]
    assert d.unchanged == 1 and not d.empty


def test_diff_failed_source_not_gone():
    prev = {o.key: o for o in [mk("1"), mk("t", source="toyoko")]}
    curr = [mk("1")]
    d = compute_diff(prev, curr, sources_ok={"rakuten"})  # toyoko は失敗
    assert d.gone == [] and d.empty
    merged = merge_for_save(prev, curr, {"rakuten"})
    assert {o.key for o in merged} == set(prev)


def test_state_roundtrip_and_history(tmp_path):
    offers = [mk("1"), mk("2", extra={"breakfast": True})]
    save_state(tmp_path, offers, meta={"x": 1})
    loaded = load_state(tmp_path)
    assert set(loaded) == {o.key for o in offers}
    assert dataclasses.asdict(loaded[offers[1].key]) == dataclasses.asdict(offers[1])
    d = compute_diff({}, offers)
    append_history(tmp_path, d)
    files = list((tmp_path / "history").glob("*.jsonl"))
    assert len(files) == 1
    events = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["new", "new"]


def test_threshold_is_per_party(cfg):
    cheap = mk("1", price=90000, party="4人1室")   # 1泊 45,000 → 4人枠では「即」
    s = score_offer(cheap, cfg.scoring, cfg.threshold_for(cheap))
    assert s.priority == "即"
    same_price_2ppl = mk("2", price=90000, party="親子2人1室")
    assert score_offer(same_price_2ppl, cfg.scoring, cfg.threshold_for(same_price_2ppl)).priority == "参考"


def test_key_separates_parties():
    a = mk("1", party="4人1室")
    b = mk("1", party="4人2室")
    assert a.key != b.key


def test_load_state_missing_or_broken(tmp_path):
    assert load_state(tmp_path) == {}
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
    assert load_state(tmp_path) == {}


def test_scoring_priority_and_bonuses(cfg):
    o = mk(
        "1",
        price=36000,
        access="近鉄白子駅より徒歩3分",
        hotel_text="大浴場完備。朝食は6:30〜9:00。",
        room_name="ツイン",
    )
    s = score_offer(o, cfg.scoring, cfg.threshold_for(o))
    assert s.priority == "即"
    assert s.bonuses == ["駅徒歩3分", "大浴場", "朝食6:30〜", "ツイン"]

    o2 = mk("2", price=60000, access="駅から徒歩12分", room_name="シングル", extra={"breakfast": True})
    s2 = score_offer(o2, cfg.scoring, cfg.threshold_for(o2))
    assert s2.priority == "参考" and s2.bonuses == ["朝食付"]

    o3 = mk("3", price=None)
    assert score_offer(o3, cfg.scoring, cfg.threshold_for(o3)).priority == "不明"

    o4 = mk("4", price=20000, access="駅前")
    assert "駅前" in score_offer(o4, cfg.scoring, cfg.threshold_for(o4)).bonuses


def test_sort_key_orders_instant_first(cfg):
    items = [mk("a", 60000), mk("b", 30000, tier=2), mk("c", 40000)]
    scored = [(o, score_offer(o, cfg.scoring, cfg.threshold_for(o))) for o in items]
    scored.sort(key=lambda t: sort_key(*t))
    assert [o.hotel_id for o, _ in scored] == ["c", "b", "a"]


def test_booking_requires_confirmable_free_cancellation(cfg, monkeypatch):
    from f1hotel.booking import BookingBlocked, check_guards

    monkeypatch.setenv("F1HOTEL_BOOKING", "1")
    for k, v in (("GUEST_NAME", "山田"), ("GUEST_KANA", "ヤマダ"), ("GUEST_PHONE", "09000000000"), ("GUEST_EMAIL", "a@b.c")):
        monkeypatch.setenv(k, v)
    cfg.raw["booking"]["enabled"] = True

    def offer(**kw):
        return mk("1", price=90000, party="親子2人1室", checkin="2027-04-09", checkout="2027-04-12", nights=3, **kw)

    # 記載が無い → 実行しない
    with pytest.raises(BookingBlocked, match="無料キャンセル"):
        check_guards(cfg, offer(plan_text="素泊まり"))
    # 取消不可 → 実行しない
    with pytest.raises(BookingBlocked, match="取消不可|事前決済"):
        check_guards(cfg, offer(plan_text="早期割引・事前決済・返金不可"))
    # 無料キャンセルが読み取れる → 通る
    check_guards(cfg, offer(plan_text="現地決済。前日まで無料でキャンセルできます"))
    # 規定確認済みのソース（スーパーホテル）は本文に記載が無くても通る
    check_guards(cfg, offer(source="superhotel", plan_text="素泊まり"))
    # ただし事前決済プランは確認済みソースでも拒否
    with pytest.raises(BookingBlocked, match="取消不可|事前決済"):
        check_guards(cfg, offer(source="superhotel", plan_text="事前カード決済・返金不可"))


def test_parking_bonus_for_car_trip(cfg):
    free = mk("1", extra={"parking": "あり", "fetched_at": "x"}, plan_name="無料駐車場付き 4名")
    assert "駐車場無料" in score_offer(free, cfg.scoring, cfg.threshold_for(free)).bonuses
    有 = mk("2", extra={"parking": "あり"})
    assert "駐車場あり" in score_offer(有, cfg.scoring, cfg.threshold_for(有)).bonuses
    無 = mk("3", extra={"parking": "なし"})
    b = score_offer(無, cfg.scoring, cfg.threshold_for(無)).bonuses
    assert "駐車場あり" not in b and "駐車場無料" not in b
    未知 = mk("4")
    assert not [x for x in score_offer(未知, cfg.scoring, cfg.threshold_for(未知)).bonuses if "駐車場" in x]
