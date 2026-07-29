"""会话恢复列表测试：覆盖全量候选导航、窗口跟随和过滤边界。"""

from __future__ import annotations

import pytest

from seacode.memory.session import SessionMeta
from seacode.session_dialog import InlineResumeWidget


def _make_sessions(count: int) -> list[SessionMeta]:
    return [
        SessionMeta(id=f"session-{index}", title=f"Session {index}")
        for index in range(count)
    ]


def _make_widget(
    sessions: list[SessionMeta], monkeypatch: pytest.MonkeyPatch
) -> InlineResumeWidget:
    widget = InlineResumeWidget(sessions)
    monkeypatch.setattr(widget, "_refresh", lambda: None)
    return widget


# 验证恢复列表可移动到可见窗口之外的候选，并让当前候选进入渲染窗口。
# 构造 12 条会话连续向下移动，断言最后一条可见且可以被 Enter 选择。
def test_resume_widget_navigates_all_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _make_sessions(12)
    widget = _make_widget(sessions, monkeypatch)
    selected: list[str | None] = []
    monkeypatch.setattr(
        widget,
        "post_message",
        lambda event: selected.append(event.session_id),
    )

    for _ in range(len(sessions) - 1):
        widget.action_cursor_down()

    assert widget._cursor == len(sessions) - 1
    rendered = widget._build_content()
    assert "Session 11" in rendered
    assert "Session 0" not in rendered
    assert "more session(s)" in rendered

    widget.action_select()
    assert selected == ["session-11"]


# 验证向上移动可以从末尾窗口回到首个候选，且过滤会重置导航位置。
# 先移动到末尾再回到开头，随后过滤到单条结果并断言游标重新归零。
def test_resume_widget_moves_back_and_refilters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _make_sessions(12)
    widget = _make_widget(sessions, monkeypatch)

    for _ in range(len(sessions) - 1):
        widget.action_cursor_down()
    for _ in range(len(sessions) - 1):
        widget.action_cursor_up()

    assert widget._cursor == 0
    assert "Session 0" in widget._build_content()

    widget._search = "Session 11"
    widget._refilter()

    assert widget._cursor == 0
    assert len(widget._filtered) == 1
    assert "Session 11" in widget._build_content()


# 验证恢复组件遇到空列表时仍能渲染操作提示，不访问不存在的候选。
# 直接构造空 widget 并调用渲染与选择路径，断言不抛异常且回传取消事件。
def test_resume_widget_handles_empty_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = _make_widget([], monkeypatch)
    selected: list[str | None] = []
    monkeypatch.setattr(
        widget,
        "post_message",
        lambda event: selected.append(event.session_id),
    )

    rendered = widget._build_content()
    widget.action_cursor_down()
    widget.action_select()

    assert "Resume session (0 of 0)" in rendered
    assert selected == [None]
