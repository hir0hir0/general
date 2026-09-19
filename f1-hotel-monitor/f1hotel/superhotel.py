"""スーパーホテル 空室取得（Playwright）。

- 2027/4 分は 2026/11/1 に開放される見込み。開放直後は秒単位で埋まるので
  config の watch_windows で短間隔ポーリングし、空きを見つけたら booking へ渡す。
- 中身は websource（設定駆動の共通ソース）。ここは [superhotel] を渡すだけ。
"""
from __future__ import annotations

from typing import Any, Callable

from .config import Config
from .models import Party, SourceResult, Stay
from .toyoko import FetchedPage
from .websource import fetch_web_source, offers_from_rows  # noqa: F401  (後方互換の再輸出)
from .websource import build_url as _build_url


def build_url(cfg: Config, hotel: dict[str, Any], stay: Stay, party: Party) -> str:
    return _build_url(cfg, "superhotel", hotel, stay, party)


def fetch_superhotel(
    cfg: Config,
    stays: list[Stay] | None = None,
    parties: list[Party] | None = None,
    fetcher: Callable[[list[str], dict[str, Any]], list[FetchedPage | Exception]] | None = None,
    dump_all: bool = False,
) -> SourceResult:
    return fetch_web_source(cfg, "superhotel", stays, parties, fetcher, dump_all)
