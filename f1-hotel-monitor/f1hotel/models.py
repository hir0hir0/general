"""共通データモデル。"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Stay:
    """1 回分の宿泊条件（チェックイン〜チェックアウト）。"""

    checkin: dt.date
    checkout: dt.date

    @property
    def nights(self) -> int:
        return (self.checkout - self.checkin).days

    @property
    def label(self) -> str:
        return f"{self.checkin.month}/{self.checkin.day}-{self.checkout.month}/{self.checkout.day}({self.nights}泊)"


@dataclass
class Offer:
    """空室 1 件（宿 × プラン × 日程）。"""

    source: str  # "rakuten" / "toyoko" ...
    hotel_id: str
    hotel_name: str
    area_label: str
    tier: int
    checkin: str  # YYYY-MM-DD
    checkout: str  # YYYY-MM-DD
    nights: int
    plan_id: str
    plan_name: str
    room_name: str = ""
    total_price: int | None = None  # 滞在合計（円）
    url: str = ""
    access: str = ""  # 最寄駅・徒歩分などの文字列
    plan_text: str = ""  # プラン説明（子連れ加点判定に使用）
    hotel_text: str = ""  # 施設説明（子連れ加点判定に使用）
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.hotel_id}:{self.plan_id}:{self.checkin}:{self.checkout}"

    @property
    def price_per_night(self) -> int | None:
        if self.total_price is None or self.nights <= 0:
            return None
        return round(self.total_price / self.nights)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        d["price_per_night"] = self.price_per_night
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Offer":
        d = dict(d)
        d.pop("key", None)
        d.pop("price_per_night", None)
        return cls(**d)


@dataclass
class SourceResult:
    """1 ソースの取得結果。"""

    source: str
    offers: list[Offer]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors
