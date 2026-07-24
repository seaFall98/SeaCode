from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from pydantic import BaseModel
from textual.containers import Horizontal
from textual.widgets import Button, OptionList, Static

from seacode.app import ChatInput, SeaCodeApp, ToolGroupSummary
from seacode.client import (
    LLMClient,
    NetworkError,
    StreamComplete,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallStart,
)
from seacode.config import ProviderConfig
from seacode.conversation import Message
from seacode.tools.base import Tool, ToolCategory, ToolResult


# 提供可按回合返回事件或抛出错误的本地假客户端。
class _FakeClient(LLMClient):
    # 保存每个测试回合的预设结果。
    def __init__(self, outcomes: list[list[StreamEvent] | Exception | _PartialFailure]) -> None:
        self._outcomes = outcomes
        self.requests: list[tuple[Message, ...]] = []

    # 记录请求历史并交付预设事件，不连接真实 Provider。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system, tools
        self.requests.append(tuple(messages))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, _PartialFailure):
            for event in outcome.events:
                yield event
            raise outcome.error
        for event in outcome:
            yield event


# 表示已产生部分流事件后才失败的 Provider 回合。
class _PartialFailure:
    # 保存失败前事件和随后抛出的错误。
    def __init__(self, events: list[StreamEvent], error: Exception) -> None:
        self.events = events
        self.error = error


# 模拟在首字节前持续等待的 Provider 流。
class _BlockingClient(LLMClient):
    # 初始化用于控制等待状态的异步信号。
    def __init__(self) -> None:
        self.requests: list[tuple[Message, ...]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    # 在测试释放前保持回合活动，用于验证输入锁定。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system, tools
        self.requests.append(tuple(messages))
        self.started.set()
        await self.release.wait()
        yield TextDelta("Done")
        yield StreamComplete()


# 创建用于 TUI 交互测试的无密钥 Provider 配置。
def _provider(name: str = "test") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol="openai-compat",
        model="test-model",
        base_url="https://api.example.test",
        api_key="test-key",
    )


# 等待异步 Textual 事件处理器完成当前回合。
async def _settle(pilot: Any) -> None:
    await pilot.pause(0.05)
    await pilot.pause(0.05)


# 验证单 Provider 进入既定 TUI 壳层，Enter 提交且没有主要 Send 按钮。
# 假流同时证明三行标题、横向状态栏、唯一模型位置和流后输入恢复。
@pytest.mark.asyncio
async def test_single_profile_streams_with_enter_and_has_no_send_button() -> None:
    client = _FakeClient(
        [
            [
                ThinkingDelta("First thought. "),
                ThinkingDelta("Second thought."),
                TextDelta("Hello"),
                StreamComplete(input_tokens=2, output_tokens=1),
            ]
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        assert input_widget.disabled is False
        assert not app.query(Button)
        title = app.query_one("#title-bar", Static)
        assert str(title.styles.height) == "3"
        assert str(title.render()).count("\n") == 2
        assert "SeaCode" in str(title.render())
        assert os.getcwd() in str(title.render())
        assert isinstance(app.query_one("#status-bar"), Horizontal)
        assert "test-model" in str(app.query_one("#model-label", Static).render())
        assert not app.query("#mode-label")
        assert not app.query("#teammates-label")
        input_widget.load_text("Hi")
        await pilot.press("enter")
        await _settle(pilot)

        assert input_widget.disabled is False
        assert client.requests == [(Message(role="user", content="Hi"),)]
        assert "Ready" in str(app.query_one("#turn-status").render())
        assert "First thought. Second thought." in str(
            app.query_one(".thinking-message").render()
        )


# 验证 Shift+Enter 在输入框插入换行而不会提前发送。
# 随后 Enter 应发送保留换行的同一条完整文本。
@pytest.mark.asyncio
async def test_shift_enter_inserts_newline_before_submit() -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Line one")
        input_widget.cursor_location = (0, len("Line one"))
        await pilot.press("shift+enter")
        input_widget.insert("Line two")

        assert "\n" in input_widget.text
        assert client.requests == []

        await pilot.press("enter")
        await _settle(pilot)

        assert client.requests == [
            (Message(role="user", content="Line one\nLine two"),)
        ]


# 验证流失败后不完整回答不会进入下一次模型请求历史。
# 第二次成功回合应可发送，并只携带新的用户输入。
@pytest.mark.asyncio
async def test_stream_error_recovers_without_polluting_conversation_history() -> None:
    client = _FakeClient(
        [
            _PartialFailure([TextDelta("Partial answer")], NetworkError("network failure")),
            [TextDelta("Recovered"), StreamComplete(input_tokens=1, output_tokens=1)],
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("First")
        await pilot.press("enter")
        await _settle(pilot)

        assert input_widget.disabled is False
        input_widget.load_text("Second")
        await pilot.press("enter")
        await _settle(pilot)

        assert client.requests == [
            (Message(role="user", content="First"),),
            (Message(role="user", content="Second"),),
        ]
        assert "Ready" in str(app.query_one("#turn-status").render())


# 验证等待首字节期间输入被锁定，不能建立第二个并发回合。
# 阻塞流释放后恢复输入，证明状态机不会永久卡住。
@pytest.mark.asyncio
async def test_waiting_turn_prevents_duplicate_submission() -> None:
    client = _BlockingClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("First")
        await pilot.press("enter")
        await asyncio.wait_for(client.started.wait(), timeout=1)

        assert input_widget.disabled is True
        input_widget.load_text("Second")
        await pilot.press("enter")
        await pilot.pause()
        assert client.requests == [(Message(role="user", content="First"),)]

        client.release.set()
        await _settle(pilot)
        assert input_widget.disabled is False


# 验证多个配置先显示键盘可操作的选择控件。
# 对话和输入区域在选择前保持隐藏，避免误用未选定的模型。
@pytest.mark.asyncio
async def test_multiple_profiles_begin_with_keyboard_selection() -> None:
    client = _FakeClient([])
    app = SeaCodeApp(
        [_provider("first"), _provider("second")],
        client_factory=lambda _: client,
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.query_one(OptionList).display is True
        assert app.query_one("#chat-area").display is False
        assert app.query_one("#input-area").display is False


# 验证连续成功和失败回合后的请求历史只包含完整回合。
# 顺序覆盖两轮成功、一轮失败与下一轮成功的完整用户路径。
@pytest.mark.asyncio
async def test_end_to_end_history_survives_failure_and_continues() -> None:
    client = _FakeClient(
        [
            [TextDelta("Answer one"), StreamComplete()],
            [TextDelta("Answer two"), StreamComplete()],
            NetworkError("network failure"),
            [TextDelta("Answer four"), StreamComplete()],
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        for message in ("One", "Two", "Three", "Four"):
            input_widget.load_text(message)
            await pilot.press("enter")
            await _settle(pilot)

    assert client.requests == [
        (Message("user", "One"),),
        (
            Message("user", "One"),
            Message("assistant", "Answer one"),
            Message("user", "Two"),
        ),
        (
            Message("user", "One"),
            Message("assistant", "Answer one"),
            Message("user", "Two"),
            Message("assistant", "Answer two"),
            Message("user", "Three"),
        ),
        (
            Message("user", "One"),
            Message("assistant", "Answer one"),
            Message("user", "Two"),
            Message("assistant", "Answer two"),
            Message("user", "Four"),
        ),
    ]


# ---------------------------------------------------------------------------
# batch03：ToolGroupSummary、Esc 取消、thinking-done
# ---------------------------------------------------------------------------


# 在首字节后阻塞的 Provider 流，用于验证 Esc 取消时已累积文本的保留。
class _PartialBlockingClient(LLMClient):
    # 保存释放信号，流在发出部分文本后等待释放。
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.requests: list[tuple[Message, ...]] = []

    # 先产出部分文本增量再阻塞，取消时已累积文本应被保留。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system, tools
        self.requests.append(tuple(messages))
        yield TextDelta("Partial ")
        yield TextDelta("answer")
        await self.release.wait()
        yield StreamComplete()


# 轮询等待活动回合结束，避免固定时长导致测试不稳定。
async def _wait_done(app: SeaCodeApp, pilot: Any, max_pauses: int = 40) -> None:
    for _ in range(max_pauses):
        if not app._streaming:
            break
        await pilot.pause(0.05)


# 验证单轮内 >=2 个可折叠工具时 mount ToolGroupSummary 并隐藏工具块。
# 用 mock ReadFile 替换真实工具避免文件系统访问，断言摘要文本含工具数与耗时。
@pytest.mark.asyncio
async def test_tool_group_summary_mounts_for_multiple_collapsible_tools() -> None:
    class _ReadParams(BaseModel):
        file_path: str = ""

    class _MockReadFile(Tool):
        name = "ReadFile"
        description = "mock read for collapsible summary"
        params_model = _ReadParams
        category = ToolCategory.READ

        async def execute(self, params: BaseModel) -> ToolResult:
            del params
            return ToolResult(content="file content")

    client = _FakeClient(
        [
            [
                ToolCallStart(tool_name="ReadFile", tool_id="r1"),
                ToolCallComplete(
                    tool_id="r1", tool_name="ReadFile", arguments={"file_path": "a"}
                ),
                ToolCallStart(tool_name="ReadFile", tool_id="r2"),
                ToolCallComplete(
                    tool_id="r2", tool_name="ReadFile", arguments={"file_path": "b"}
                ),
                StreamComplete(input_tokens=1, output_tokens=1),
            ],
            [TextDelta("Done"), StreamComplete(input_tokens=1, output_tokens=1)],
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    # 用 mock 覆盖真实 ReadFile，避免读取真实文件。
    app._tool_registry.register(_MockReadFile())

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Read both")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        summaries = app.query(ToolGroupSummary)
        assert len(summaries) == 1
        assert "2 tool uses" in str(summaries[0].render())


# 验证 Esc 取消正在进行的回合，保留已累积文本并显示系统消息。
# 阻塞流先产出部分文本再等待，按 Esc 后断言系统消息与输入恢复。
@pytest.mark.asyncio
async def test_escape_cancels_running_turn_and_shows_system_message() -> None:
    client = _PartialBlockingClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Block")
        await pilot.press("enter")
        # 让部分文本流出并进入阻塞等待。
        for _ in range(8):
            await pilot.pause(0.05)
        assert app._streaming is True

        await pilot.press("escape")
        await _wait_done(app, pilot)

        system_messages = app.query(".system-message")
        assert any("Operation cancelled" in str(m.render()) for m in system_messages)
        assert "Ready" in str(app.query_one("#turn-status").render())
        assert input_widget.disabled is False


# 验证 LoopComplete 后展示 thinking-done 行，含动词过去式与耗时。
# 单轮流式回复结束后，断言挂载 thinking-done 行且文本含耗时格式。
@pytest.mark.asyncio
async def test_loop_complete_mounts_thinking_done_line() -> None:
    client = _FakeClient(
        [[TextDelta("Answer"), StreamComplete(input_tokens=1, output_tokens=1)]]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Hi")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        done_lines = app.query(".thinking-done")
        assert len(done_lines) == 1
        text = str(done_lines[0].render())
        assert "✻" in text
        assert " for " in text
