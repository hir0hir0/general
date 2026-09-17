"""標準出力向けの表整形（全角幅を考慮）。"""
from __future__ import annotations

import unicodedata
from typing import Iterable

from .config import Config
from .models import Offer
from .scoring import score_offer, sort_key


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "FWA" else 1 for c in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    fill = " " * max(0, width - _w(s))
    return fill + s if right else s + fill


def _cut(s: str, width: int) -> str:
    out = ""
    for c in s:
        if _w(out + c) > width:
            return out + "…" if _w(out) < width else out
        out += c
    return out


def render_table(rows: list[list[str]], headers: list[str], right_cols: set[int] = frozenset()) -> str:
    cols = len(headers)
    widths = [_w(h) for h in headers]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], _w(r[i]))
    line = "  ".join(_pad(h, widths[i], i in right_cols) for i, h in enumerate(headers))
    sep = "  ".join("-" * w for w in widths)
    body = [
        "  ".join(_pad(r[i], widths[i], i in right_cols) for i in range(cols))
        for r in rows
    ]
    return "\n".join([line, sep, *body])


def _price(v: int | None, offer: Offer) -> str:
    if v is None:
        return "-"
    prefix = "≈" if offer.extra.get("price_basis") == "estimated" else ""
    return f"{prefix}{v:,}"


def offers_table(offers: Iterable[Offer], cfg: Config, max_plan: int = 34) -> str:
    scored = [(o, score_offer(o, cfg.scoring, cfg.threshold_for(o))) for o in offers]
    scored.sort(key=lambda t: (t[0].party, *sort_key(*t)))
    if not scored:
        return "(空室なし)"
    rows: list[list[str]] = []
    for o, s in scored:
        rows.append(
            [
                s.priority,
                _cut(o.party, 12),
                str(o.tier),
                o.source,
                _cut(o.area_label, 16),
                _cut(o.hotel_name, 26),
                f"{o.checkin[5:].replace('-', '/')}-{o.checkout[8:]}",
                _cut(f"{o.room_name} {o.plan_name}".strip(), max_plan),
                _price(o.total_price, o),
                _price(o.price_per_night, o),
                ",".join(s.bonuses),
            ]
        )
    headers = ["判定", "人数", "T", "src", "エリア", "宿", "日程", "部屋/プラン", "合計", "/泊", "加点"]
    return render_table(rows, headers, right_cols={8, 9})


def summary_line(offers: list[Offer]) -> str:
    by_src: dict[str, int] = {}
    by_party: dict[str, int] = {}
    hotels: set[str] = set()
    for o in offers:
        by_src[o.source] = by_src.get(o.source, 0) + 1
        by_party[o.party] = by_party.get(o.party, 0) + 1
        hotels.add(f"{o.source}:{o.hotel_id}")
    parts = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
    pp = " / ".join(f"{k}: {v}件" for k, v in sorted(by_party.items())) if by_party else ""
    line = f"空室プラン {len(offers)} 件 / 宿 {len(hotels)} 軒 ({parts or 'なし'})"
    return line + ("\n  " + pp if pp else "")
