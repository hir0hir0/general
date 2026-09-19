"""楽天トラベル VacantHotelSearch / GetAreaClass API クライアント。

- レート制限 1 req/sec を守る（request_interval_sec）
- 該当なしは HTTP 404 + error=not_found で返るので 0 件として扱う
- エリアコードは GetAreaClass の名称にキーワード一致させて解決し、キャッシュする
- 2026/2 新仕様: applicationId と accessKey の両方をクエリで送り、Referer/Origin ヘッダーに
  アプリ登録「Allowed websites」のドメインを付ける（無いと 403）
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .config import Config, RakutenArea
from .models import Offer, Party, SourceResult, Stay

log = logging.getLogger(__name__)

# 2026/2 の仕様変更後の新エンドポイント（旧 app.rakuten.co.jp/services/api は 2026/5/14 停止）
VACANT_URL = "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426"
AREA_URL = "https://openapi.rakuten.co.jp/engine/api/Travel/GetAreaClass/20140210"
DEFAULT_REFERER = "https://github.com/hir0hir0/general"
AREA_CACHE_MAX_AGE_DAYS = 30


class RakutenError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# HTTP クライアント
# ---------------------------------------------------------------------------
class RakutenClient:
    def __init__(
        self,
        app_id: str,
        access_key: str = "",
        interval_sec: float = 1.05,
        timeout_sec: float = 20,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ):
        if not app_id:
            raise RakutenError("RAKUTEN_APP_ID が未設定です（.env を確認）")
        if not access_key:
            raise RakutenError("RAKUTEN_ACCESS_KEY が未設定です（楽天アプリ一覧の Access Key。2026/2 以降必須）")
        self.app_id = app_id
        self.access_key = access_key
        referer = os.environ.get("RAKUTEN_REFERER", DEFAULT_REFERER)
        origin = re.match(r"https?://[^/]+", referer)
        self.headers = {
            "Referer": referer,
            "Origin": origin.group(0) if origin else referer,
            "User-Agent": "f1-hotel-monitor/0.1 (+" + referer + ")",
        }
        self.interval = interval_sec
        self.timeout = timeout_sec
        self.session = session or requests.Session()
        self.sleep = sleep
        self.max_retries = max_retries
        self._last_call = 0.0
        self.request_count = 0

    def _throttle(self) -> None:
        wait = self.interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self.sleep(wait)

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """GET して JSON を返す。該当なし（404 not_found）は None。"""
        q = {"applicationId": self.app_id, "accessKey": self.access_key, "format": "json", **params}
        headers = self.headers
        backoff = 2.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_call = time.monotonic()
            self.request_count += 1
            resp = self.session.get(url, params=q, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            try:
                body = resp.json()
            except ValueError:
                body = {"error": f"http_{resp.status_code}", "error_description": resp.text[:200]}
            err = body.get("error", "")
            if resp.status_code == 404 and err == "not_found":
                return None
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                log.warning("rakuten %s %s -> retry in %.0fs", resp.status_code, err, backoff)
                self.sleep(backoff)
                backoff *= 2
                continue
            raise RakutenError(f"{resp.status_code} {err}: {body.get('error_description', '')}")
        raise RakutenError("retry exhausted")

    def get_area_classes(self) -> dict[str, Any]:
        data = self.get(AREA_URL, {})
        if not data:
            raise RakutenError("GetAreaClass returned empty")
        return data

    def vacant_search(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self.get(VACANT_URL, params)


# ---------------------------------------------------------------------------
# エリアツリー
# ---------------------------------------------------------------------------
@dataclass
class SmallClass:
    code: str
    name: str
    details: list[tuple[str, str]] = field(default_factory=list)  # (code, name)


@dataclass
class MiddleClass:
    code: str
    name: str
    smalls: list[SmallClass] = field(default_factory=list)


def _collect_classes(node: Any, level: str) -> list[tuple[dict[str, Any], Any]]:
    """JSON のどこにあっても level のクラス要素を拾う（ラッパー構造の違いを吸収）。

    戻り値は (info, container)。info は Code/Name を含む dict、container は
    その要素の入れ子（下位クラスを探す対象）。
    """
    code_key = f"{level}ClassCode"
    out: list[tuple[dict[str, Any], Any]] = []
    if isinstance(node, dict):
        if code_key in node:
            return [(node, node)]
        for v in node.values():
            out.extend(_collect_classes(v, level))
    elif isinstance(node, list):
        merged: dict[str, Any] = {}
        for item in node:
            if isinstance(item, dict) and code_key in item:
                merged.update(item)
        if merged:
            return [(merged, node)]
        for item in node:
            out.extend(_collect_classes(item, level))
    return out


def parse_area_tree(raw: dict[str, Any]) -> list[MiddleClass]:
    out: list[MiddleClass] = []
    seen: set[str] = set()
    for minfo, mnode in _collect_classes(raw, "middle"):
        code = str(minfo.get("middleClassCode", ""))
        if not code or code in seen:
            continue
        seen.add(code)
        mid = MiddleClass(code, str(minfo.get("middleClassName", "")))
        small_seen: set[str] = set()
        for sinfo, snode in _collect_classes(mnode, "small"):
            scode = str(sinfo.get("smallClassCode", ""))
            if not scode or scode in small_seen:
                continue
            small_seen.add(scode)
            small = SmallClass(scode, str(sinfo.get("smallClassName", "")))
            detail_seen: set[str] = set()
            for dinfo, _ in _collect_classes(snode, "detail"):
                dcode = str(dinfo.get("detailClassCode", ""))
                if dcode and dcode not in detail_seen:
                    detail_seen.add(dcode)
                    small.details.append((dcode, str(dinfo.get("detailClassName", ""))))
            mid.smalls.append(small)
        out.append(mid)
    return out


def dump_tree(tree: list[MiddleClass]) -> str:
    """都道府県 > 地区 > 詳細地区 をテキスト化（config 調整用に /data へ書き出す）。"""
    lines = [describe_tree(tree, limit=100), ""]
    for m in tree:
        lines.append(f"{m.code}\t{m.name}")
        for sm in m.smalls:
            lines.append(f"  {sm.code}\t{sm.name}")
            for code, name in sm.details:
                lines.append(f"    {code}\t{name}")
    return "\n".join(lines) + "\n"


def describe_tree(tree: list[MiddleClass], limit: int = 12) -> str:
    """解析結果の要約（設定が合わないときの診断用）。"""
    if not tree:
        return "エリア一覧を解析できませんでした（0 件）"
    items = [f"{m.code}({m.name},{len(m.smalls)}地区)" for m in tree[:limit]]
    more = f" …他 {len(tree) - limit}" if len(tree) > limit else ""
    return f"解析できた都道府県 {len(tree)} 件: " + ", ".join(items) + more


def load_area_tree(client: RakutenClient | None, cache_path: Path, force: bool = False) -> list[MiddleClass]:
    """キャッシュ（30日）を優先し、なければ API から取得して保存する。"""
    if cache_path.exists() and not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            fetched = dt.date.fromisoformat(cached["fetched"])
            if (dt.date.today() - fetched).days <= AREA_CACHE_MAX_AGE_DAYS or client is None:
                return parse_area_tree(cached["raw"])
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            log.warning("area cache broken (%s); refetch", e)
    if client is None:
        raise RakutenError("エリアキャッシュがなく、API クライアントもありません")
    raw = client.get_area_classes()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"fetched": dt.date.today().isoformat(), "raw": raw}, ensure_ascii=False),
        encoding="utf-8",
    )
    return parse_area_tree(raw)


@dataclass(frozen=True)
class SearchTarget:
    tier: int
    label: str  # 設定の label + 一致した名称
    middle: str
    small: str
    detail: str | None = None
    name: str = ""
    hotel_no: str = ""  # 名指しで見る宿。エリアではなく hotelNo で検索する

    @property
    def area_key(self) -> tuple[str, str, str | None]:
        return (self.middle, self.small, self.detail)


def resolve_targets(tree: list[MiddleClass], areas: list[RakutenArea]) -> tuple[list[SearchTarget], list[str]]:
    """設定エリア → 検索対象（middle/small/detail）に解決する。

    戻り値: (targets, warnings)。同じ area_key は tier の小さい方を残す。
    """
    by_middle = {m.code: m for m in tree}
    chosen: dict[tuple[str, str, str | None], SearchTarget] = {}
    warnings: list[str] = []

    for area in areas:
        mid = by_middle.get(area.middle)
        if mid is None:
            near = [m.code for m in tree if area.middle[:2] in m.code or m.code[:2] == area.middle[:2]]
            hint = f" 近い候補: {', '.join(near[:5])}" if near else f" 全 {len(tree)} 件中になし"
            warnings.append(f"[{area.label}] middleClassCode '{area.middle}' が見つかりません.{hint}")
            continue
        matched_smalls = [s for s in mid.smalls if any(k in s.name for k in area.small_keywords)]
        if not matched_smalls:
            warnings.append(f"[{area.label}] small_keywords {area.small_keywords} に一致する地区なし（middle={area.middle}）")
            continue
        for small in matched_smalls:
            candidates: list[SearchTarget] = []
            if area.detail_keywords:
                for code, name in small.details:
                    if any(k in name for k in area.detail_keywords):
                        candidates.append(SearchTarget(area.tier, area.label, mid.code, small.code, code, name))
            if not candidates and small.details:
                # 新仕様では detailClassCode まで要求されるため、一致しなければ全 detail を対象にする
                candidates.extend(
                    SearchTarget(area.tier, area.label, mid.code, small.code, code, name) for code, name in small.details
                )
            if not candidates:
                candidates.append(SearchTarget(area.tier, area.label, mid.code, small.code, None, small.name))
            for t in candidates:
                prev = chosen.get(t.area_key)
                if prev is None or t.tier < prev.tier:
                    chosen[t.area_key] = t
    targets = sorted(chosen.values(), key=lambda t: (t.tier, t.middle, t.small, t.detail or ""))
    return targets, warnings


# ---------------------------------------------------------------------------
# 空室検索
# ---------------------------------------------------------------------------
def build_vacant_params(cfg: Config, stay: Stay, target: SearchTarget, page: int, party: Party) -> dict[str, Any]:
    p: dict[str, Any] = {
        "checkinDate": stay.checkin.isoformat(),
        "checkoutDate": stay.checkout.isoformat(),
        "adultNum": party.adults,
        "roomNum": party.rooms,
        "responseType": "large",
        "datumType": 1,
        "hits": int(cfg.rakuten.get("hits", 30)),
        "page": page,
        "sort": "+roomCharge",
    }
    if target.hotel_no:
        # 名指しの宿はエリア指定と併用できない。満室なら 404 not_found が返る
        p["hotelNo"] = target.hotel_no
        return p
    p["largeClassCode"] = "japan"
    p["middleClassCode"] = target.middle
    p["smallClassCode"] = target.small
    if target.detail:
        p["detailClassCode"] = target.detail
    if party.infants_no_meal_no_bed:
        p["infantWithoutMBNum"] = party.infants_no_meal_no_bed
    return p


def _split_hotel(entry: Any) -> tuple[dict[str, Any], list[list[dict[str, Any]]]]:
    """1 ホテル分の配列から hotelBasicInfo と roomInfo（プラン毎）を取り出す。"""
    parts = entry.get("hotel", []) if isinstance(entry, dict) else entry
    basic: dict[str, Any] = {}
    rooms: list[list[dict[str, Any]]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "hotelBasicInfo" in part:
            basic = part["hotelBasicInfo"]
        elif "roomInfo" in part:
            rooms.append(part["roomInfo"])
    return basic, rooms


def parse_vacant_response(data: dict[str, Any], stay: Stay, target: SearchTarget, party: Party) -> list[Offer]:
    offers: list[Offer] = []
    fetched = dt.datetime.now().isoformat(timespec="seconds")
    for entry in data.get("hotels", []):
        basic, rooms = _split_hotel(entry)
        hotel_no = str(basic.get("hotelNo", ""))
        if not hotel_no:
            continue
        hotel_text = " ".join(
            str(basic.get(k, "")) for k in ("hotelSpecial", "access", "nearestStation", "parkingInformation")
        )
        for room in rooms:
            rb: dict[str, Any] = {}
            charges: list[dict[str, Any]] = []
            for item in room:
                if "roomBasicInfo" in item:
                    rb = item["roomBasicInfo"]
                elif "dailyCharge" in item:
                    charges.append(item["dailyCharge"])
            if not rb:
                continue
            # dailyCharge.total は「その日 1 泊・1 室あたりの合計」。
            # API は初日分しか返さないことが多いので、その場合は 1 泊単価 × 泊数 で概算する。
            day_totals: list[int] = []
            for c in charges:
                v = c.get("total")
                if v in (None, "", 0):
                    v = c.get("rakutenCharge") or 0
                try:
                    day_totals.append(int(v))
                except (TypeError, ValueError):
                    continue
            day_totals = [v for v in day_totals if v > 0]
            total: int | None = None
            basis = "none"
            if len(day_totals) >= stay.nights:
                total = sum(day_totals[: stay.nights]) * party.rooms
                basis = "daily_sum"
            elif day_totals:
                per_night = sum(day_totals) / len(day_totals)
                total = int(round(per_night * stay.nights)) * party.rooms
                basis = "estimated"
            offers.append(
                Offer(
                    source="rakuten",
                    hotel_id=hotel_no,
                    hotel_name=str(basic.get("hotelName", "")),
                    area_label=f"{target.label}/{target.name}" if target.name else target.label,
                    tier=target.tier,
                    party=party.label,
                    checkin=stay.checkin.isoformat(),
                    checkout=stay.checkout.isoformat(),
                    nights=stay.nights,
                    plan_id=str(rb.get("planId", "")) + ":" + str(rb.get("roomClass", "")),
                    plan_name=str(rb.get("planName", "")),
                    room_name=str(rb.get("roomName", "")),
                    total_price=total,
                    url=str(rb.get("reserveUrl") or basic.get("planListUrl") or basic.get("hotelInformationUrl") or ""),
                    access=str(basic.get("access", "")),
                    plan_text=str(rb.get("planContents", "")),
                    hotel_text=hotel_text,
                    extra={
                        "price_basis": basis,  # daily_sum=日別合算 / estimated=初日単価×泊数
                        "daily_charges": len(day_totals),
                        "breakfast": bool(rb.get("withBreakfastFlag")),
                        "review": basic.get("reviewAverage"),
                        "fetched_at": fetched,
                        "hotel_url": basic.get("hotelInformationUrl", ""),
                        # 車で行く場合の判断材料。API の値は「あり」「無料」など短い文字列
                        "parking": str(basic.get("parkingInformation", "") or ""),
                    },
                )
            )
    return offers


def save_sample(data: dict[str, Any], path: Path, max_hotels: int = 2) -> None:
    """料金仕様の確認用に、生応答の先頭数件を保存する。"""
    try:
        sample = dict(data)
        sample["hotels"] = data.get("hotels", [])[:max_hotels]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, TypeError) as e:
        log.warning("サンプル保存に失敗: %s", e)


def watch_hotel_targets(cfg: Config) -> list[SearchTarget]:
    """config の [[rakuten.watch_hotels]] を、hotelNo で引く検索対象にする。

    エリア検索は「空室のある宿」しか返さないので、名指しで押さえたい宿は
    そこに現れない。満室のあいだは 404 not_found（＝該当なし）が返るだけなので、
    空きが出た瞬間に拾える。
    """
    out: list[SearchTarget] = []
    for h in cfg.rakuten.get("watch_hotels", []):
        no = str(h.get("hotel_no", "")).strip()
        if not no:
            continue
        out.append(SearchTarget(
            tier=int(h.get("tier", 1)),
            label=str(h.get("label", "名指し")),
            middle="", small="",
            name=str(h.get("name", no)),
            hotel_no=no,
        ))
    return out


def fetch_rakuten(
    cfg: Config,
    client: RakutenClient,
    targets: list[SearchTarget],
    stays: list[Stay] | None = None,
    parties: list[Party] | None = None,
) -> SourceResult:
    stays = stays or cfg.stays
    parties = parties or cfg.parties
    max_pages = int(cfg.rakuten.get("max_pages", 5))
    result = SourceResult(source="rakuten", offers=[])
    seen: set[str] = set()
    sampled_nights: set[int] = set()
    for party in parties:
      for stay in stays:
        for target in targets:
            page = 1
            while page <= max_pages:
                params = build_vacant_params(cfg, stay, target, page, party)
                try:
                    data = client.vacant_search(params)
                except (RakutenError, requests.RequestException) as e:
                    msg = f"rakuten [{party.label}] {target.name or target.small} {stay.label} p{page}: {e}"
                    log.error(msg)
                    result.errors.append(msg)
                    break
                if not data:
                    log.info("rakuten [%s] %s %s: 該当なし", party.label, target.name or target.small, stay.label)
                    break
                if stay.nights not in sampled_nights and data.get("hotels"):
                    save_sample(data, cfg.data_dir / f"rakuten_sample_{stay.nights}nights.json")
                    sampled_nights.add(stay.nights)
                offers = parse_vacant_response(data, stay, target, party)
                for o in offers:
                    if o.key not in seen:
                        seen.add(o.key)
                        result.offers.append(o)
                paging = data.get("pagingInfo", {})
                if page >= int(paging.get("pageCount", 1)):
                    break
                page += 1
    bases = {}
    for o in result.offers:
        b = o.extra.get("price_basis", "?")
        bases[b] = bases.get(b, 0) + 1
    log.info("rakuten: %d offers, %d requests, 料金の根拠 %s", len(result.offers), client.request_count, bases)
    return result
