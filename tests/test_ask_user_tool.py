"""AskUserTool 单元测试：覆盖参数模型、execute 行为、超时与 _pending_event 清理。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from seacode.agents.tool_filter import ALL_AGENT_DISALLOWED_TOOLS
from seacode.tools.ask_user import (
    AskUserEvent,
    AskUserParams,
    AskUserTool,
    QuestionItem,
)
from seacode.tools.base import Tool, ToolCategory

# ---------------------------------------------------------------------------
# QuestionItem 与 AskUserParams
# ---------------------------------------------------------------------------


# 验证 QuestionItem text 类型默认 options 为空列表。
# 构造 text 类型问题，断言 options == []。
def test_question_item_text_type_defaults_empty_options() -> None:
    item = QuestionItem(type="text", name="q1", message="hi")
    assert item.type == "text"
    assert item.name == "q1"
    assert item.message == "hi"
    assert item.options == []


# 验证 QuestionItem radio 类型保留传入 options。
# 构造 radio 类型问题带 options，断言 options 列表保留。
def test_question_item_radio_type_keeps_options() -> None:
    item = QuestionItem(
        type="radio", name="q1", message="choose", options=["a", "b"]
    )
    assert item.type == "radio"
    assert item.options == ["a", "b"]


# 验证 QuestionItem select 类型保留传入 options。
# 构造 select 类型问题带 options，断言 options 列表保留。
def test_question_item_select_type_keeps_options() -> None:
    item = QuestionItem(
        type="select", name="q1", message="pick", options=["x", "y", "z"]
    )
    assert item.type == "select"
    assert item.options == ["x", "y", "z"]


# 验证 QuestionItem checkbox 类型保留传入 options。
# 构造 checkbox 类型问题带 options，断言 options 列表保留。
def test_question_item_checkbox_type_keeps_options() -> None:
    item = QuestionItem(
        type="checkbox", name="q1", message="multi", options=["m", "n"]
    )
    assert item.type == "checkbox"
    assert item.options == ["m", "n"]


# 验证 AskUserParams 可构造多问题列表。
# 构造含 2 个问题的 AskUserParams，断言 questions 长度为 2。
def test_ask_user_params_holds_multiple_questions() -> None:
    params = AskUserParams(
        questions=[
            QuestionItem(type="text", name="q1", message="hi"),
            QuestionItem(
                type="radio", name="q2", message="choose", options=["a", "b"]
            ),
        ]
    )
    assert len(params.questions) == 2
    assert params.questions[0].name == "q1"
    assert params.questions[1].name == "q2"


# 验证 AskUserParams 默认 questions 为空列表。
# 构造空 AskUserParams，断言 questions == []。
def test_ask_user_params_defaults_empty_questions() -> None:
    params = AskUserParams()
    assert params.questions == []


# ---------------------------------------------------------------------------
# AskUserTool 类属性与默认状态
# ---------------------------------------------------------------------------


# 验证 AskUserTool 类属性：name / category / is_system_tool / should_defer。
# 直接断言类属性值。
def test_ask_user_tool_class_attributes() -> None:
    assert AskUserTool.name == "AskUserQuestion"
    assert AskUserTool.category == ToolCategory.READ
    assert AskUserTool.is_system_tool is True
    assert AskUserTool.should_defer is True


# 验证 AskUserTool 实例化后 _pending_event 默认 None。
# 构造 AskUserTool，断言 _pending_event is None。
def test_ask_user_tool_default_pending_event_none() -> None:
    tool = AskUserTool()
    assert tool._pending_event is None


# 验证 AskUserTool 继承 Tool 基类。
# isinstance 断言 AskUserTool 实例是 Tool 子类。
def test_ask_user_tool_is_tool_subclass() -> None:
    tool = AskUserTool()
    assert isinstance(tool, Tool)


# ---------------------------------------------------------------------------
# execute 行为
# ---------------------------------------------------------------------------


# 验证 execute 创建 Future 并设置 _pending_event。
# 启动 execute 任务后立即检查 _pending_event 非 None 且持 future。
async def test_execute_sets_pending_event_with_future() -> None:
    tool = AskUserTool()
    params = AskUserParams(
        questions=[QuestionItem(type="text", name="q1", message="hi")]
    )

    # 启动 execute 但不 await，让它阻塞在 wait_for 上。
    task = asyncio.create_task(tool.execute(params))
    # 让事件循环调度让 execute 进入 wait_for。
    await asyncio.sleep(0.01)

    assert tool._pending_event is not None
    assert isinstance(tool._pending_event, AskUserEvent)
    assert tool._pending_event.future is not None
    assert not tool._pending_event.future.done()
    # questions 序列化为 dict 列表。
    assert len(tool._pending_event.questions) == 1
    assert tool._pending_event.questions[0]["name"] == "q1"

    # 清理：回填 future 让 task 完成。
    tool._pending_event.future.set_result({"q1": "answer"})
    await task


# 验证 execute future.set_result 后返回 "name: answer" 格式。
# set_result({"q1": "answer1"})，断言 content == "q1: answer1"。
async def test_execute_returns_name_answer_format() -> None:
    tool = AskUserTool()
    params = AskUserParams(
        questions=[QuestionItem(type="text", name="q1", message="hi")]
    )

    # 先启动 execute，等 _pending_event 设置后回填 future。
    task = asyncio.create_task(tool.execute(params))
    await asyncio.sleep(0.01)
    assert tool._pending_event is not None
    tool._pending_event.future.set_result({"q1": "answer1"})

    result = await task
    assert result.is_error is False
    assert result.content == "q1: answer1"


# 验证 execute 多问题返回多行 "name: answer"。
# set_result 含两个键值对，断言 content 含两行。
async def test_execute_multiple_questions_returns_multiple_lines() -> None:
    tool = AskUserTool()
    params = AskUserParams(
        questions=[
            QuestionItem(type="text", name="q1", message="hi"),
            QuestionItem(type="text", name="q2", message="hi2"),
        ]
    )

    task = asyncio.create_task(tool.execute(params))
    await asyncio.sleep(0.01)
    assert tool._pending_event is not None
    tool._pending_event.future.set_result(
        {"q1": "a1", "q2": "a2"}
    )

    result = await task
    assert "q1: a1" in result.content
    assert "q2: a2" in result.content


# 验证 execute 5 分钟超时返回 is_error=True。
# monkeypatch asyncio.wait_for 抛 TimeoutError，断言 is_error=True 且 content 含 "5 minutes"。
async def test_execute_timeout_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = AskUserTool()

    async def _raise_timeout(
        awaitable: Any, timeout: float
    ) -> Any:
        del awaitable, timeout
        raise TimeoutError()

    # wait_for 是 asyncio 模块的函数，monkeypatch 替换。
    monkeypatch.setattr(asyncio, "wait_for", _raise_timeout)
    params = AskUserParams(
        questions=[QuestionItem(type="text", name="q1", message="hi")]
    )

    result = await tool.execute(params)

    assert result.is_error is True
    assert "5 minutes" in result.content


# 验证 execute 无论成功或超时 finally 清空 _pending_event。
# 超时路径完成后断言 _pending_event is None。
async def test_execute_finally_clears_pending_event_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = AskUserTool()

    async def _raise_timeout(
        awaitable: Any, timeout: float
    ) -> Any:
        del awaitable, timeout
        raise TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _raise_timeout)
    params = AskUserParams(
        questions=[QuestionItem(type="text", name="q1", message="hi")]
    )

    await tool.execute(params)

    assert tool._pending_event is None


# 验证 execute 成功路径 finally 清空 _pending_event。
# set_result 后 await 完成，断言 _pending_event is None。
async def test_execute_finally_clears_pending_event_on_success() -> None:
    tool = AskUserTool()
    params = AskUserParams(
        questions=[QuestionItem(type="text", name="q1", message="hi")]
    )

    task = asyncio.create_task(tool.execute(params))
    await asyncio.sleep(0.01)
    assert tool._pending_event is not None
    tool._pending_event.future.set_result({"q1": "a1"})

    await task
    assert tool._pending_event is None


# 验证 execute questions 序列化为 dict 列表含 type/name/message/options。
# 检查 _pending_event.questions 结构。
async def test_execute_serializes_questions_to_dicts() -> None:
    tool = AskUserTool()
    params = AskUserParams(
        questions=[
            QuestionItem(
                type="radio", name="q1", message="choose", options=["a", "b"]
            )
        ]
    )

    task = asyncio.create_task(tool.execute(params))
    await asyncio.sleep(0.01)
    assert tool._pending_event is not None
    q = tool._pending_event.questions[0]
    assert q["type"] == "radio"
    assert q["name"] == "q1"
    assert q["message"] == "choose"
    assert q["options"] == ["a", "b"]

    tool._pending_event.future.set_result({"q1": "a"})
    await task


# ---------------------------------------------------------------------------
# ALL_AGENT_DISALLOWED_TOOLS 集成
# ---------------------------------------------------------------------------


# 验证 AskUserQuestion 在 ALL_AGENT_DISALLOWED_TOOLS 中。
# 直接断言集合含 "AskUserQuestion"。
def test_ask_user_question_in_disallowed_tools() -> None:
    assert "AskUserQuestion" in ALL_AGENT_DISALLOWED_TOOLS


# ---------------------------------------------------------------------------
# AskUserEvent 数据类
# ---------------------------------------------------------------------------


# 验证 AskUserEvent 持 questions 与 future 字段。
# 构造 AskUserEvent，断言字段持有传入值。
async def test_ask_user_event_holds_fields() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
    questions = [{"type": "text", "name": "q1", "message": "hi", "options": []}]
    event = AskUserEvent(questions=questions, future=future)

    assert event.questions == questions
    assert event.future is future
