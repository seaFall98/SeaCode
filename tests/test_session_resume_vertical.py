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


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="batch18-test",
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
        session_id = app._session.session_id if app._session is not None else ""
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("First intent")
        await pilot.press("enter")
        await _wait_done(app, pilot)

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
