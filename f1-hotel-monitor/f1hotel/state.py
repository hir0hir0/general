"""前回結果の保存と差分検出。"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Offer

log = logging.getLogger(__name__)

STATE_FILE = "state.json"


@dataclass
class Diff:
    new: list[Offer] = field(default_factory=list)
    price_changed: list[tuple[Offer, Offer]] = field(default_factory=list)  # (old, new)
    gone: list[Offer] = field(default_factory=list)
    unchanged: int = 0

    @property
    def empty(self) -> bool:
        return not (self.new or self.price_changed or self.gone)


def load_state(data_dir: Path) -> dict[str, Offer]:
    path = data_dir / STATE_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("state.json が壊れています (%s)。空として扱います", e)
        return {}
    return {k: Offer.from_dict(v) for k, v in raw.get("offers", {}).items()}


def save_state(data_dir: Path, offers: list[Offer], meta: dict[str, Any] | None = None) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / STATE_FILE
    payload = {
        "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
        "meta": meta or {},
        "offers": {o.key: o.to_dict() for o in offers},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def compute_diff(prev: dict[str, Offer], curr: list[Offer], sources_ok: set[str] | None = None) -> Diff:
    """差分を計算する。

    sources_ok: 今回正常に取得できたソース名。取得失敗したソースの前回分は
    「消滅」扱いにしない（一時的な失敗で消滅→再出現の通知が乱れるのを防ぐ）。
    """
    d = Diff()
    curr_map = {o.key: o for o in curr}
    for k, o in curr_map.items():
        old = prev.get(k)
        if old is None:
            d.new.append(o)
        elif old.total_price != o.total_price:
            d.price_changed.append((old, o))
        else:
            d.unchanged += 1
    for k, old in prev.items():
        if k in curr_map:
            continue
        if sources_ok is not None and old.source not in sources_ok:
            continue
        d.gone.append(old)
    return d


def merge_for_save(prev: dict[str, Offer], curr: list[Offer], sources_ok: set[str]) -> list[Offer]:
    """保存用: 失敗したソースは前回分を引き継ぐ。"""
    kept = [o for o in prev.values() if o.source not in sources_ok]
    return kept + list(curr)


def append_history(data_dir: Path, diff: Diff) -> None:
    """差分イベントを日別 JSONL に追記（監査・後追い用）。"""
    if diff.empty:
        return
    hist = data_dir / "history"
    hist.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    path = hist / f"{now:%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for o in diff.new:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "event": "new", **o.to_dict()}, ensure_ascii=False) + "\n")
        for old, new in diff.price_changed:
            f.write(
                json.dumps(
                    {"ts": now.isoformat(timespec="seconds"), "event": "price", "old_total": old.total_price, **new.to_dict()},
                    ensure_ascii=False,
                )
                + "\n"
            )
        for o in diff.gone:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "event": "gone", **o.to_dict()}, ensure_ascii=False) + "\n")
