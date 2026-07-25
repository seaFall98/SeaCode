"""InlineAskUserWidget：text/radio/select/checkbox 表单组件。

渲染 ``AskUserTool._pending_event.questions`` 列表；用户提交后通过 ``Responded``
事件携带 ``answers: dict[str, str]``，回填 ``Future.set_result(answers)``、
移除组件、恢复 ``#chat-input`` 焦点。

本组件保持极简：text 用 TextArea；radio / select / checkbox 用 OptionList。
不引入复杂样式，与既有 InlinePermissionWidget 同层级。
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option


class InlineAskUserWidget(Static):
    """内联提问表单；提交后回填 future 并移除自身。"""

    class Responded(Message):
        """用户提交答案事件；answers 是 name → answer 映射。"""

        def __init__(self, answers: dict[str, str]) -> None:
            self.answers = answers
            super().__init__()

    def __init__(
        self,
        questions: list[dict[str, Any]],
        future: asyncio.Future[dict[str, str]],
    ) -> None:
        super().__init__()
        self._questions = questions
        self._future = future
        self._inputs: dict[str, Any] = {}

    # 渲染表单：每个问题按 type 选择对应控件。
    def compose(self) -> ComposeResult:
        with Vertical(id="askuser-form"):
            for q in self._questions:
                name = q.get("name", "")
                message = q.get("message", "")
                qtype = q.get("type", "text")
                options = q.get("options", []) or []

                yield Static(f"{message}", classes="askuser-label")
                if qtype == "text":
                    ta = TextArea(id=f"q-{name}", classes="askuser-text")
                    self._inputs[name] = ("text", ta)
                    yield ta
                else:
                    # radio / select / checkbox 都用 OptionList；提交时按类型收集。
                    # Option 的 prompt 即为展示文本；用索引在 _options_map 中取回原值。
                    opts = [Option(opt) for opt in options]
                    ol = OptionList(*opts, id=f"q-{name}", classes="askuser-option")
                    self._inputs[name] = (qtype, ol)
                    yield ol
        yield Button("Submit", id="askuser-submit", classes="askuser-submit")

    # 收集表单输入并提交。
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "askuser-submit":
            return
        answers: dict[str, str] = {}
        for name, (qtype, widget) in self._inputs.items():
            if qtype == "text":
                answers[name] = widget.text.strip()
            elif qtype == "checkbox":
                # OptionList 多选；取所有 highlighted 的 prompt 文本。
                selected = [
                    str(widget.get_option_at_index(i).prompt)
                    for i in widget.highlighted
                ] if hasattr(widget, "highlighted") and isinstance(
                    widget.highlighted, set
                ) else []
                answers[name] = ", ".join(str(s) for s in selected)
            else:
                # radio / select 单选；取当前 highlighted 的 prompt 文本。
                idx = widget.highlighted
                if isinstance(idx, int) and idx >= 0:
                    answers[name] = str(widget.get_option_at_index(idx).prompt)
                else:
                    answers[name] = ""

        # 回填 future 让 AgentTool.execute 继续。
        if not self._future.done():
            self._future.set_result(answers)
        # 通过事件让 app.py 处理组件移除与焦点恢复。
        self.post_message(self.Responded(answers=answers))
