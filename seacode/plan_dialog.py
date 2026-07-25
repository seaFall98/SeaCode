"""Plan 模式审批对话框：内联展示计划选项并收集用户审批决策。"""

from __future__ import annotations

from enum import StrEnum

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static


class PlanChoice(StrEnum):
    """用户对计划的三种审批决策。"""

    YOLO = "yolo"
    MANUAL = "manual"
    FEEDBACK = "feedback"


# 三个审批选项及对应枚举值；顺序即光标遍历顺序。
_OPTIONS: list[tuple[str, PlanChoice]] = [
    ("Yes, enter YOLO mode (auto-approve all)", PlanChoice.YOLO),
    ("Yes, manually approve edits", PlanChoice.MANUAL),
    ("Tell SeaCode what to change", PlanChoice.FEEDBACK),
]


class InlinePlanWidget(Vertical, can_focus=True):
    """内联计划审批组件：上下移动光标、回车确认、Esc 取消。

    第三项（反馈）选中时进入文本输入态，可用 shift+tab 提交反馈内容；
    组件通过 Responded 消息把决策回传给上层 App。
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("shift+tab", "approve_with_feedback", "Approve+Feedback", priority=True),
    ]

    class Responded(Message):
        """审批决策事件；choice 为决策枚举，feedback 携带可选反馈文本。"""

        def __init__(self, choice: PlanChoice, feedback: str = "") -> None:
            super().__init__()
            self.choice = choice
            self.feedback = feedback

    def __init__(self, **kwargs: object) -> None:
        super().__init__(id="plan-inline", **kwargs)  # type: ignore[arg-type]
        self._cursor = 0
        self._input = ""

    def compose(self) -> ComposeResult:
        yield Static(self._build_content(), id="plan-content")

    def on_mount(self) -> None:
        self.focus()

    # 拼装审批面板内容：标题 + 选项列表 + 反馈输入区（仅第三项选中时）。
    def _build_content(self) -> str:
        lines = [
            "\n [bold #875fff]SeaCode has written up a plan and is ready to execute. "
            "Would you like to proceed?[/bold #875fff]\n"
        ]
        for i, (label, _choice) in enumerate(_OPTIONS):
            if i == self._cursor:
                lines.append(f" [bold cyan]❯[/bold cyan] {i + 1}. [bold]{label}[/bold]")
            else:
                lines.append(f"   {i + 1}. [dim]{label}[/dim]")

        # 第三项（反馈）选中时展示输入框与提交提示。
        if self._cursor == 2:
            display = self._input if self._input else "[dim]Type feedback here...[/dim]"
            lines.append(f"      {display}█")
            lines.append("      [dim]shift+tab to approve with this feedback[/dim]")

        return "\n".join(lines)

    # 刷新展示内容；避免重建整个组件。
    def _refresh(self) -> None:
        self.query_one("#plan-content", Static).update(self._build_content())

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh()

    def action_cursor_down(self) -> None:
        if self._cursor < 2:
            self._cursor += 1
            self._refresh()

    def action_select(self) -> None:
        # 第三项需要有反馈文本才提交；前两项直接提交对应决策。
        if self._cursor == 2 and self._input:
            self.post_message(self.Responded(PlanChoice.FEEDBACK, self._input))
        elif self._cursor == 0:
            self.post_message(self.Responded(PlanChoice.YOLO))
        elif self._cursor == 1:
            self.post_message(self.Responded(PlanChoice.MANUAL))

    def action_cancel(self) -> None:
        # 取消视为手动模式，避免误进 YOLO。
        self.post_message(self.Responded(PlanChoice.MANUAL))

    def action_approve_with_feedback(self) -> None:
        if self._cursor == 2 and self._input:
            self.post_message(self.Responded(PlanChoice.FEEDBACK, self._input))

    # 反馈输入态下捕获可打印字符与退格；其他状态不拦截按键。
    def on_key(self, event: object) -> None:
        if self._cursor != 2:
            return
        key = getattr(event, "key", "")
        if key == "backspace":
            if self._input:
                self._input = self._input[:-1]
                self._refresh()
            self._stop_event(event)
        elif len(key) == 1 and key.isprintable():
            self._input += key
            self._refresh()
            self._stop_event(event)

    # 统一调用 event.stop()，兼容 Textual 事件对象签名。
    def _stop_event(self, event: object) -> None:
        stop = getattr(event, "stop", None)
        if callable(stop):
            stop()
