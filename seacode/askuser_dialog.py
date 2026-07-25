"""InlineAskUserWidget：内联 AskUser 表单组件，支持多问题键盘导航。

渲染 ``AskUserTool._pending_event.questions`` 列表；用户提交后通过 ``Responded``
事件携带 ``answers: dict[str, str] | None``，由 ``app.py`` 回填 ``Future.set_result``、
移除组件、恢复 ``#chat-input`` 焦点。

交互模型与原版 TUI 对齐：带 ☐/☑ 勾选标记的导航栏、上下光标导航、
多选切换（multiSelect）、"Other" 自定义输入、复核/提交视图。
text 问题无 options，自动落到 "Other" 输入框让用户自由键入。
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static


class InlineAskUserWidget(Vertical, can_focus=True):
    """内联的 AskUser 组件，支持多问题之间的 Tab 切换导航。

    基类为 ``Vertical`` 且 ``can_focus=True``，让键盘绑定优先生效；
    内部用单个 ``Static`` 渲染当前问题或复核视图，通过 ``_refresh`` 刷新。
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("tab", "next_q", "Next", priority=True),
        Binding("shift+tab", "prev_q", "Prev", priority=True),
        # 用 toggle_option 而非 toggle，避免与 DOMNode.action_toggle(attribute_name) 签名冲突。
        Binding("space", "toggle_option", "Toggle", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    class Responded(Message):
        """用户提交答案事件；answers 为 None 表示取消，dict 为提交的答案。"""

        def __init__(self, answers: dict[str, str] | None) -> None:
            self.answers = answers
            super().__init__()

    def __init__(
        self,
        questions: list[dict[str, Any]],
    ) -> None:
        super().__init__(id="askuser-inline")
        self._questions = questions
        self._q_idx = 0
        n = len(questions)
        # 每个问题的光标位置（指向 options 或末尾的 "Other"）。
        self._cursors: list[int] = [0] * n
        # 多选问题已勾选的 option 索引集合。
        self._selected: list[dict[int, bool]] = [{} for _ in range(n)]
        # 每个问题 "Other" 输入框的当前文本。
        self._others: list[str] = [""] * n
        # 每个问题已确认的答案。
        self._answered: dict[int, str] = {}
        # 是否进入复核/提交视图。
        self._on_submit = False
        # 复核视图中的光标：0=Submit answers，1=Cancel。
        self._submit_idx = 0

    # 渲染表单：单 Static 承载当前内容（问题或复核视图）。
    # id="askuser-content" 供 app.py 定位父组件并整体移除。
    def compose(self) -> ComposeResult:
        yield Static(self._build_content(), id="askuser-content")

    def on_mount(self) -> None:
        self.focus()

    # 当前问题可选项数 = options 数 + 1（"Other" 占位）。
    def _option_count(self, q_idx: int) -> int:
        return len(self._questions[q_idx].get("options", [])) + 1

    # 构建当前应渲染的内容：复核视图或当前问题。
    def _build_content(self) -> str:
        if self._on_submit:
            return self._render_submit()
        return self._render_question()

    # 渲染当前问题：导航栏（多问题时）+ 标题 + 选项列表 + Other 输入框。
    def _render_question(self) -> str:
        lines: list[str] = []
        multi = len(self._questions) > 1

        if multi:
            lines.append(self._render_nav_bar())
            lines.append("")

        q = self._questions[self._q_idx]
        # 用 question 作标题；兼容 message 字段。
        header = q.get("question", q.get("message", f"Question {self._q_idx + 1}"))
        # 用 magenta 命名色：textual markup 不支持 color(99) 256 色语法。
        lines.append(f" [bold magenta]{header}[/]\n")

        options = q.get("options", []) or []
        # 多选仅看 multiSelect 字段。
        is_multi = q.get("multiSelect", False)
        cursor = self._cursors[self._q_idx]

        for i, opt in enumerate(options):
            label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
            desc = opt.get("description", "") if isinstance(opt, dict) else ""

            prefix = " ❯ " if i == cursor else "   "
            bold = "[bold]" if i == cursor else ""
            end_bold = "[/]" if i == cursor else ""

            if is_multi:
                check = "● " if self._selected[self._q_idx].get(i) else "○ "
            else:
                check = ""

            desc_part = f" — [dim]{desc}[/]" if desc else ""
            lines.append(f"{prefix}{check}{bold}{label}{end_bold}{desc_part}")

        # "Other" 选项（自定义输入）。
        other_idx = len(options)
        prefix = " ❯ " if cursor == other_idx else "   "
        bold = "[bold]" if cursor == other_idx else ""
        end_bold = "[/]" if cursor == other_idx else ""
        lines.append(f"{prefix}{bold}Other{end_bold}")

        if cursor == other_idx:
            text = self._others[self._q_idx]
            display = text if text else "[dim]Type your answer here...[/]"
            lines.append(f"      {display}█")

        if is_multi:
            lines.append("\n      [dim]space to toggle, enter to confirm[/]")
        else:
            lines.append("\n      [dim]enter to confirm[/]")

        return "\n".join(lines)

    # 渲染顶部导航栏：每个问题一个 ☐/☑ 标签 + Submit 项 + 左右箭头。
    def _render_nav_bar(self) -> str:
        parts: list[str] = []
        for i, q in enumerate(self._questions):
            header = q.get("header", f"Q{i + 1}")
            check = "☑" if i in self._answered else "☐"
            if i == self._q_idx and not self._on_submit:
                parts.append(f"[bold reverse] {header} {check} [/]")
            else:
                parts.append(f" {header} {check} ")
        submit_part = (
            "[bold reverse] ✓ Submit [/]" if self._on_submit else " ✓ Submit "
        )
        parts.append(submit_part)
        left = "[bold]←[/]" if self._q_idx > 0 else "[dim]←[/]"
        right = "[bold]→[/]"
        return f" {left} {'|'.join(parts)} {right}"

    # 渲染复核/提交视图：列出各问题答案 + Submit answers / Cancel 选项。
    def _render_submit(self) -> str:
        lines = ["\n [bold magenta]Review your answers:[/]\n"]
        for i, q in enumerate(self._questions):
            header = q.get("header", q.get("question", f"Q{i + 1}"))
            ans = self._answered.get(i, "")
            if ans:
                lines.append(f"   {header}: {ans}")
            else:
                lines.append(f"   {header}: [dim](not answered)[/]")
        lines.append("")
        for j, label in enumerate(["Submit answers", "Cancel"]):
            if j == self._submit_idx:
                lines.append(f" [bold cyan]❯[/] [bold]{label}[/]")
            else:
                lines.append(f"   [dim]{label}[/]")
        return "\n".join(lines)

    # 刷新渲染内容到 #askuser-content Static。
    def _refresh(self) -> None:
        self.query_one("#askuser-content", Static).update(self._build_content())

    # 把当前问题的当前光标位置答案写入 _answered。
    def _save_current_answer(self) -> None:
        q = self._questions[self._q_idx]
        options = q.get("options", []) or []
        cursor = self._cursors[self._q_idx]
        is_multi = q.get("multiSelect", False)

        if cursor == len(options):  # "Other"（自定义输入）
            self._answered[self._q_idx] = self._others[self._q_idx] or "Other"
        elif is_multi:
            selected = [
                (opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt))
                for i, opt in enumerate(options)
                if self._selected[self._q_idx].get(i)
            ]
            if not selected:
                opt = options[cursor]
                selected = [
                    opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                ]
            self._answered[self._q_idx] = ", ".join(selected)
        else:
            opt = options[cursor]
            self._answered[self._q_idx] = (
                opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
            )

    # 上移光标；复核视图切换 Submit/Cancel。
    def action_cursor_up(self) -> None:
        if self._on_submit:
            if self._submit_idx > 0:
                self._submit_idx -= 1
                self._refresh()
        else:
            if self._cursors[self._q_idx] > 0:
                self._cursors[self._q_idx] -= 1
                self._refresh()

    # 下移光标；复核视图切换 Submit/Cancel。
    def action_cursor_down(self) -> None:
        if self._on_submit:
            if self._submit_idx < 1:
                self._submit_idx += 1
                self._refresh()
        else:
            max_c = self._option_count(self._q_idx) - 1
            if self._cursors[self._q_idx] < max_c:
                self._cursors[self._q_idx] += 1
                self._refresh()

    # Tab 切换到下一题；末题再按 Tab 进入复核视图。
    def action_next_q(self) -> None:
        if self._on_submit or len(self._questions) <= 1:
            return
        if self._q_idx < len(self._questions) - 1:
            self._q_idx += 1
        else:
            self._on_submit = True
            self._submit_idx = 0
        self._refresh()

    # Shift+Tab 回到上一题；复核视图按 Shift+Tab 回到末题。
    def action_prev_q(self) -> None:
        if self._on_submit:
            self._on_submit = False
            self._q_idx = len(self._questions) - 1
            self._refresh()
        elif self._q_idx > 0:
            self._q_idx -= 1
            self._refresh()

    # 空格切换多选项的勾选状态；非多选或复核视图忽略。
    # 方法名用 toggle_option 避免覆盖 DOMNode.action_toggle(attribute_name: str)。
    def action_toggle_option(self) -> None:
        if self._on_submit:
            return
        q = self._questions[self._q_idx]
        if not q.get("multiSelect", False):
            return
        cursor = self._cursors[self._q_idx]
        options = q.get("options", []) or []
        if cursor < len(options):
            self._selected[self._q_idx][cursor] = not self._selected[
                self._q_idx
            ].get(cursor, False)
            self._refresh()

    # Enter 确认：问题视图保存答案并前进；复核视图提交或取消。
    # 提交/取消均 post Responded，由 app.py 回填 future 并清理 UI。
    def action_select(self) -> None:
        if self._on_submit:
            if self._submit_idx == 0:
                answers = self._collect_answers()
                self.post_message(self.Responded(answers))
            else:
                self.post_message(self.Responded(None))
        else:
            self._save_current_answer()
            if len(self._questions) == 1:
                answers = self._collect_answers()
                self.post_message(self.Responded(answers))
            elif self._q_idx < len(self._questions) - 1:
                self._q_idx += 1
                self._refresh()
            else:
                self._on_submit = True
                self._submit_idx = 0
                self._refresh()

    # ESC 取消：post Responded(None) 让 app.py 回填空 dict 并清理 UI。
    def action_cancel(self) -> None:
        self.post_message(self.Responded(None))

    # 当光标停在 "Other" 输入框时，捕获可打印按键与 backspace 实现内联输入。
    def on_key(self, event: Any) -> None:
        if self._on_submit:
            return
        cursor = self._cursors[self._q_idx]
        options = self._questions[self._q_idx].get("options", []) or []
        if cursor != len(options):  # 当前光标不在 "Other" 上
            return
        key = event.key
        if key == "backspace":
            if self._others[self._q_idx]:
                self._others[self._q_idx] = self._others[self._q_idx][:-1]
                self._refresh()
            event.stop()
        elif len(key) == 1 and key.isprintable():
            self._others[self._q_idx] += key
            self._refresh()
            event.stop()

    # 收集所有问题答案：key 取 question，fallback 到 message 与 q{i}。
    def _collect_answers(self) -> dict[str, str]:
        answers: dict[str, str] = {}
        for i, q in enumerate(self._questions):
            key = q.get("question", q.get("message", f"q{i}"))
            answers[key] = self._answered.get(i, "")
        return answers
