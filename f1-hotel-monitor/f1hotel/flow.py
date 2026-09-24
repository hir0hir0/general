"""設定で記述するブラウザ操作フロー（予約フォームなど）。

実サイトの DOM を見ずに実装するため、操作手順は config.toml に書けるようにする。
各ステップは {action, ...} の dict。value には {checkin} などのプレースホルダを使える。

action:
  goto        url               ページを開く
  click       selector          クリック（候補を "," 区切りで複数書ける。最初に見つかったもの）
  fill        selector, value   入力
  select      selector, value   <select> を選ぶ
  check       selector          チェックボックス/ラジオ
  wait_for    selector          要素が出るまで待つ
  wait_ms     value             固定待ち
  assert_text value             本文に含まれることを確認（無ければ中断）
  shot        name              スクリーンショット保存
  submit      selector          最終確定。confirm=False のときは実行せず終了
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


class FlowError(RuntimeError):
    pass


class FlowAborted(RuntimeError):
    """確定前に意図的に停止した（dry-run など）。"""


class PageLike(Protocol):  # Playwright の Page のうち使う部分だけ
    def goto(self, url: str, **kw: Any) -> Any: ...
    def click(self, selector: str, **kw: Any) -> Any: ...
    def fill(self, selector: str, value: str, **kw: Any) -> Any: ...
    def select_option(self, selector: str, value: str, **kw: Any) -> Any: ...
    def check(self, selector: str, **kw: Any) -> Any: ...
    def wait_for_selector(self, selector: str, **kw: Any) -> Any: ...
    def wait_for_timeout(self, ms: float) -> Any: ...
    def content(self) -> str: ...
    def screenshot(self, **kw: Any) -> bytes: ...


@dataclass
class FlowResult:
    done: list[str] = field(default_factory=list)
    submitted: bool = False
    stopped_at: str | None = None
    shots: dict[str, bytes] = field(default_factory=dict)


def render(value: Any, ctx: dict[str, str]) -> str:
    """{key} をコンテキストで置換する。未定義キーがあればエラー。"""
    s = str(value)
    missing = [k for k in PLACEHOLDER_RE.findall(s) if k not in ctx]
    if missing:
        raise FlowError(f"フロー変数が未設定: {', '.join(sorted(set(missing)))}")
    return PLACEHOLDER_RE.sub(lambda m: ctx[m.group(1)], s)


def _first_selector(page: PageLike, selectors: str, timeout_ms: int) -> str:
    """"a, b, c" のうち最初に存在するセレクタを返す。"""
    candidates = [s.strip() for s in selectors.split(",") if s.strip()]
    last: Exception | None = None
    per = max(500, timeout_ms // max(1, len(candidates)))
    for sel in candidates:
        try:
            page.wait_for_selector(sel, timeout=per)
            return sel
        except Exception as e:  # noqa: BLE001
            last = e
    raise FlowError(f"要素が見つかりません: {selectors} ({last})")


def run_flow(
    page: PageLike,
    steps: list[dict[str, Any]],
    ctx: dict[str, str],
    *,
    confirm: bool = False,
    timeout_ms: int = 15000,
    on_step: Callable[[str], None] | None = None,
) -> FlowResult:
    """steps を順に実行する。confirm=False なら submit の手前で止まる。"""
    res = FlowResult()
    for i, step in enumerate(steps):
        action = str(step.get("action", "")).strip()
        label = f"{i+1}:{action}"
        sel = step.get("selector")
        val = step.get("value")
        try:
            if action == "goto":
                page.goto(render(val or step.get("url", ""), ctx), timeout=timeout_ms)
            elif action == "click":
                page.click(_first_selector(page, str(sel), timeout_ms), timeout=timeout_ms)
            elif action == "fill":
                page.fill(_first_selector(page, str(sel), timeout_ms), render(val, ctx), timeout=timeout_ms)
            elif action == "select":
                page.select_option(_first_selector(page, str(sel), timeout_ms), render(val, ctx), timeout=timeout_ms)
            elif action == "check":
                page.check(_first_selector(page, str(sel), timeout_ms), timeout=timeout_ms)
            elif action == "wait_for":
                _first_selector(page, str(sel), timeout_ms)
            elif action == "wait_ms":
                page.wait_for_timeout(float(val or 500))
            elif action == "assert_text":
                want = render(val, ctx)
                if want not in page.content():
                    raise FlowError(f"想定の文言がありません: {want}")
            elif action == "shot":
                res.shots[str(val or label)] = page.screenshot(full_page=True)
            elif action == "submit":
                if not confirm:
                    res.stopped_at = label
                    log.info("確定前で停止（confirm=False）: %s", label)
                    return res
                page.click(_first_selector(page, str(sel), timeout_ms), timeout=timeout_ms)
                res.submitted = True
            else:
                raise FlowError(f"未知の action: {action}")
        except FlowError:
            raise
        except Exception as e:  # noqa: BLE001
            raise FlowError(f"ステップ {label} で失敗: {e}") from e
        res.done.append(label)
        if on_step:
            on_step(label)
    return res
