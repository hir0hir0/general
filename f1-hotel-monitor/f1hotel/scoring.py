"""通知判定（即／参考）と子連れ加点。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Offer

WALK_RE = re.compile(r"徒歩\s*(?:約)?\s*(\d{1,2})\s*分")
BREAKFAST_RE = re.compile(r"朝食[^。\n]{0,40}?(\d{1,2})\s*[:：時]\s*(\d{2})?")


@dataclass
class Score:
    priority: str  # "即" | "参考" | "不明"
    bonuses: list[str] = field(default_factory=list)

    @property
    def points(self) -> int:
        return len(self.bonuses)

    @property
    def label(self) -> str:
        return f"【{self.priority}】" + (" +" + ",".join(self.bonuses) if self.bonuses else "")


def _walk_minutes(text: str) -> int | None:
    mins = [int(m.group(1)) for m in WALK_RE.finditer(text)]
    return min(mins) if mins else None


def _breakfast_start(text: str) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    for m in BREAKFAST_RE.finditer(text):
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        if 4 <= h <= 11:
            t = (h, mi)
            if best is None or t < best:
                best = t
    return best


def score_offer(offer: Offer, scoring_cfg: dict[str, Any], instant_price_per_night: int) -> Score:
    ppn = offer.price_per_night
    if ppn is None:
        priority = "不明"
    elif ppn <= instant_price_per_night:
        priority = "即"
    else:
        priority = "参考"

    bonuses: list[str] = []
    all_text = " ".join([offer.access, offer.hotel_text, offer.plan_text, offer.plan_name, offer.room_name])

    walk_max = int(scoring_cfg.get("walk_minutes_max", 5))
    walk = _walk_minutes(offer.access + " " + offer.hotel_text)
    if walk is not None and walk <= walk_max:
        bonuses.append(f"駅徒歩{walk}分")
    elif "駅前" in offer.access or "駅直結" in all_text:
        bonuses.append("駅前")

    if any(k in all_text for k in scoring_cfg.get("bath_keywords", ["大浴場"])):
        bonuses.append("大浴場")

    limit = scoring_cfg.get("breakfast_start_max", "06:30")
    lh, lm = (int(x) for x in str(limit).split(":"))
    bf = _breakfast_start(all_text)
    if bf is not None and bf <= (lh, lm):
        bonuses.append(f"朝食{bf[0]}:{bf[1]:02d}〜")
    elif offer.extra.get("breakfast"):
        bonuses.append("朝食付")

    room_text = offer.room_name + " " + offer.plan_name
    fam = [k for k in scoring_cfg.get("family_room_keywords", []) if k in room_text]
    if fam:
        bonuses.append(fam[0])

    return Score(priority=priority, bonuses=bonuses)


def sort_key(offer: Offer, score: Score) -> tuple:
    """通知・表の並び: 即 > 参考 > 不明、tier 小、加点多、1泊単価安。"""
    pri = {"即": 0, "参考": 1, "不明": 2}[score.priority]
    return (pri, offer.tier, -score.points, offer.price_per_night or 10**9, offer.hotel_name)
