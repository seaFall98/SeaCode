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
from seacode.permission_dialog import InlinePermissionWidget
from seacode.permissions import PermissionMode, RuleEngine
from seacode.tools.base import Tool, ToolCategory, ToolResult


# 提供可按回合返回事件或抛出错误的本地假客户端。
class _FakeClient(LLMClient):
    # 保存每个测试回合的预设结果。
    def __init__(self, outcomes: list[list[StreamEvent] | Exception | _PartialFailure]) -> None:
        self._outcomes = outcomes
        self.requests: list[tuple[Message, ...]] = []

    # 记录请求历史并交付预设事件，不连接真实 Provider。
    # batch08 后 LoopComplete 会异步触发记忆提取、会话摘要与内存整理，
    # 三者通过同一 client.stream 发起后台请求；这些请求以特定提示词
    # 开头，返回空流不消耗 outcome，也不记入 requests，避免污染断言。
    # 内存整理通过子 Agent 发起，会注入环境上下文，故检查全部消息。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system, tools
        first = messages[0].content if messages else ""
        if first.startswith("Analyze the conversation below"):
            return
        if first.startswith("你是一个对话摘要助手"):
            return
        if any(m.content.startswith("# Dream: Memory Consolidation") for m in messages):
            return
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


# 过滤请求中的环境上下文与长期记忆注入消息，便于断言用户/助手消息序列。
# agent.run 会通过 inject_environment 在 position 0 插入会话级环境上下文
# （content 以 "Current working directory" 开头），并通过 inject_long_term_memory
# 在 position 1 插入 <system-reminder> 包裹的指令+MEMORY.md+日期（content 以
# "<system-reminder>" 开头）。两者时间戳会变且与断言无关，故过滤后再断言。
def _strip_env_context(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    return tuple(
        m for m in messages
        if not m.content.startswith("Current working directory")
        and not m.content.startswith("<system-reminder>")
    )


# 批量过滤多个请求的环境上下文消息，并排除后台 LLM 请求。
# batch08 后 LoopComplete 会异步触发记忆提取与会话摘要生成，
# 两者都通过同一 client.stream 发起，与主对话流无关：
# - 记忆提取：首条消息以 "Analyze the conversation below" 开头
# - 会话摘要：首条消息以 "你是一个对话摘要助手" 开头
def _strip_env_from_requests(
    requests: list[tuple[Message, ...]],
) -> list[tuple[Message, ...]]:
    result: list[tuple[Message, ...]] = []
    for r in requests:
        stripped = _strip_env_context(r)
        if not stripped:
            continue
        first = stripped[0].content
        if first.startswith("Analyze the conversation below"):
            continue
        if first.startswith("你是一个对话摘要助手"):
            continue
        result.append(stripped)
    return result


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
        # batch05 后状态栏新增 mode-label，初始显示 [default]。
        assert "[default]" in str(app.query_one("#mode-label", Static).render())
        assert not app.query("#teammates-label")
        input_widget.load_text("Hi")
        await pilot.press("enter")
        await _settle(pilot)

        assert input_widget.disabled is False
        assert _strip_env_from_requests(client.requests) == [
            (Message(role="user", content="Hi"),)
        ]
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

        assert _strip_env_from_requests(client.requests) == [
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

        assert _strip_env_from_requests(client.requests) == [
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
        assert _strip_env_from_requests(client.requests) == [
            (Message(role="user", content="First"),)
        ]

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

    assert _strip_env_from_requests(client.requests) == [
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


# ---------------------------------------------------------------------------
# batch05：TUI 权限对话框与模式切换
# ---------------------------------------------------------------------------


# 权限对话框测试专用的 Mock WriteFile，避免真实文件写入。
class _PermWriteParams(BaseModel):
    file_path: str = ""
    content: str = ""


class _MockPermWriteFile(Tool):
    name = "WriteFile"
    description = "mock write for TUI permission tests"
    params_model = _PermWriteParams
    category = ToolCategory.WRITE

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(content=f"wrote {params.file_path}")


# 构造单次 WriteFile 工具调用流 + 文本回复流，供权限对话框测试复用。
def _write_file_call_and_reply(
    file_path: str = "perm_test.txt",
) -> list[list[StreamEvent]]:
    return [
        [
            ToolCallStart(tool_name="WriteFile", tool_id="w1"),
            ToolCallComplete(
                tool_id="w1",
                tool_name="WriteFile",
                arguments={"file_path": file_path},
            ),
            StreamComplete(input_tokens=1, output_tokens=1),
        ],
        [TextDelta("Done"), StreamComplete(input_tokens=1, output_tokens=1)],
    ]


# 轮询等待权限对话框挂载；通过 _pending_permission 判定。
async def _wait_for_permission_dialog(app: SeaCodeApp, pilot: Any) -> None:
    for _ in range(40):
        await pilot.pause(0.05)
        if app._pending_permission is not None:
            return
    raise AssertionError("权限对话框未出现")


# 替换 app 的 rule engine 为空规则引擎，避免本地配置干扰与文件写入。
def _reset_rule_engine(app: SeaCodeApp) -> None:
    if app._permission_checker is not None:
        app._permission_checker.rule_engine = RuleEngine()


# 检查权限对话框是否仍挂载在聊天区。
def _has_permission_dialog(app: SeaCodeApp) -> bool:
    try:
        app.query_one("#perm-inline", InlinePermissionWidget)
        return True
    except Exception:
        return False


# 验证 Shift+Tab 循环切换四种权限模式，且模式标签同步更新。
# 从 default 开始按 4 次 Shift+Tab，断言每次模式与标签文本正确。
@pytest.mark.asyncio
async def test_shift_tab_cycles_permission_modes() -> None:
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        assert app._permission_mode == PermissionMode.DEFAULT
        assert "[default]" in str(app.query_one("#mode-label", Static).render())

        await pilot.press("shift+tab")
        assert app._permission_mode == PermissionMode.ACCEPT_EDITS
        assert "[accept-edits]" in str(
            app.query_one("#mode-label", Static).render()
        )

        await pilot.press("shift+tab")
        assert app._permission_mode == PermissionMode.PLAN
        assert "[plan]" in str(app.query_one("#mode-label", Static).render())

        await pilot.press("shift+tab")
        assert app._permission_mode == PermissionMode.BYPASS
        assert "[YOLO]" in str(app.query_one("#mode-label", Static).render())

        # 第 4 次循环回 default。
        await pilot.press("shift+tab")
        assert app._permission_mode == PermissionMode.DEFAULT
        assert "[default]" in str(app.query_one("#mode-label", Static).render())


# 验证 DEFAULT 模式下 WriteFile 触发内联权限对话框。
# 提交触发 WriteFile 的文本后，断言 InlinePermissionWidget 挂载且 _pending_permission 非空。
@pytest.mark.asyncio
async def test_permission_dialog_appears_for_write_in_default_mode() -> None:
    client = _FakeClient(_write_file_call_and_reply())
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    app._tool_registry.register(_MockPermWriteFile())

    async with app.run_test() as pilot:
        _reset_rule_engine(app)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Write file")
        await pilot.press("enter")

        await _wait_for_permission_dialog(app, pilot)
        assert app._pending_permission is not None
        assert app._pending_permission.tool_name == "WriteFile"
        assert _has_permission_dialog(app)

        # 清理：Esc 拒绝以结束回合。
        await pilot.press("escape")
        await _wait_done(app, pilot)


# 验证权限对话框 Enter 确认（默认光标在 Yes）放行工具执行。
# 等待对话框后按 Enter，断言无错误工具块且对话框已移除。
@pytest.mark.asyncio
async def test_permission_dialog_enter_allows_tool_execution() -> None:
    client = _FakeClient(_write_file_call_and_reply())
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    app._tool_registry.register(_MockPermWriteFile())

    async with app.run_test() as pilot:
        _reset_rule_engine(app)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Write file")
        await pilot.press("enter")

        await _wait_for_permission_dialog(app, pilot)
        # 默认光标在第 0 项（Yes = ALLOW），Enter 确认。
        await pilot.press("enter")
        await _wait_done(app, pilot)

        # 工具执行成功：无错误样式块，对话框已移除。
        assert not app.query(".tool-block-error")
        assert not _has_permission_dialog(app)


# 验证权限对话框通过键盘导航到 No 选项后 Enter 拒绝工具执行。
# 等待对话框后按 Down+Down 移到 No，再 Enter 确认，断言挂载错误工具块且对话框已移除。
@pytest.mark.asyncio
async def test_permission_dialog_navigate_to_no_and_deny_tool() -> None:
    client = _FakeClient(_write_file_call_and_reply())
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    app._tool_registry.register(_MockPermWriteFile())

    async with app.run_test() as pilot:
        _reset_rule_engine(app)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Write file")
        await pilot.press("enter")

        await _wait_for_permission_dialog(app, pilot)
        # Down+Down 移到第 3 项（No = DENY），Enter 确认。
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        error_blocks = app.query(".tool-block-error")
        assert len(error_blocks) == 1
        assert not _has_permission_dialog(app)


# 验证权限对话框方向键导航到第 2 项后 Enter 触发 ALLOW_ALWAYS。
# 按 Down 移到 "don't ask again" 项后 Enter，断言工具执行成功且对话框移除。
@pytest.mark.asyncio
async def test_permission_dialog_down_then_enter_allow_always() -> None:
    client = _FakeClient(_write_file_call_and_reply())
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    app._tool_registry.register(_MockPermWriteFile())

    async with app.run_test() as pilot:
        _reset_rule_engine(app)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Write file")
        await pilot.press("enter")

        await _wait_for_permission_dialog(app, pilot)
        # Down 移到第 2 项（Yes, and don't ask again = ALLOW_ALWAYS）。
        await pilot.press("down")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        assert not app.query(".tool-block-error")
        assert not _has_permission_dialog(app)


# 验证 BYPASS 模式下 WriteFile 自动放行，不触发权限对话框。
# 切换到 BYPASS 后提交触发 WriteFile 的文本，断言无对话框且工具执行成功。
@pytest.mark.asyncio
async def test_bypass_mode_skips_permission_dialog() -> None:
    client = _FakeClient(_write_file_call_and_reply())
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    app._tool_registry.register(_MockPermWriteFile())

    async with app.run_test() as pilot:
        _reset_rule_engine(app)
        # 切换到 BYPASS 模式。
        await pilot.press("shift+tab")
        await pilot.press("shift+tab")
        await pilot.press("shift+tab")
        assert app._permission_mode == PermissionMode.BYPASS

        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Write file")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        # BYPASS 模式不触发 HITL 对话框。
        assert app._pending_permission is None
        assert not _has_permission_dialog(app)
        assert not app.query(".tool-block-error")
