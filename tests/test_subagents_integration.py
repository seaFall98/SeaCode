"""子 Agent 集成测试：覆盖 Agent 字段、run_to_completion 与环境上下文集成。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic import BaseModel

from seacode.agent import Agent
from seacode.client import (
    LLMClient,
    StreamComplete,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    ToolCallStart,
)
from seacode.conversation import ConversationManager, Message
from seacode.prompts import build_environment_context
from seacode.tools import ToolRegistry
from seacode.tools.base import Tool, ToolCategory, ToolResult

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


# 可按回合返回预设事件序列的假客户端，不连接真实 Provider。
class _FakeClient(LLMClient):
    def __init__(self, outcomes: list[list[StreamEvent]]) -> None:
        self._outcomes = outcomes
        self.requests: list[tuple[Message, ...]] = []
        self.systems_passed: list[str] = []

    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del tools
        self.requests.append(tuple(messages))
        self.systems_passed.append(system)
        outcome = self._outcomes.pop(0)
        for event in outcome:
            yield event


# 构造纯文本回复流事件序列。
def _text_stream(text: str, input_tokens: int = 10, output_tokens: int = 5) -> list[StreamEvent]:
    return [TextDelta(text), StreamComplete(input_tokens=input_tokens, output_tokens=output_tokens)]


# 构造单次工具调用流事件序列。
def _tool_call_stream(
    tool_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> list[StreamEvent]:
    return [
        ToolCallStart(tool_name=tool_name, tool_id=tool_id),
        ToolCallComplete(
            tool_id=tool_id,
            tool_name=tool_name,
            arguments=arguments or {},
        ),
        StreamComplete(input_tokens=1, output_tokens=1),
    ]


# 假工具参数模型；避免直接用 BaseModel 无法生成 JSON schema。
class _FakeToolParams(BaseModel):
    input: str = ""


# 假工具：返回固定结果供 run_to_completion 工具调用路径测试。
class _FakeTool(Tool):
    description = "fake tool"
    params_model = _FakeToolParams
    category = ToolCategory.READ

    def __init__(self, name: str = "FakeTool", result: str = "tool result") -> None:
        self.name = name
        self._result = result

    async def execute(self, params: BaseModel) -> ToolResult:
        del params
        return ToolResult(content=self._result)


# 构造含一个工具的 Agent 与注册中心。
def _make_agent(
    client: _FakeClient,
    *,
    tools: list[Tool] | None = None,
    agent_id: str | None = None,
    parent_id: str | None = None,
    trace_id: str | None = None,
    team_name: str | None = None,
    max_iterations: int = 100,
) -> Agent:
    registry = ToolRegistry()
    for tool in tools or []:
        registry.register(tool)
    return Agent(
        client=client,
        registry=registry,
        protocol="anthropic",
        max_iterations=max_iterations,
        agent_id=agent_id,
        parent_id=parent_id,
        trace_id=trace_id,
        team_name=team_name,
    )


# ---------------------------------------------------------------------------
# agent_id / parent_id / trace_id 字段
# ---------------------------------------------------------------------------


# 验证 Agent 构造时 agent_id 自动生成 12 字符 hex。
# 传 agent_id=None，断言 agent_id 长度 12 且是十六进制。
def test_agent_auto_generates_agent_id() -> None:
    client = _FakeClient([])
    agent = _make_agent(client)
    assert len(agent.agent_id) == 12
    int(agent.agent_id, 16)


# 验证 Agent 构造时 parent_id 与 trace_id 正确设置。
# 传 parent_id="parent1" 与 trace_id="trace1"，断言字段持有传入值。
def test_agent_parent_id_and_trace_id_set() -> None:
    client = _FakeClient([])
    agent = _make_agent(client, parent_id="parent1", trace_id="trace1")
    assert agent.parent_id == "parent1"
    assert agent.trace_id == "trace1"


# 验证 Agent 构造时传入自定义 agent_id 保留。
# 传 agent_id="custom-id"，断言 agent_id == "custom-id"。
def test_agent_custom_agent_id_preserved() -> None:
    client = _FakeClient([])
    agent = _make_agent(client, agent_id="custom-id-12")
    assert agent.agent_id == "custom-id-12"


# 验证多个子 Agent 同 trace_id 但 agent_id 不同。
# 构造 2 个 Agent 同 trace_id，断言 trace_id 相同且 agent_id 不同。
def test_multiple_agents_same_trace_different_agent_id() -> None:
    client = _FakeClient([])
    agent1 = _make_agent(client, trace_id="trace1")
    agent2 = _make_agent(client, trace_id="trace1")
    assert agent1.trace_id == agent2.trace_id
    assert agent1.agent_id != agent2.agent_id


# ---------------------------------------------------------------------------
# run_to_completion 非交互执行
# ---------------------------------------------------------------------------


# 验证 run_to_completion 无工具调用时返回最终文本。
# fake client 返回纯文本流，断言返回值与 last_output 一致。
async def test_run_to_completion_returns_final_text() -> None:
    client = _FakeClient([_text_stream("fixed text")])
    agent = _make_agent(client)

    result = await agent.run_to_completion("do task")

    assert result == "fixed text"
    assert agent.last_output == "fixed text"


# 验证 run_to_completion 累计 input/output tokens。
# fake client 返回 input=10/output=5，断言 total 累计。
async def test_run_to_completion_accumulates_tokens() -> None:
    client = _FakeClient([_text_stream("done", input_tokens=100, output_tokens=50)])
    agent = _make_agent(client)

    await agent.run_to_completion("do task")

    assert agent.total_input_tokens == 100
    assert agent.total_output_tokens == 50


# 验证 run_to_completion 工具调用后继续循环到最终回复。
# 第一轮返回工具调用，第二轮返回文本，断言工具被调用且最终返回文本。
async def test_run_to_completion_with_tool_call_continues_loop() -> None:
    tool = _FakeTool(name="ReadFile", result="file content")
    client = _FakeClient(
        [
            _tool_call_stream("tc1", "ReadFile", {"file_path": "test.txt"}),
            _text_stream("done with 1 tool uses"),
        ]
    )
    agent = _make_agent(client, tools=[tool])

    result = await agent.run_to_completion("read file")

    assert result == "done with 1 tool uses"
    # 应发起两轮 LLM 请求。
    assert len(client.requests) == 2


# 验证 run_to_completion 复用传入 conversation 时仍注入本次非空 task。
# 传入预填充 conversation，断言请求历史同时含原消息与一次 new task。
async def test_run_to_completion_uses_passed_conversation() -> None:
    client = _FakeClient([_text_stream("result")])
    agent = _make_agent(client)
    conv = ConversationManager()
    conv.add_user_message("previous context")

    await agent.run_to_completion("new task", conversation=conv)

    # 传入的 conversation 应被使用；请求历史应含预填充消息。
    assert len(client.requests) == 1
    # 第一条消息应是预填充的 "previous context"。
    assert client.requests[0][0].content == "previous context"
    assert [message.content for message in client.requests[0]].count("new task") == 1


# 验证 run_to_completion max_iterations 限制循环次数。
# max_iterations=1，fake client 持续返回工具调用，断言只发起 1 轮请求。
async def test_run_to_completion_respects_max_iterations() -> None:
    tool = _FakeTool(name="ReadFile")
    client = _FakeClient(
        [
            _tool_call_stream("tc1", "ReadFile"),
            _tool_call_stream("tc2", "ReadFile"),
        ]
    )
    agent = _make_agent(client, tools=[tool], max_iterations=1)

    result = await agent.run_to_completion("task")

    # max_iterations=1 只允许 1 轮，工具调用后停止。
    assert len(client.requests) == 1
    # last_output 在工具调用路径可能为空（无最终文本）。
    del result


# 验证 run_to_completion 无 task 时不添加空 user message。
# 传 task=""，fake client 返回文本，断言不抛异常。
async def test_run_to_completion_empty_task_does_not_crash() -> None:
    client = _FakeClient([_text_stream("ok")])
    agent = _make_agent(client)

    result = await agent.run_to_completion("")

    assert result == "ok"


# ---------------------------------------------------------------------------
# team_name / _team_manager 字段保留
# ---------------------------------------------------------------------------


# 验证 Agent team_name 字段保留但不路由。
# 构造 team_name="team1"，断言字段持有且运行一轮不抛异常。
async def test_agent_team_name_preserved_but_not_routed() -> None:
    client = _FakeClient([_text_stream("done")])
    agent = _make_agent(client, team_name="team1")

    assert agent.team_name == "team1"
    # 运行一轮不抛异常（不调用团队协调）。
    result = await agent.run_to_completion("task")
    assert result == "done"


# 验证 Agent _team_manager 默认 None。
# 构造 Agent 不传 team_manager，断言 _team_manager is None。
def test_agent_default_team_manager_none() -> None:
    client = _FakeClient([])
    agent = _make_agent(client)
    assert agent._team_manager is None


# ---------------------------------------------------------------------------
# set_agent_catalog 与 build_environment_context
# ---------------------------------------------------------------------------


# 验证 set_agent_catalog 保存 catalog 与 catalog_list。
# 调用 set_agent_catalog，断言 _agent_catalog 与 _agent_catalog_list 持有值。
def test_set_agent_catalog_stores_values() -> None:
    client = _FakeClient([])
    agent = _make_agent(client)
    catalog = "## Available Sub-Agent Types\n- Explore: 探索"
    catalog_list = [("Explore", "探索"), ("Plan", "规划")]

    agent.set_agent_catalog(catalog, catalog_list)

    assert agent._agent_catalog == catalog
    assert agent._agent_catalog_list == catalog_list


# 验证 set_agent_catalog 默认空 catalog。
# 构造 Agent 不调用 set_agent_catalog，断言 _agent_catalog 为空串。
def test_agent_default_catalog_empty() -> None:
    client = _FakeClient([])
    agent = _make_agent(client)
    assert agent._agent_catalog == ""
    assert agent._agent_catalog_list == []


# 验证 build_environment_context 接受 agent_catalog 参数注入。
# 调用 build_environment_context 传 agent_catalog，断言输出含 catalog 内容。
def test_build_environment_context_includes_agent_catalog() -> None:
    catalog = "## Available Sub-Agent Types\n- Explore: 探索"
    context = build_environment_context(
        work_dir="/test", agent_catalog=catalog
    )

    assert "## Available Sub-Agent Types" in context
    assert "Explore" in context
    assert "/test" in context


# 验证 build_environment_context 不传 agent_catalog 时不含子 Agent 段落。
# 调用 build_environment_context 不传 agent_catalog，断言输出不含 "Available Sub-Agent"。
def test_build_environment_context_without_catalog() -> None:
    context = build_environment_context(work_dir="/test")

    assert "Available Sub-Agent" not in context
    assert "/test" in context


# 验证 build_environment_context 含 skill_catalog 与 agent_catalog 双段落。
# 同时传两个 catalog，断言输出含两段内容。
def test_build_environment_context_with_both_catalogs() -> None:
    agent_catalog = "## Available Sub-Agent Types\n- Explore: 探索"
    skill_catalog = "## Available Skills\n- test: 测试"
    context = build_environment_context(
        work_dir="/test",
        agent_catalog=agent_catalog,
        skill_catalog=skill_catalog,
    )

    assert "Available Sub-Agent Types" in context
    assert "Available Skills" in context


# ---------------------------------------------------------------------------
# last_output 字段
# ---------------------------------------------------------------------------


# 验证 Agent last_output 默认空串。
# 构造 Agent 不运行，断言 last_output == ""。
def test_agent_default_last_output_empty() -> None:
    client = _FakeClient([])
    agent = _make_agent(client)
    assert agent.last_output == ""


# 验证 run_to_completion 设置 last_output。
# 运行后断言 last_output 与返回值一致。
async def test_run_to_completion_sets_last_output() -> None:
    client = _FakeClient([_text_stream("final answer")])
    agent = _make_agent(client)

    result = await agent.run_to_completion("task")

    assert agent.last_output == result
    assert agent.last_output == "final answer"


# ---------------------------------------------------------------------------
# set_full_registry
# ---------------------------------------------------------------------------


# 验证 set_full_registry 保存引用。
# 调用 set_full_registry，断言 _full_registry 持有传入值。
def test_set_full_registry_stores_reference() -> None:
    client = _FakeClient([])
    agent = _make_agent(client)
    registry = ToolRegistry()

    agent.set_full_registry(registry)

    assert agent._full_registry is registry


# 验证 Agent 默认 _full_registry 为 None。
# 构造 Agent 不调用 set_full_registry，断言 _full_registry is None。
def test_agent_default_full_registry_none() -> None:
    client = _FakeClient([])
    agent = _make_agent(client)
    assert agent._full_registry is None
