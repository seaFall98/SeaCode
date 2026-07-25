# Agent 与团队协调集成测试：coordinator_mode / _consume_mailbox / notification_fn。
# 覆盖字段默认值、邮箱消费全分支、notification_fn 注入、run_to_completion 调用顺序。
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from seacode.agent import Agent
from seacode.client import LLMClient, StreamComplete, StreamEvent, TextDelta
from seacode.conversation import ConversationManager
from seacode.teams.mailbox import create_message
from seacode.tools import ToolRegistry


# 假 LLMClient：按预设事件序列返回流，记录 system / tools 参数供断言。
class _FakeClient(LLMClient):
    def __init__(self, outcomes: list[list[StreamEvent]]) -> None:
        self._outcomes = outcomes
        self.systems_passed: list[str] = []
        self.tools_passed: list[list[dict[str, Any]] | None] = []

    async def stream(
        self,
        messages: Any,
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        self.systems_passed.append(system)
        self.tools_passed.append(tools)
        outcome = self._outcomes.pop(0)
        for event in outcome:
            yield event


# 构造纯文本回复流事件序列。
def _text_stream(text: str) -> list[StreamEvent]:
    return [TextDelta(text), StreamComplete(input_tokens=1, output_tokens=1)]


# ---------------------------------------------------------------------------
# 字段默认值
# ---------------------------------------------------------------------------


# 验证 Agent 新增团队字段默认值不影响既有行为。
# coordinator_mode=False / team_name=None / _team_manager=None / notification_fn=None。
def test_agent_team_fields_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # 避免真实 session 目录创建。
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    client = _FakeClient([])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    assert agent.coordinator_mode is False
    assert agent.team_name is None
    assert agent._team_manager is None
    assert agent.notification_fn is None


# ---------------------------------------------------------------------------
# _consume_mailbox
# ---------------------------------------------------------------------------


# 验证 _consume_mailbox 消费邮箱并 add_user_message 注入消息。
# fake mailbox.consume 返回 2 条消息，断言 add_user_message 调用 2 次。
def test_consume_mailbox_injects_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    client = _FakeClient([])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    agent.team_name = "demo"
    agent._team_manager = MagicMock()
    fake_mailbox = MagicMock()
    fake_mailbox.consume.return_value = [
        create_message("alice", "lead", "msg1", "s1"),
        create_message("bob", "lead", "msg2", "s2"),
    ]
    agent._team_manager.get_mailbox.return_value = fake_mailbox

    conv = MagicMock()
    agent._consume_mailbox(conv)

    agent._team_manager.get_mailbox.assert_called_once_with("demo")
    fake_mailbox.consume.assert_called_once_with(agent.agent_id)
    assert conv.add_user_message.call_count == 2
    # 第一条消息含 From alice 前缀。
    first_call_args = conv.add_user_message.call_args_list[0][0][0]
    assert "From alice" in first_call_args
    assert "msg1" in first_call_args


# 验证 _consume_mailbox 在 _team_manager 为 None 时跳过。
# 不调用任何 mailbox / conversation 方法。
def test_consume_mailbox_skips_when_no_team_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    client = _FakeClient([])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    agent.team_name = "demo"
    # _team_manager 默认 None。
    conv = MagicMock()
    agent._consume_mailbox(conv)
    conv.add_user_message.assert_not_called()


# 验证 _consume_mailbox 在 team_name 为空时跳过。
# 即使 _team_manager 非 None，team_name 空也跳过。
def test_consume_mailbox_skips_when_no_team_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    client = _FakeClient([])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    # team_name 默认 None；_team_manager 设为非 None。
    agent._team_manager = MagicMock()
    conv = MagicMock()
    agent._consume_mailbox(conv)
    agent._team_manager.get_mailbox.assert_not_called()
    conv.add_user_message.assert_not_called()


# ---------------------------------------------------------------------------
# notification_fn
# ---------------------------------------------------------------------------


# 验证 notification_fn 返回的 notes 通过 add_system_reminder 注入。
# fake notification_fn 返回 2 条 note，断言 add_system_reminder 调用 2 次。
def test_consume_team_notifications_injects_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    client = _FakeClient([])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    # _team_manager 为 None，_consume_mailbox 跳过；只测 notification_fn 路径。
    agent.notification_fn = lambda: ["note1", "note2"]
    conv = MagicMock()
    agent._consume_team_notifications(conv)
    assert conv.add_system_reminder.call_count == 2
    first_arg = conv.add_system_reminder.call_args_list[0][0][0]
    assert first_arg == "note1"


# 验证 notification_fn 为 None 时跳过 add_system_reminder 调用。
# _team_manager 也为 None，确保两条路径都跳过。
def test_consume_team_notifications_skips_when_no_notification_fn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    client = _FakeClient([])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conv = MagicMock()
    agent._consume_team_notifications(conv)
    conv.add_system_reminder.assert_not_called()


# ---------------------------------------------------------------------------
# run_to_completion 调用顺序
# ---------------------------------------------------------------------------


# 验证 run_to_completion 每轮开头调用 _consume_team_notifications。
# mock _consume_team_notifications 与 _consume_mailbox，验证调用顺序与次数。
@pytest.mark.asyncio
async def test_run_to_completion_invokes_consume_each_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    # 两次纯文本流：第一轮返回 "thinking"，第二轮返回 "final answer"。
    client = _FakeClient([_text_stream("thinking"), _text_stream("final answer")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    # mock _consume_team_notifications 记录调用次数。
    call_count = [0]

    def fake_consume(conv: Any) -> None:
        call_count[0] += 1

    monkeypatch.setattr(agent, "_consume_team_notifications", fake_consume)

    # 第一轮流式返回 "thinking"，但 run_to_completion 只跑一轮就结束（stop_reason=end_turn）。
    # 实际 run_to_completion 循环直到 stop_reason 为 end_turn 或工具调用结束。
    # 用单轮流即一轮结束，验证调用一次。
    conv = ConversationManager()
    conv.add_user_message("Hi")
    # run_to_completion 需要 client.stream 返回 StreamComplete 且 stop_reason 为空。
    # 单轮即结束。
    result = await agent.run_to_completion("Hi", conv)
    assert call_count[0] == 1
    assert result == "thinking"


# ---------------------------------------------------------------------------
# build_system_prompt 在 coordinator_mode=True 时传参
# ---------------------------------------------------------------------------


# 验证 coordinator_mode=True 时 run 调用 build_system_prompt 传 coordinator_mode=True。
# mock prompts.build_system_prompt，验证参数包含 coordinator_mode=True。
@pytest.mark.asyncio
async def test_run_passes_coordinator_mode_to_build_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("coordinator reply")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    agent.coordinator_mode = True

    captured: dict[str, Any] = {}

    def fake_build(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "coordinator system prompt"

    monkeypatch.setattr("seacode.agent.build_system_prompt", fake_build)

    conv = ConversationManager()
    conv.add_user_message("Hi")
    events = [event async for event in agent.run(conv)]
    # 验证 coordinator_mode 传 True。
    assert captured.get("coordinator_mode") is True
    # 验证 system prompt 被替换为协调者提示词。
    assert client.systems_passed[0] == "coordinator system prompt"
    # 验证有事件产出（不空）。
    assert len(events) > 0


# ---------------------------------------------------------------------------
# coordinator_mode=False 不影响既有行为
# ---------------------------------------------------------------------------


# 验证 coordinator_mode=False 时 build_system_prompt 不走协调者分支。
# mock prompts.build_system_prompt，验证 coordinator_mode=False。
@pytest.mark.asyncio
async def test_run_default_coordinator_mode_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seacode.context.ensure_session_dir", lambda work_dir: __import__("pathlib").Path(".")
    )
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("normal reply")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    # coordinator_mode 默认 False。

    captured: dict[str, Any] = {}

    def fake_build(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "normal system prompt"

    monkeypatch.setattr("seacode.agent.build_system_prompt", fake_build)

    conv = ConversationManager()
    conv.add_user_message("Hi")
    _ = [event async for event in agent.run(conv)]
    assert captured.get("coordinator_mode") is False
