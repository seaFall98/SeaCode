"""Agent 与 FileHistory 集成测试：覆盖 file_history 字段、make_snapshot 调用点与 work_dir 切换。"""

from __future__ import annotations

from typing import Any

import pytest

from seacode.agent import Agent
from seacode.client import LLMClient, StreamComplete, StreamEvent, TextDelta
from seacode.conversation import ConversationManager
from seacode.tools import ToolRegistry

# ---------------------------------------------------------------------------
# 测试辅助 fake 类
# ---------------------------------------------------------------------------


# 假 LLMClient：按预设事件序列返回流，记录请求次数。
class _FakeClient(LLMClient):
    def __init__(self, streams: list[list[StreamEvent]]) -> None:
        self._streams = list(streams)
        self.requests: int = 0

    def stream(
        self, messages: Any, system: str = "", tools: Any = None
    ) -> Any:
        idx = self.requests
        self.requests += 1

        async def _gen() -> Any:
            for event in self._streams[idx]:
                yield event

        return _gen()


# 构造纯文本回复流事件序列。
def _text_stream(text: str) -> list[StreamEvent]:
    return [TextDelta(text), StreamComplete(input_tokens=1, output_tokens=1)]


# 假 FileHistory：记录 make_snapshot 调用参数。
class _FakeFileHistory:
    def __init__(self) -> None:
        self.snapshots: list[tuple[int, str]] = []

    def make_snapshot(self, msg_index: int, user_text: str) -> None:
        self.snapshots.append((msg_index, user_text))


# 收集 agent.run 的全部事件。
async def _collect(agent_run: Any) -> list[Any]:
    return [event async for event in agent_run]


# ---------------------------------------------------------------------------
# T12：file_history 字段与 make_snapshot 调用
# ---------------------------------------------------------------------------


# 验证 Agent.file_history 默认为 None。
# 不传 file_history 构造 Agent，断言 .file_history is None。
def test_agent_file_history_defaults_to_none() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("hi")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")

    assert agent.file_history is None


# 验证 Agent.file_history 可外部注入并保留引用。
# 构造 Agent 后注入 FakeFileHistory，断言 agent.file_history 是同一实例。
def test_agent_file_history_can_be_injected() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("hi")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    fh = _FakeFileHistory()

    agent.file_history = fh

    assert agent.file_history is fh


# 验证 run() 在用户回合起点调用 make_snapshot(len(history), user_text)。
# 预置 user 消息后 run，断言 snapshots 收到一条记录，
# msg_index 等于历史长度，user_text 为最后一条 user 消息内容。
@pytest.mark.asyncio
async def test_run_calls_make_snapshot_with_user_text() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("done")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    fh = _FakeFileHistory()
    agent.file_history = fh
    conv = ConversationManager()
    conv.add_user_message("hello world")

    await _collect(agent.run(conv))

    assert len(fh.snapshots) == 1
    msg_index, user_text = fh.snapshots[0]
    # add_user_message 后历史长度为 1，snapshot 在 run 起点（注入 env 之前）调用。
    assert msg_index == 1
    assert user_text == "hello world"


# 验证连续多次 run 都触发 make_snapshot，msg_index 随历史增长递增。
# 第一次 run 后再 add_user_message 触发第二次 run，断言 snapshots 长度为 2 且 msg_index 递增。
@pytest.mark.asyncio
async def test_run_calls_make_snapshot_each_turn() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("first"), _text_stream("second")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    fh = _FakeFileHistory()
    agent.file_history = fh
    conv = ConversationManager()
    conv.add_user_message("turn1")

    await _collect(agent.run(conv))
    conv.add_user_message("turn2")
    await _collect(agent.run(conv))

    assert len(fh.snapshots) == 2
    # 第一次：历史只有 turn1，长度 1。
    assert fh.snapshots[0] == (1, "turn1")
    # 第二次：历史有 env + user1 + assistant1 + user2，长度 >= 3。
    # msg_index 反映注入 env 后的真实历史长度。
    assert fh.snapshots[1][1] == "turn2"
    assert fh.snapshots[1][0] >= 3


# 验证 file_history 为 None 时跳过 make_snapshot 不抛异常。
# 不注入 file_history 直接 run，断言 run 正常完成无异常。
@pytest.mark.asyncio
async def test_run_skips_snapshot_when_file_history_none() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("ok")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conv = ConversationManager()
    conv.add_user_message("test")

    # 不应抛异常。
    events = await _collect(agent.run(conv))

    assert agent.file_history is None
    # 至少有一条 LoopComplete 事件表示正常完成。
    assert any(e.__class__.__name__ == "LoopComplete" for e in events)


# 验证 make_snapshot 抛异常时不阻塞主循环。
# 注入会抛异常的 FakeFileHistory，断言 run 仍能完成并产出 LoopComplete。
@pytest.mark.asyncio
async def test_run_tolerates_make_snapshot_failure() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("ok")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")

    class _BoomFileHistory:
        def make_snapshot(self, msg_index: int, user_text: str) -> None:
            raise RuntimeError("boom")

    agent.file_history = _BoomFileHistory()
    conv = ConversationManager()
    conv.add_user_message("test")

    events = await _collect(agent.run(conv))

    # 异常被吞掉，run 正常完成。
    assert any(e.__class__.__name__ == "LoopComplete" for e in events)


# 验证 work_dir 是可写属性，切换后后续工具调用使用新路径。
# 构造 Agent 后赋值新 work_dir，断言 agent.work_dir 反映新值。
def test_agent_work_dir_is_writable() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("hi")])
    agent = Agent(
        client=client, registry=registry, protocol="anthropic", work_dir="."
    )

    agent.work_dir = "/tmp/new-workdir"

    assert agent.work_dir == "/tmp/new-workdir"


# 验证无 user 消息时 make_snapshot 以空字符串调用而非抛异常。
# 空 conversation 直接 run，断言 snapshots 收到 (0, "") 或不抛异常。
@pytest.mark.asyncio
async def test_run_make_snapshot_with_empty_conversation() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("ok")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    fh = _FakeFileHistory()
    agent.file_history = fh
    conv = ConversationManager()

    await _collect(agent.run(conv))

    # 无 user 消息时 user_text 为空串；msg_index 为历史长度 0。
    assert len(fh.snapshots) == 1
    assert fh.snapshots[0] == (0, "")
