"""内联权限确认对话框组件：渲染在聊天区，三选项 + 键盘导航。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from seacode.agent import PermissionResponse

# 三选项：标签 + 对应的 PermissionResponse 枚举值。
_PERM_OPTIONS: list[tuple[str, PermissionResponse]] = [
    ("Yes", PermissionResponse.ALLOW),
    ("Yes, and don't ask again for this pattern", PermissionResponse.ALLOW_ALWAYS),
    ("No", PermissionResponse.DENY),
]


class InlinePermissionWidget(Vertical, can_focus=True):
    """渲染在聊天区域内部的内联权限确认提示。

    工具名 + 描述 + 带编号的选项，支持方向键导航 + 回车确认 + Esc 拒绝。
    挂载时自动聚焦；任何退出路径（含 Esc）都通过 Responded 消息 resolve future。
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "deny", "Deny", priority=True),
    ]

    class Responded(Message):
        """用户对权限请求的回复；由 app.py 的处理器 resolve future。"""

        def __init__(self, response: PermissionResponse) -> None:
            super().__init__()
            self.response = response

    def __init__(self, tool_name: str, description: str, **kwargs: object) -> None:
        super().__init__(id="perm-inline", **kwargs)  # type: ignore[arg-type]
        self._tool_name = tool_name
        self._description = description
        self._cursor = 0

    # 渲染工具名、描述、确认提示与三选项；当前光标项用 ❯ 标记。
    def compose(self) -> ComposeResult:
        yield Static(self._build_content(), id="perm-content")

    # 挂载后自动聚焦，便于直接键盘操作。
    def on_mount(self) -> None:
        self.focus()

    # 构造展示文本：工具名 + 描述 + 确认提示 + 三选项。
    def _build_content(self) -> str:
        lines: list[str] = []
        lines.append(f"\n  [bold yellow]{self._tool_name} command[/bold yellow]\n")
        lines.append(f"    {self._description}\n")
        lines.append("  [dim]This command requires approval[/dim]\n")
        lines.append("  Do you want to proceed?\n")

        for i, (label, _resp) in enumerate(_PERM_OPTIONS):
            if i == self._cursor:
                lines.append(f" [bold cyan]❯[/bold cyan] {i + 1}. [bold]{label}[/bold]")
            else:
                lines.append(f"   {i + 1}. [dim]{label}[/dim]")

        return "\n".join(lines)

    # 局部刷新内容，避免重新挂载组件。
    def _refresh(self) -> None:
        content = self.query_one("#perm-content", Static)
        content.update(self._build_content())

    # 光标上移；到达顶部不循环。
    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh()

    # 光标下移；到达底部不循环。
    def action_cursor_down(self) -> None:
        if self._cursor < len(_PERM_OPTIONS) - 1:
            self._cursor += 1
            self._refresh()

    # 回车确认当前光标项，发出 Responded 消息。
    def action_select(self) -> None:
        _, response = _PERM_OPTIONS[self._cursor]
        self.post_message(self.Responded(response))

    # Esc 直接拒绝，发出 Responded(DENY) 消息。
    def action_deny(self) -> None:
        self.post_message(self.Responded(PermissionResponse.DENY))
