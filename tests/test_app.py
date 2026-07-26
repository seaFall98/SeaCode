from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from rich.text import Text
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, OptionList, Static

from seacode.app import (
    MAX_AT_REF_BYTES,
    ChatInput,
    SeaCodeApp,
    ToolGroupSummary,
    expand_at_refs,
    scan_files_for_at,
)
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
from seacode.commands.registry import Command, CommandContext, CommandType
from seacode.config import ProviderConfig
from seacode.context import build_recovery_attachment
from seacode.conversation import Message
from seacode.permission_dialog import InlinePermissionWidget
from seacode.permissions import PermissionMode, RuleEngine
from seacode.plan_dialog import InlinePlanWidget, PlanChoice
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


# 捕获 Plan 模式下实际交给模型的上下文，不连接真实 Provider。
class _PlanModeCaptureClient(LLMClient):
    def __init__(self) -> None:
        self.requests: list[tuple[Message, ...]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system, tools
        self.requests.append(tuple(messages))
        yield TextDelta("Plan context received")
        yield StreamComplete(input_tokens=1, output_tokens=1)


# 独立记忆选择器客户端，避免与主对话的预设流混用。
class _MemorySelectorClient(LLMClient):
    def __init__(self) -> None:
        self.requests: list[tuple[Message, ...]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system, tools
        self.requests.append(tuple(messages))
        yield TextDelta('{"selected_memories": ["conventions.md"]}')
        yield StreamComplete()


# 记录跨回合初始化次数的 MCP 管理器，不连接真实外部服务。
class _SessionMCPManager:
    def __init__(self) -> None:
        from seacode.mcp.manager import ConnectResult, ServerInfo

        self._result = ConnectResult(
            servers=[ServerInfo(name="docs", instructions="Use the project docs first.")]
        )
        self.is_initialized = False
        self.register_calls = 0

    async def register_all_tools(self, registry: Any) -> Any:
        del registry
        self.register_calls += 1
        self.is_initialized = True
        return self._result


# 模拟完成计划、请求审批与后续执行的两段模型响应。
class _PlanApprovalClient(LLMClient):
    def __init__(self) -> None:
        self.requests: list[tuple[Message, ...]] = []
        self.plan_path: Path | None = None
        self._requested_approval = False

    # 首次 Plan 回合写入提示中的计划文件并请求审批，后续回合返回完成文本。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system
        if tools is None:
            return
        self.requests.append(tuple(messages))
        reminders = [
            message.content
            for message in messages
            if "Plan mode is active" in message.content
        ]
        if reminders and not self._requested_approval:
            match = re.search(r"Plan file: ([^\n]+)", reminders[-1])
            assert match is not None
            self.plan_path = Path(match.group(1))
            self.plan_path.write_text(
                "# Delivery plan\\n\\nImplement the requested change.",
                encoding="utf-8",
            )
            self._requested_approval = True
            yield ToolCallStart(tool_name="ExitPlanMode", tool_id="plan-exit")
            yield ToolCallComplete(
                tool_id="plan-exit", tool_name="ExitPlanMode", arguments={}
            )
            yield StreamComplete(input_tokens=1, output_tokens=1)
            return
        yield TextDelta("Execution complete")
        yield StreamComplete(input_tokens=1, output_tokens=1)


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
        # thinking 内容不直接在 TUI 显示；回合结束后展示 thinking-done 行。
        assert app.query_one(".thinking-done") is not None


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


# 等待 Plan 审批组件挂载，避免依赖固定延迟。
async def _wait_for_plan_approval(app: SeaCodeApp, pilot: Any) -> InlinePlanWidget:
    for _ in range(40):
        await pilot.pause(0.05)
        try:
            return app.query_one("#plan-inline", InlinePlanWidget)
        except Exception:
            pass
    raise AssertionError("Plan 审批组件未出现")


# 等待审批选择触发的下一次模型请求完成。
async def _wait_for_request_count(
    app: SeaCodeApp, client: _PlanApprovalClient, pilot: Any, count: int
) -> None:
    for _ in range(40):
        await pilot.pause(0.05)
        if len(client.requests) >= count and not app._streaming:
            return
    raise AssertionError(f"模型请求数量未达到 {count}")


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


# 验证 /permission 在首回合及后续回合均同步 App、检查器与 Agent 的权限模式。
# 首回合前切换后完成两次普通回合，断言新建 Agent 始终继承同一模式。
@pytest.mark.asyncio
async def test_permission_command_syncs_modes_across_turns() -> None:
    client = _FakeClient(
        [
            [TextDelta("first"), StreamComplete(input_tokens=1, output_tokens=1)],
            [TextDelta("second"), StreamComplete(input_tokens=1, output_tokens=1)],
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        assert await app._dispatch_command("/permission mode acceptEdits")
        assert app._permission_mode == PermissionMode.ACCEPT_EDITS
        assert app._permission_checker is not None
        assert app._permission_checker.mode == PermissionMode.ACCEPT_EDITS

        input_widget = app.query_one(ChatInput)
        input_widget.load_text("first turn")
        await pilot.press("enter")
        await _wait_done(app, pilot)
        assert app._agent is not None
        assert app._agent.permission_mode == PermissionMode.ACCEPT_EDITS

        assert await app._dispatch_command("/permission mode bypassPermissions")
        assert app._permission_mode == PermissionMode.BYPASS
        assert app._permission_checker.mode == PermissionMode.BYPASS
        assert app._agent.permission_mode == PermissionMode.BYPASS

        input_widget.load_text("second turn")
        await pilot.press("enter")
        await _wait_done(app, pilot)
        assert app._agent is not None
        assert app._agent.permission_mode == PermissionMode.BYPASS


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


# 验证受限窗口和长聊天历史下，写入确认组件完整显示在聊天区底部。
# 构造溢出聊天历史后触发 WriteFile，断言刷新后的组件高度和滚动位置正确。
@pytest.mark.asyncio
async def test_permission_dialog_is_fully_visible_in_short_viewport() -> None:
    client = _FakeClient(_write_file_call_and_reply())
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    app._tool_registry.register(_MockPermWriteFile())

    async with app.run_test(size=(100, 30)) as pilot:
        _reset_rule_engine(app)
        for index in range(28):
            await app._append_static(Text(f"history {index}"), "message system-message")

        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Write file")
        await pilot.press("enter")
        await _wait_for_permission_dialog(app, pilot)
        await pilot.pause()

        chat = app.query_one("#chat-area", VerticalScroll)
        dialog = app.query_one("#perm-inline", InlinePermissionWidget)
        assert dialog.region.height > 1
        assert chat.scroll_y == chat.max_scroll_y
        assert dialog.region.bottom <= chat.region.bottom

        await pilot.press("escape")
        await _wait_done(app, pilot)


# 验证 /plan 后下一次模型请求收到 Plan 提醒与计划文件路径。
# 通过真实 TUI 命令切换后发送普通消息，捕获 Provider 输入并断言运行时语义。
@pytest.mark.asyncio
async def test_plan_mode_is_injected_into_next_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _PlanModeCaptureClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        assert await app._dispatch_command("/plan") is True
        assert app._permission_mode == PermissionMode.PLAN

        input_widget = app.query_one(ChatInput)
        input_widget.load_text("当前是什么模式？")
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if client.requests:
                break
        assert client.requests
        await _wait_done(app, pilot)

    request = next(
        request
        for request in client.requests
        if any("当前是什么模式？" in message.content for message in request)
    )
    assert any("Plan mode is active" in message.content for message in request)
    assert app._permission_checker is not None
    plan_path = Path(app._permission_checker.plan_file_path)
    assert plan_path.parent == tmp_path / ".seacode" / "plans"


# 验证主 TUI 回合会为用户消息启动非阻塞的记忆召回任务。
# 替换召回入口为可观察协程，断言实际查询文本由当前回合传入。
@pytest.mark.asyncio
async def test_user_turn_starts_memory_recall_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    memory_dir = tmp_path / ".seacode" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "conventions.md").write_text(
        "---\ndescription: project conventions\ntype: project\n---\n\nUse the established API.",
        encoding="utf-8",
    )
    client = _FakeClient([[TextDelta("Done"), StreamComplete()]])
    selector_client = _MemorySelectorClient()
    clients = iter([client, selector_client])
    app = SeaCodeApp([_provider()], client_factory=lambda _: next(clients))

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Recall the project conventions")
        await pilot.press("enter")
        await _wait_done(app, pilot)
        await pilot.pause(0.1)

    assert len(selector_client.requests) == 1
    assert "Recall the project conventions" in selector_client.requests[0][0].content


# 验证连续 TUI 回合复用 MCP 初始化结果，不重复连接或注入服务器说明。
# 用共享管理器完成两条消息，断言只注册一次且第二次请求没有重复说明。
@pytest.mark.asyncio
async def test_consecutive_turns_reuse_mcp_initialization() -> None:
    client = _FakeClient(
        [
            [TextDelta("First response"), StreamComplete()],
            [TextDelta("Second response"), StreamComplete()],
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    manager = _SessionMCPManager()
    app._mcp_manager = manager  # type: ignore[assignment]

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("First request")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        input_widget.load_text("Second request")
        await pilot.press("enter")
        await _wait_done(app, pilot)

    assert manager.register_calls == 1
    assert len(client.requests) == 2
    instruction_count = sum(
        message.content.count("Use the project docs first.")
        for message in client.requests[1]
    )
    assert instruction_count == 1


# 验证跨用户回合保留压缩恢复快照与熔断状态。
# 在首回合写入文件快照和熔断记录，第二回合断言新 Agent 仍引用同一会话状态。
@pytest.mark.asyncio
async def test_consecutive_turns_preserve_context_recovery_state() -> None:
    client = _FakeClient(
        [
            [TextDelta("First response"), StreamComplete()],
            [TextDelta("Second response"), StreamComplete()],
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("First request")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        first_agent = app._agent
        assert first_agent is not None
        first_agent.recovery_state.record_file_read("/workspace/settings.py", "PORT = 8080")
        first_agent.compact_breaker.record_failure()
        first_agent.replacement_state.seen_ids.add("tool-result-1")
        first_agent.active_skills["review"] = "Inspect the latest diff."

        input_widget.load_text("Second request")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        second_agent = app._agent
        assert second_agent is not None
        assert second_agent.recovery_state is first_agent.recovery_state
        assert second_agent.compact_breaker is first_agent.compact_breaker
        assert second_agent.replacement_state is first_agent.replacement_state
        assert second_agent.active_skills is first_agent.active_skills
        attachment = build_recovery_attachment(second_agent.recovery_state, [])
        assert "settings.py" in attachment
        assert "PORT = 8080" in attachment


# 验证完成计划后显示审批组件，YOLO 选择会切换权限并带计划内容进入执行回合。
# 模拟模型写入计划并调用 ExitPlanMode，断言 TUI 审批和下一轮上下文形成完整闭环。
@pytest.mark.asyncio
async def test_plan_approval_yolo_starts_execution_with_plan_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _PlanApprovalClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        app.set_plan_mode(True)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Prepare a delivery plan")
        await pilot.press("enter")

        await _wait_for_plan_approval(app, pilot)
        assert input_widget.disabled is True
        await pilot.press("enter")
        await _wait_for_request_count(app, client, pilot, 2)

        assert app._permission_mode == PermissionMode.BYPASS
        assert app._permission_checker is not None
        assert app._permission_checker.mode == PermissionMode.BYPASS

    assert client.plan_path is not None
    execution_request = client.requests[1]
    assert any("Exited Plan Mode" in message.content for message in execution_request)
    assert any("Approved Plan:" in message.content for message in execution_request)
    assert any(
        "Implement the requested change." in message.content
        for message in execution_request
    )


# 验证长聊天历史和受限窗口中，Plan 审批组件仍完整可见并接收键盘焦点。
# 构造成功退出 Plan 的真实 TUI 路径，断言组件尺寸、滚动位置和焦点均可交互。
@pytest.mark.asyncio
async def test_plan_approval_is_visible_and_focused_in_short_viewport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _PlanApprovalClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test(size=(100, 30)) as pilot:
        for index in range(8):
            content = "\n".join(f"history {index}-{line}" for line in range(4))
            await app._append_static(Text(content), "message system-message")

        app.set_plan_mode(True)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Prepare a delivery plan")
        await pilot.press("enter")

        dialog = await _wait_for_plan_approval(app, pilot)
        await pilot.pause()

        chat = app.query_one("#chat-area", VerticalScroll)
        assert dialog.region.height > 1
        assert chat.scroll_y == chat.max_scroll_y
        assert dialog.region.y >= chat.region.y
        assert dialog.region.bottom <= chat.region.bottom
        assert app.focused is dialog

        await pilot.press("escape")
        await _wait_done(app, pilot)


# 验证手动确认恢复进入 Plan 前的权限模式，而不会错误进入自动确认模式。
# 先设为 accept-edits 再完成计划并选中第二项，断言应用与检查器同步恢复。
@pytest.mark.asyncio
async def test_plan_approval_manual_restores_previous_permission_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _PlanApprovalClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        app.set_permission_mode(PermissionMode.ACCEPT_EDITS)
        app.set_plan_mode(True)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Prepare a delivery plan")
        await pilot.press("enter")

        await _wait_for_plan_approval(app, pilot)
        await pilot.press("down", "enter")
        await _wait_for_request_count(app, client, pilot, 2)

        assert app._permission_mode == PermissionMode.ACCEPT_EDITS
        assert app._permission_checker is not None
        assert app._permission_checker.mode == PermissionMode.ACCEPT_EDITS


# 验证反馈后保留 Plan 模式并把同一计划文件交给下一回合继续修订。
# 在审批组件发送反馈，断言下一次请求含反馈、仍是 Plan 提醒且路径没有变化。
@pytest.mark.asyncio
async def test_plan_approval_feedback_keeps_plan_mode_and_plan_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _PlanApprovalClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        app.set_plan_mode(True)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Prepare a delivery plan")
        await pilot.press("enter")

        widget = await _wait_for_plan_approval(app, pilot)
        widget.post_message(
            InlinePlanWidget.Responded(PlanChoice.FEEDBACK, "Please simplify the plan.")
        )
        await _wait_for_request_count(app, client, pilot, 2)

        assert app._permission_mode == PermissionMode.PLAN
        assert app._permission_checker is not None
        assert app._permission_checker.mode == PermissionMode.PLAN

    assert client.plan_path is not None
    feedback_request = client.requests[1]
    assert any("Please simplify the plan." in message.content for message in feedback_request)
    assert any(
        "Plan mode is active" in message.content and str(client.plan_path) in message.content
        for message in feedback_request
    )


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


# ---------------------------------------------------------------------------
# batch09：命令框架集成测试
# （expand_at_refs / scan_files_for_at 纯函数 + _dispatch_command 分支 +
#  Pilot 端到端 /help /clear /status /review /unknown + ChatInput 历史持久化与重载）
# ---------------------------------------------------------------------------


# 异常 handler，用于验证 _dispatch_command 捕获 handler 异常的分支。
async def _raise_handler(ctx: CommandContext) -> None:
    raise RuntimeError("boom for test")


# 轮询等待包含 needle 的系统消息出现在聊天区，超时返回 False。
async def _wait_for_system_message(
    app: SeaCodeApp, pilot: Any, needle: str, max_pauses: int = 40
) -> bool:
    for _ in range(max_pauses):
        await pilot.pause(0.05)
        msgs = app.query(".system-message")
        if any(needle in str(m.render()) for m in msgs):
            return True
    return False


# === expand_at_refs 纯函数测试 ===

# 验证 expand_at_refs 把 @path 替换为文件内容块。
# 测试设计为在临时目录创建 a.txt 含 hello，断言展开结果含 [File: a.txt] 与 hello。
def test_expand_at_refs_replaces_with_file_content(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    result = expand_at_refs("@a.txt", tmp_path)
    assert "[File: a.txt]" in result
    assert "hello" in result


# 验证 expand_at_refs 对不存在的文件保留原文，不抛异常。
# 测试设计为引用不存在文件，断言原文保留且不含 [File: 标记。
def test_expand_at_refs_preserves_nonexistent_file(tmp_path: Path) -> None:
    result = expand_at_refs("@nonexistent.txt", tmp_path)
    assert "@nonexistent.txt" in result
    assert "[File:" not in result


# 验证 expand_at_refs 对超过 MAX_AT_REF_BYTES 的文件截断到上限。
# 测试设计为创建大文件，断言展开内容恰为 MAX_AT_REF_BYTES 字节，不含完整原文。
def test_expand_at_refs_truncates_large_file(tmp_path: Path) -> None:
    big_content = "A" * (MAX_AT_REF_BYTES + 5000)
    (tmp_path / "big.txt").write_text(big_content, encoding="utf-8")
    result = expand_at_refs("@big.txt", tmp_path)
    assert "[File: big.txt]" in result
    assert ("A" * MAX_AT_REF_BYTES) in result
    assert ("A" * (MAX_AT_REF_BYTES + 1)) not in result


# 验证 expand_at_refs 一次展开多个 @ 引用。
# 测试设计为创建两个文件并引用，断言两个文件内容都出现在结果中。
def test_expand_at_refs_expands_multiple_refs(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("content_a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("content_b", encoding="utf-8")
    result = expand_at_refs("@a.txt @b.txt", tmp_path)
    assert "[File: a.txt]" in result
    assert "content_a" in result
    assert "[File: b.txt]" in result
    assert "content_b" in result


# 验证 expand_at_refs 拒绝展开工作目录外的路径，保留原文。
# 测试设计为把工作目录设为 tmp_path 子目录，引用父目录文件，断言原文保留。
def test_expand_at_refs_blocks_path_traversal(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (tmp_path / "outside_secret.txt").write_text("SECRET_CONTENT", encoding="utf-8")
    result = expand_at_refs("@../outside_secret.txt", work_dir)
    assert "@../outside_secret.txt" in result
    assert "SECRET_CONTENT" not in result


# 验证 expand_at_refs 对无 @ 的文本原样返回。
# 测试设计为传入普通文本，断言返回值与原文完全相等。
def test_expand_at_refs_no_at_returns_original(tmp_path: Path) -> None:
    assert expand_at_refs("hello world", tmp_path) == "hello world"


# === scan_files_for_at 纯函数测试 ===

# 验证 scan_files_for_at 按文件名前缀返回匹配候选。
# 测试设计为创建 app.py / apple.py / banana.py，断言前缀 app 命中前两者、不含 banana。
def test_scan_files_for_at_prefix_match(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    (tmp_path / "apple.py").write_text("", encoding="utf-8")
    (tmp_path / "banana.py").write_text("", encoding="utf-8")
    result = scan_files_for_at("app", tmp_path)
    assert "app.py" in result
    assert "apple.py" in result
    assert "banana.py" not in result


# 验证 scan_files_for_at 跳过运行时产物与依赖目录下的文件。
# 测试设计为在跳过目录中放置匹配文件，断言结果只含根目录匹配文件。
def test_scan_files_for_at_skips_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "match_root.txt").write_text("", encoding="utf-8")
    for skip_dir in (".seacode", ".git", "__pycache__", ".venv", "node_modules"):
        d = tmp_path / skip_dir
        d.mkdir()
        (d / "match_skip.txt").write_text("", encoding="utf-8")
    result = scan_files_for_at("match", tmp_path)
    assert "match_root.txt" in result
    assert not any("match_skip" in r for r in result)


# 验证 scan_files_for_at 的 limit 参数限制返回数量。
# 测试设计为创建 10 个匹配文件并设 limit=3，断言结果长度恰为 3。
def test_scan_files_for_at_respects_limit(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"file_{i:02d}.txt").write_text("", encoding="utf-8")
    result = scan_files_for_at("file", tmp_path, limit=3)
    assert len(result) == 3
    assert all(r.startswith("file_") for r in result)


# 验证 scan_files_for_at 无匹配时返回空列表。
# 测试设计为传入不匹配的前缀，断言结果为空列表。
def test_scan_files_for_at_no_match_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    assert scan_files_for_at("zzz", tmp_path) == []


# === _dispatch_command 分支测试（直接调用，避免弹窗时序干扰） ===

# 验证 _dispatch_command 对仅斜杠输入列出全部命令。
# 测试设计为直接调用 _dispatch_command("/")，断言返回 True 且系统消息含命令列表。
@pytest.mark.asyncio
async def test_dispatch_slash_only_lists_all_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        result = await app._dispatch_command("/")
        assert result is True
        ok = await _wait_for_system_message(app, pilot, "可用命令")
        assert ok
        text = "\n".join(str(m.render()) for m in app.query(".system-message"))
        assert "/help" in text
        assert "/clear" in text


# 验证 _dispatch_command 对非斜杠输入返回 False 不走命令路径。
# 测试设计为直接调用 _dispatch_command("hello")，断言返回 False。
@pytest.mark.asyncio
async def test_dispatch_non_command_returns_false() -> None:
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    result = await app._dispatch_command("hello world")
    assert result is False


# 验证 _dispatch_command 捕获 handler 异常并显示失败提示。
# 测试设计为注册一个抛异常的命令，直接调用 _dispatch_command 并断言错误提示出现。
@pytest.mark.asyncio
async def test_dispatch_command_handler_exception_shows_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    boom_cmd = Command(
        name="boom",
        description="raise for test",
        type=CommandType.LOCAL,
        handler=_raise_handler,
    )
    app._command_registry.register_sync(boom_cmd)
    async with app.run_test() as pilot:
        result = await app._dispatch_command("/boom")
        assert result is True
        ok = await _wait_for_system_message(app, pilot, "命令执行失败")
        assert ok
        text = "\n".join(str(m.render()) for m in app.query(".system-message"))
        assert "命令执行失败" in text


# === TUI 集成测试（Pilot 驱动输入 + 断言聊天区） ===

# 验证通过 Pilot 输入 /help 回车后聊天区显示命令列表。
# 测试设计为 load_text("/help") 后回车，断言系统消息含 "可用命令" 与若干命令名。
@pytest.mark.asyncio
async def test_pilot_help_command_lists_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("/help")
        await pilot.press("enter")
        ok = await _wait_for_system_message(app, pilot, "可用命令")
        assert ok
        text = "\n".join(str(m.render()) for m in app.query(".system-message"))
        assert "/help" in text
        assert "/clear" in text


# 验证通过 Pilot 输入 /clear 回车后聊天区被清空并创建新会话。
# 测试设计为先 /status 制造消息，再 /clear，断言只剩 "已清空" 且会话已切换。
@pytest.mark.asyncio
async def test_pilot_clear_command_clears_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        # 先发 /status 制造一条系统消息。
        input_widget.load_text("/status")
        await pilot.press("enter")
        await _wait_for_system_message(app, pilot, "SeaCode 当前状态")
        session_before = app._session

        # 提交 /clear 清空聊天并创建新会话。
        input_widget.load_text("/clear")
        await pilot.press("enter")
        ok = await _wait_for_system_message(app, pilot, "已清空")
        assert ok
        await pilot.pause()

        # 旧消息已移除，仅剩 "已清空"。
        text = "\n".join(str(m.render()) for m in app.query(".system-message"))
        assert "已清空" in text
        assert "SeaCode 当前状态" not in text
        # 新会话已创建。
        assert app._session is not None
        assert app._session is not session_before


# 验证通过 Pilot 输入 /status 回车后聊天区显示状态信息。
# 测试设计为 load_text("/status") 后回车，断言系统消息含状态标题与字段。
@pytest.mark.asyncio
async def test_pilot_status_command_shows_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("/status")
        await pilot.press("enter")
        ok = await _wait_for_system_message(app, pilot, "SeaCode 当前状态")
        assert ok
        text = "\n".join(str(m.render()) for m in app.query(".system-message"))
        assert "模型：" in text
        assert "会话 ID：" in text


# 验证通过 Pilot 输入 /review 回车后构造审查提示词并发给 LLM。
# 测试设计为 load_text("/review 关注并发安全") 后回车，断言 fake client 收到含提示词的请求。
@pytest.mark.asyncio
async def test_pilot_review_command_triggers_llm_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient(
        [[TextDelta("审查完成"), StreamComplete(input_tokens=1, output_tokens=1)]]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("/review 关注并发安全")
        await pilot.press("enter")
        await _wait_done(app, pilot)
        # /review 通过 send_user_message 把审查提示词发给 LLM。
        requests = _strip_env_from_requests(client.requests)
        assert any(
            any(m.content.startswith("请对当前工作目录的代码变更进行审查") for m in req)
            for req in requests
        )
        assert any(
            any("关注并发安全" in m.content for m in req) for req in requests
        )


# 验证通过 Pilot 输入未知命令后聊天区显示未知命令提示。
# 测试设计为 load_text("/unknown") 后回车，断言系统消息含 "未知命令：unknown"。
@pytest.mark.asyncio
async def test_pilot_unknown_command_shows_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("/unknown")
        await pilot.press("enter")
        ok = await _wait_for_system_message(app, pilot, "未知命令")
        assert ok
        text = "\n".join(str(m.render()) for m in app.query(".system-message"))
        assert "未知命令：unknown" in text


# === ChatInput 历史持久化与重载 ===

# 验证提交命令后历史写入 .seacode/history 文件。
# 测试设计为在临时工作目录提交 /help，断言 history 文件含 /help。
@pytest.mark.asyncio
async def test_command_history_persisted_after_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("/help")
        await pilot.press("enter")
        await _wait_for_system_message(app, pilot, "可用命令")
    history_file = tmp_path / ".seacode" / "history"
    assert history_file.exists()
    assert "/help" in history_file.read_text(encoding="utf-8")


# 验证启动时从 .seacode/history 加载历史并支持上键回填最后一条。
# 测试设计为预写 history 文件，启动 app 后断言 _history 列表与上键回填文本。
@pytest.mark.asyncio
async def test_command_history_loaded_on_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    history_file = tmp_path / ".seacode" / "history"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text("cmd1\ncmd2\n", encoding="utf-8")
    client = _FakeClient([])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one(ChatInput)
        assert input_widget._history == ["cmd1", "cmd2"]
        # 直接调用 action_nav_up 避免键盘模拟不稳定。
        input_widget.action_nav_up()
        assert input_widget.text == "cmd2"
