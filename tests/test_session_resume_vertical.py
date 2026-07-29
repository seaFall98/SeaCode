"""batch18：会话增量持久化、thinking 往返与跨启动恢复回归。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from seacode.app import ChatInput, SeaCodeApp
from seacode.client import (
    LLMClient,
    NetworkError,
    StreamComplete,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    ToolCallStart,
)
from seacode.config import ProviderConfig
from seacode.conversation import (
    ConversationManager,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from seacode.memory.session import SessionManager
from seacode.session_dialog import InlineResumeWidget


class _ScriptedClient(LLMClient):
    """按顺序返回预设流事件，并忽略 App 的后台摘要请求。"""

    def __init__(
        self, outcomes: Sequence[Sequence[StreamEvent] | Exception]
    ) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[tuple[Message, ...]] = []

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
        self.requests.append(tuple(messages))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for event in outcome:
            yield event


def _provider(name: str = "batch18-test") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol="openai-compat",
        model="test-model",
        base_url="https://api.example.test",
        api_key="test-key",
    )


async def _wait_done(app: SeaCodeApp, pilot: Any) -> None:
    for _ in range(30):
        await pilot.pause(0.05)
        if not app._streaming:
            return
    raise AssertionError("TUI turn did not finish")


def _canonical_request(messages: Sequence[Message]) -> list[Message]:
    return [
        message
        for message in messages
        if not message.content.startswith("Current working directory")
        and not message.content.startswith("<system-reminder>")
    ]


# 验证启动和退出不会隐式创建没有消息的持久化 session。
# 预写一个已有会话，启动 App 但不提交消息，断言当前 session 为空且文件集合不变。
@pytest.mark.asyncio
async def test_startup_without_message_does_not_create_empty_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    manager = SessionManager(str(tmp_path))
    existing = manager.create()
    existing_id = existing.session_id
    existing.append(Message(role="user", content="existing conversation"))
    existing.close()
    session_dir = tmp_path / ".seacode" / "sessions"
    before = {path.name for path in session_dir.iterdir()}

    app = SeaCodeApp([_provider()], client_factory=lambda _: _ScriptedClient([]))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app._session is None
        assert {path.name for path in session_dir.iterdir()} == before

    assert {path.name for path in session_dir.iterdir()} == before
    metas = SessionManager(str(tmp_path)).list()
    assert [meta.id for meta in metas] == [existing_id]


# 验证切换 Provider 后旧 session 已关闭，新的普通消息才创建新 session。
# 先完成一回合再切换 Provider，断言切换状态为空且下一回合不复用已关闭句柄。
@pytest.mark.asyncio
async def test_provider_switch_defers_new_session_until_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    providers = [_provider("first-provider"), _provider("second-provider")]
    clients = {
        "first-provider": _ScriptedClient([[StreamComplete()]]),
        "second-provider": _ScriptedClient([[StreamComplete()]]),
    }
    app = SeaCodeApp(
        providers,
        client_factory=lambda provider: clients[provider.name],
    )

    async with app.run_test() as pilot:
        app._select_provider(providers[0])
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("First provider turn")
        await pilot.press("enter")
        await _wait_done(app, pilot)
        assert app._session is not None
        first_session_id = app._session.session_id

        app._select_provider(providers[1])
        assert app._session is None
        assert app.file_history is None

        input_widget.load_text("Second provider turn")
        await pilot.press("enter")
        await _wait_done(app, pilot)
        assert app._session is not None
        assert app._session.session_id != first_session_id


# 验证运行时上下文不进入稳定会话增量边界。
# 普通用户消息可被写入，环境、system-reminder 与后台提示必须保持 transient。
def test_conversation_persistence_boundary_excludes_runtime_messages() -> None:
    conversation = ConversationManager()
    user = conversation.add_user_message("implement the fix")
    conversation.add_system_reminder("plan reminder")
    conversation.inject_environment("Current working directory: C:/repo")

    assert conversation.messages_to_persist() == (user,)
    conversation.mark_persisted((user,))
    assert conversation.messages_to_persist() == ()


# 验证 thinking 与签名在 JSONL 中无损往返。
# assistant 的正文、思考块、tool_use 和 tool_result 恢复后必须保持关联字段。
def test_session_roundtrip_preserves_thinking_signature_and_tools(tmp_path: Path) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session_id = session.session_id
    session.append(Message(role="user", content="inspect the file"))
    session.append(
        Message(
            role="assistant",
            content="I will inspect it.",
            thinking_blocks=[ThinkingBlock("check the project conventions", "sig-123")],
            tool_uses=[
                ToolUseBlock(
                    tool_use_id="tool-1",
                    tool_name="ReadFile",
                    arguments={"file_path": "README.md"},
                )
            ],
        )
    )
    session.append(
        Message(
            role="user",
            tool_results=[ToolResultBlock("tool-1", "file contents")],
        )
    )
    session.close()

    result = manager.resume(session_id)
    assert result is not None
    assistant = result.messages[1]
    assert assistant.content == "I will inspect it."
    assert assistant.thinking_blocks == [
        ThinkingBlock("check the project conventions", "sig-123")
    ]
    assert assistant.tool_uses[0].tool_use_id == "tool-1"
    assert result.messages[2].tool_results[0].content == "file contents"
    result.session.close()


# 验证恢复旧 JSONL 时按 assistant tool_use 顺序重排逆序 tool_result。
# 历史文件保留原始记录供审计，但 Provider 请求必须收到稳定的配对顺序。
def test_session_resume_orders_tool_results_by_assistant_calls(tmp_path: Path) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session_id = session.session_id
    session.append(
        Message(
            role="assistant",
            content="",
            tool_uses=[
                ToolUseBlock("call-1", "ReadFile", {"file_path": "one.txt"}),
                ToolUseBlock("call-2", "ReadFile", {"file_path": "two.txt"}),
            ],
        )
    )
    session.append(
        Message(
            role="user",
            tool_results=[
                ToolResultBlock("call-2", "two"),
                ToolResultBlock("call-1", "one"),
            ],
        )
    )
    session.close()

    result = manager.resume(session_id)
    assert result is not None
    assert [item.tool_use_id for item in result.messages[1].tool_results] == [
        "call-1",
        "call-2",
    ]
    result.session.close()


# 验证 Provider 请求失败前用户消息已经落盘，且当前进程后续回合不被失败请求污染。
# 第一次请求故意失败，随后恢复；关闭后直接 resume 应仍能读到失败前的用户意图。
@pytest.mark.asyncio
async def test_failed_turn_persists_user_message_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _ScriptedClient(
        [
            NetworkError("network failure"),
            [TextDelta("recovered"), StreamComplete(input_tokens=1, output_tokens=1)],
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("First intent")
        await pilot.press("enter")
        await _wait_done(app, pilot)
        assert app._session is not None
        session_id = app._session.session_id

        input_widget.load_text("Second intent")
        await pilot.press("enter")
        await _wait_done(app, pilot)

        assert _canonical_request(client.requests[1]) == [
            Message(role="user", content="Second intent")
        ]

    result = SessionManager(str(tmp_path)).resume(session_id)
    assert result is not None
    assert result.messages[0] == Message(role="user", content="First intent")
    assert result.messages[1] == Message(role="user", content="Second intent")
    assert result.messages[2] == Message(role="assistant", content="recovered")
    result.session.close()


# 验证 Ctrl+R 的选择事件与 /session resume 使用同一恢复 transition。
# 直接发送 InlineResumeWidget.Selected 事件，下一次请求必须带上已保存的历史。
@pytest.mark.asyncio
async def test_ctrl_r_resume_continues_previous_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session_id = session.session_id
    session.append(Message(role="user", content="Saved question"))
    session.append(Message(role="assistant", content="Saved answer"))
    session.close()

    client = _ScriptedClient(
        [[TextDelta("continued answer"), StreamComplete(input_tokens=1, output_tokens=1)]]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await app.on_inline_resume_widget_selected(InlineResumeWidget.Selected(session_id))
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Continue from Ctrl+R")
        await pilot.press("enter")
        await _wait_done(app, pilot)

    assert _canonical_request(client.requests[0]) == [
        Message(role="user", content="Saved question"),
        Message(role="assistant", content="Saved answer"),
        Message(role="user", content="Continue from Ctrl+R"),
    ]


# 验证真实 TUI 恢复入口可以用方向键浏览超过首个可视窗口的 session。
# 通过 run_test 挂载 InlineResumeWidget，连续发送下键后断言焦点候选进入后半段。
@pytest.mark.asyncio
async def test_ctrl_r_navigates_beyond_first_visible_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    manager = SessionManager(str(tmp_path))
    for index in range(12):
        session = manager.create()
        session.append(Message(role="user", content=f"Saved session {index}"))
        session.close()

    app = SeaCodeApp([_provider()], client_factory=lambda _: _ScriptedClient([]))
    async with app.run_test() as pilot:
        await app.action_open_resume()
        widget = app.query_one(InlineResumeWidget)
        await pilot.pause()
        initial_visible = widget._visible_count
        await pilot.resize_terminal(80, 12)
        assert 1 <= widget._visible_count <= initial_visible
        assert widget._filtered[widget._cursor].title in widget._build_content()

        for _ in range(11):
            await pilot.press("down")

        assert widget._cursor == 11
        assert widget._window_start > 0
        current = widget._filtered[widget._cursor]
        assert current.title in widget._build_content()


# 验证恢复历史后新的 TUI 请求携带昨天的对话，而不是从空历史重新开始。
# 第一 App 写入并关闭会话，第二 App 通过 /session resume 后继续发送 follow-up。
@pytest.mark.asyncio
async def test_resume_session_continues_previous_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first_client = _ScriptedClient(
        [[TextDelta("yesterday answer"), StreamComplete(input_tokens=1, output_tokens=1)]]
    )
    first_app = SeaCodeApp([_provider()], client_factory=lambda _: first_client)

    async with first_app.run_test() as pilot:
        input_widget = first_app.query_one(ChatInput)
        input_widget.load_text("Yesterday question")
        await pilot.press("enter")
        await _wait_done(first_app, pilot)
        session_id = first_app._session.session_id if first_app._session else ""

    second_client = _ScriptedClient(
        [[TextDelta("today answer"), StreamComplete(input_tokens=1, output_tokens=1)]]
    )
    second_app = SeaCodeApp([_provider()], client_factory=lambda _: second_client)

    async with second_app.run_test() as pilot:
        await second_app._dispatch_command(f"/session resume {session_id}")
        await pilot.pause(0.05)
        resumed_agent = second_app._agent
        assert resumed_agent is not None
        assert second_app.get_token_count()[0] == second_app._conversation.current_tokens()
        input_widget = second_app.query_one(ChatInput)
        input_widget.load_text("Continue today")
        await pilot.press("enter")
        await _wait_done(second_app, pilot)
        assert second_app._agent is resumed_agent

        await second_app._dispatch_command("/session new")
        assert second_app._agent is resumed_agent
        assert resumed_agent.total_input_tokens == 0
        assert second_app._conversation.messages == ()

    assert _canonical_request(second_client.requests[0]) == [
        Message(role="user", content="Yesterday question"),
        Message(role="assistant", content="yesterday answer"),
        Message(role="user", content="Continue today"),
    ]


# 验证工具循环在 Provider 失败时不会把未完成的 assistant/tool 链提交到 session。
# 用户意图已在请求前落盘，恢复后应只看到该 user，而不是无法继续的半截工具回合。
@pytest.mark.asyncio
async def test_failed_tool_loop_does_not_persist_unfinished_assistant_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    first_file.write_text("first\n", encoding="utf-8")
    second_file.write_text("second\n", encoding="utf-8")
    client = _ScriptedClient(
        [
            [
                ToolCallStart(tool_name="ReadFile", tool_id="call-1"),
                ToolCallComplete(
                    tool_id="call-1",
                    tool_name="ReadFile",
                    arguments={"file_path": str(first_file)},
                ),
                ToolCallStart(tool_name="ReadFile", tool_id="call-2"),
                ToolCallComplete(
                    tool_id="call-2",
                    tool_name="ReadFile",
                    arguments={"file_path": str(second_file)},
                ),
                StreamComplete(input_tokens=1, output_tokens=1),
            ],
            NetworkError("provider failure after tools"),
        ]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Read both files")
        await pilot.press("enter")
        await _wait_done(app, pilot)
        assert app._session is not None
        session_id = app._session.session_id

    result = SessionManager(str(tmp_path)).resume(session_id)
    assert result is not None
    assert result.messages == [Message(role="user", content="Read both files")]
    result.session.close()
