"""补全弹窗组件：上下键导航、回车/点击选择、ESC 关闭。"""

from __future__ import annotations

from typing import Any

from textual import events
from textual.message import Message as TextualMessage
from textual.widgets import Static


# 选中消息：携带选中值，由父组件处理后填入输入框。
class Selected(TextualMessage):
    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value


# 补全弹窗：显示候选列表，高亮当前项，通过 Selected 消息通知选中值。
# 不重排既有界面结构，仅以浮层形式挂载在输入区附近。
class CompletionPopup(Static):
    DEFAULT_CSS = """
    CompletionPopup {
        height: auto;
        max-height: 8;
        display: none;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._pairs: list[tuple[str, str]] = []
        self._cursor: int = 0
        self._visible: bool = False

    # 以 (display, value) 对填充弹窗并显示。
    def show_pairs(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = list(pairs)
        self._cursor = 0
        self._visible = True
        self._refresh_content()
        self.display = True

    # 以纯文本列表填充弹窗（display 与 value 相同）。
    def show(self, items: list[str]) -> None:
        self.show_pairs([(i, i) for i in items])

    # 隐藏弹窗并清空内部状态。
    def hide(self) -> None:
        self._visible = False
        self._pairs = []
        self._cursor = 0
        self.display = False
        self.update("")

    # 当前是否可见：供 ChatInput 判断是否拦截方向键/Enter/Tab。
    @property
    def is_visible(self) -> bool:
        return self._visible

    # 向上移动光标，已在顶部时不移动。
    def move_up(self) -> None:
        if not self._pairs:
            return
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh_content()

    # 向下移动光标，已在底部时不移动。
    def move_down(self) -> None:
        if not self._pairs:
            return
        if self._cursor < len(self._pairs) - 1:
            self._cursor += 1
            self._refresh_content()

    # 返回当前选中值，弹窗为空或光标越界时返回 None。
    def get_selected(self) -> str | None:
        if 0 <= self._cursor < len(self._pairs):
            return self._pairs[self._cursor][1]
        return None

    # 刷新渲染内容：当前项高亮，其余暗显。
    def _refresh_content(self) -> None:
        if not self._visible or not self._pairs:
            self.update("")
            return
        lines: list[str] = []
        for i, pair in enumerate(self._pairs):
            display = pair[0]
            if i == self._cursor:
                lines.append(f"[bold reverse] {display}[/]")
            else:
                lines.append(f"  [dim]{display}[/]")
        self.update("\n".join(lines))

    # 点击时根据行号定位到对应项，发送 Selected 消息并隐藏。
    def on_click(self, event: events.Click) -> None:
        if not self._pairs:
            return
        idx = event.y
        if 0 <= idx < len(self._pairs):
            self._cursor = idx
            self.post_message(Selected(self._pairs[idx][1]))
            self.hide()
