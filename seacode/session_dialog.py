"""会话恢复内联组件：搜索、上下导航、回车选择、ESC 取消。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.message import Message
from textual.widgets import Static

from seacode.memory.session import SessionMeta


# 把字节数格式化为人类可读的 B/KB/MB 字符串。
def _format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size}B"


# 把 last_active 转成相对时间描述：just now / N min ago / N hours ago / N days ago。
# 缺失 tzinfo 时按 UTC 处理，避免 naive datetime 减法报错。
def _relative_time(meta: SessionMeta) -> str:
    now = datetime.now(UTC)
    dt = meta.last_active
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} hours ago"
    return f"{secs // 86400} days ago"


class InlineResumeWidget(Vertical, can_focus=True):
    """内联会话恢复视图：搜索过滤、上下导航、回车选择、ESC 取消。

    最多展示 10 条会话，超出显示"↓ N more session(s)"提示；
    选中后通过 Selected 消息向上传递 session_id（None 表示取消）。
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    class Selected(Message):
        """用户选择了一个会话（session_id 为 None 表示取消）。"""

        def __init__(self, session_id: str | None) -> None:
            super().__init__()
            self.session_id = session_id

    # 接收 SessionManager.list() 的结果与可选的项目名（用于顶部展示）。
    def __init__(
        self,
        sessions: list[SessionMeta],
        project_name: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(id="resume-inline", **kwargs)
        self._sessions = sessions
        self._filtered = list(sessions)
        self._project = project_name
        self._cursor = 0
        self._search = ""

    def compose(self) -> ComposeResult:
        yield Static(self._build_content(), id="resume-content")

    def on_mount(self) -> None:
        self.focus()

    # 渲染整个视图：标题栏（含搜索框）+ 项目名 + 会话列表（最多 10 条）+ 操作提示。
    def _build_content(self) -> str:
        lines: list[str] = []
        total = len(self._sessions)
        showing = len(self._filtered)
        lines.append(f"[dim]Resume session ({showing} of {total})[/]\n")

        # 搜索框：固定 30 字符宽，含内容时显示输入，否则显示占位符。
        if self._search:
            lines.append(f"┌{'─' * 30}┐")
            lines.append(f"│⌕ {self._search:<28}│")
            lines.append(f"└{'─' * 30}┘")
        else:
            lines.append(f"┌{'─' * 30}┐")
            lines.append(f"│[dim]⌕ Search…{'':>20}[/]│")
            lines.append(f"└{'─' * 30}┘")

        if self._project:
            lines.append(f"\n  [dim]{self._project}[/]\n")

        # 会话列表：当前光标位置加粗高亮，附带相对时间。
        for i, meta in enumerate(self._filtered[:10]):
            title = meta.title or "(empty session)"
            if i == self._cursor:
                lines.append(f"[bold cyan]❯[/] [bold]{title}[/]")
            else:
                lines.append(f"  {title}")

            parts = [_relative_time(meta)]
            lines.append(f"  [dim]{'  ·  '.join(parts)}[/]")
            lines.append("")

        if showing > 10:
            lines.append(f"  [dim]↓ {showing - 10} more session(s)[/]")

        lines.append("[dim]Type to search · Enter to select · Esc to cancel[/]")
        return "\n".join(lines)

    # 重渲染整个视图。
    def _refresh(self) -> None:
        self.query_one("#resume-content", Static).update(self._build_content())

    # 按当前搜索词过滤会话：匹配 title 或 id（大小写不敏感）；空搜索恢复全量。
    def _refilter(self) -> None:
        if not self._search:
            self._filtered = list(self._sessions)
        else:
            s = self._search.lower()
            self._filtered = [
                m
                for m in self._sessions
                if s in (m.title or "").lower() or s in m.id.lower()
            ]
        # 过滤后重置光标到顶部，避免越界。
        self._cursor = 0
        self._refresh()

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh()

    def action_cursor_down(self) -> None:
        if self._cursor < min(len(self._filtered), 10) - 1:
            self._cursor += 1
            self._refresh()

    # 回车选择当前光标位置的会话；空列表时回传 None 让上层关闭视图。
    def action_select(self) -> None:
        if self._filtered and 0 <= self._cursor < len(self._filtered):
            self.post_message(self.Selected(self._filtered[self._cursor].id))
        else:
            self.post_message(self.Selected(None))

    def action_cancel(self) -> None:
        self.post_message(self.Selected(None))

    # 处理可打印字符与退格：累加到搜索词并重新过滤。
    # priority binding 已拦截 up/down/enter/escape，这里只处理剩余字符输入。
    def on_key(self, event: Key) -> None:
        key = event.key
        if key == "backspace":
            if self._search:
                self._search = self._search[:-1]
                self._refilter()
            event.stop()
        elif len(key) == 1 and key.isprintable():
            self._search += key
            self._refilter()
            event.stop()
