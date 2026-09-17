"""config.toml と .env の読み込み。"""
from __future__ import annotations

import datetime as dt
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import Stay

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


@dataclass
class RakutenArea:
    tier: int
    label: str
    middle: str
    small_keywords: list[str]
    detail_keywords: list[str] = field(default_factory=list)


@dataclass
class ToyokoHotel:
    code: str
    name: str
    area_label: str
    tier: int


@dataclass
class Config:
    raw: dict[str, Any]
    stays: list[Stay]
    adults: int
    infants_no_meal_no_bed: int
    rooms: int
    instant_price_per_night: int
    daily_times: list[str]
    dense_windows: list[tuple[dt.date, dt.date]]
    reminders: list[dict[str, str]]
    rakuten_areas: list[RakutenArea]
    toyoko_hotels: list[ToyokoHotel]
    data_dir: Path
    config_path: Path

    # --- 環境変数（秘密情報） -------------------------------------------
    @property
    def rakuten_app_id(self) -> str | None:
        return os.environ.get("RAKUTEN_APP_ID") or None

    @property
    def notify_channels(self) -> list[str]:
        v = os.environ.get("NOTIFY_CHANNELS", "")
        return [c.strip() for c in v.split(",") if c.strip()]

    @property
    def rakuten(self) -> dict[str, Any]:
        return self.raw.get("rakuten", {})

    @property
    def toyoko(self) -> dict[str, Any]:
        return self.raw.get("toyoko", {})

    @property
    def scoring(self) -> dict[str, Any]:
        return self.raw.get("scoring", {})

    def is_dense_day(self, day: dt.date) -> bool:
        return any(a <= day <= b for a, b in self.dense_windows)


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def load_config(path: Path | str | None = None, env_path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    load_dotenv(env_path or PROJECT_ROOT / ".env", override=False)

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    stay = raw["stay"]
    checkout = _parse_date(stay["checkout"])
    stays = [Stay(_parse_date(ci), checkout) for ci in stay["checkins"]]
    for s in stays:
        if s.nights <= 0:
            raise ValueError(f"invalid stay: {s}")

    sched = raw.get("schedule", {})
    dense = [(_parse_date(a), _parse_date(b)) for a, b in sched.get("dense_windows", [])]

    areas = [RakutenArea(**a) for a in raw.get("rakuten", {}).get("areas", [])]
    hotels = [ToyokoHotel(**h) for h in raw.get("toyoko", {}).get("hotels", [])]

    data_dir = Path(os.environ.get("F1HOTEL_DATA_DIR") or DEFAULT_DATA_DIR)

    return Config(
        raw=raw,
        stays=stays,
        adults=int(stay.get("adults", 1)),
        infants_no_meal_no_bed=int(stay.get("infants_no_meal_no_bed", 0)),
        rooms=int(stay.get("rooms", 1)),
        instant_price_per_night=int(raw.get("thresholds", {}).get("instant_price_per_night", 25000)),
        daily_times=list(sched.get("daily_times", ["09:00", "21:00"])),
        dense_windows=dense,
        reminders=list(sched.get("reminders", [])),
        rakuten_areas=areas,
        toyoko_hotels=hotels,
        data_dir=data_dir,
        config_path=path,
    )
