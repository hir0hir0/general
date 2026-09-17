import dataclasses
import json

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
    s = score_offer(o, cfg.scoring, cfg.instant_price_per_night)
    assert s.priority == "即"
    assert s.bonuses == ["駅徒歩3分", "大浴場", "朝食6:30〜", "ツイン"]

    o2 = mk("2", price=60000, access="駅から徒歩12分", room_name="シングル", extra={"breakfast": True})
    s2 = score_offer(o2, cfg.scoring, cfg.instant_price_per_night)
    assert s2.priority == "参考" and s2.bonuses == ["朝食付"]

    o3 = mk("3", price=None)
    assert score_offer(o3, cfg.scoring, cfg.instant_price_per_night).priority == "不明"

    o4 = mk("4", price=20000, access="駅前")
    assert "駅前" in score_offer(o4, cfg.scoring, cfg.instant_price_per_night).bonuses


def test_sort_key_orders_instant_first(cfg):
    items = [mk("a", 60000), mk("b", 30000, tier=2), mk("c", 40000)]
    scored = [(o, score_offer(o, cfg.scoring, cfg.instant_price_per_night)) for o in items]
    scored.sort(key=lambda t: sort_key(*t))
    assert [o.hotel_id for o, _ in scored] == ["c", "b", "a"]
