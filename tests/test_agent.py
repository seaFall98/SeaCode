from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from seacode.agent import (
    Agent,
    LoopComplete,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
)
from seacode.client import (
    LLMClient,
    StreamComplete,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    ToolCallStart,
)
from seacode.conversation import ConversationManager, Message
from seacode.tools import ToolRegistry
from seacode.tools.base import Tool, ToolCategory, ToolResult


# 可控返回结果或抛出异常的测试工具，支持自定义名称以区分多工具场景。
class _MockParams(BaseModel):
    input: str = ""


class _MockTool(Tool):
    description = "Mock tool for agent tests."
    params_model = _MockParams
    category = ToolCategory.READ

    def __init__(
        self,
        name: str = "MockTool",
        result: ToolResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self._result = result or ToolResult(content="mock output")
        self._error = error

    async def execute(self, params: BaseModel) -> ToolResult:
        if self._error is not None:
            raise self._error
        return self._result


# 带必填参数的工具，用于验证参数校验失败路径。
class _StrictParams(BaseModel):
    required_input: str


class _StrictTool(Tool):
    name = "StrictTool"
    description = "Tool with required params."
    params_model = _StrictParams
    category = ToolCategory.READ

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(content="strict ok")


# 按回合返回预设事件序列或抛出异常的假客户端，不连接真实 Provider。
class _FakeClient(LLMClient):
    def __init__(
        self, outcomes: list[list[StreamEvent] | Exception]
    ) -> None:
        self._outcomes = outcomes
        self.requests: list[tuple[Message, ...]] = []
        self.tools_passed: list[list[dict[str, Any]] | None] = []

    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system
        self.requests.append(tuple(messages))
        self.tools_passed.append(tools)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for event in outcome:
            yield event


# 构造单次工具调用流事件序列，含 start/complete 与完成事件。
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


# 构造纯文本回复流事件序列。
def _text_stream(text: str) -> list[StreamEvent]:
    return [TextDelta(text), StreamComplete(input_tokens=1, output_tokens=1)]


# 收集 agent.run 的全部事件，便于断言事件序列。
async def _collect(agent_run: Any) -> list[Any]:
    return [event async for event in agent_run]


# 构造含一个工具的注册中心与对话，供多测试复用。
def _setup(tool: Tool) -> tuple[Agent, ConversationManager, _FakeClient]:
    registry = ToolRegistry()
    registry.register(tool)
    client = _FakeClient([])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conversation = ConversationManager()
    return agent, conversation, client


# ---------------------------------------------------------------------------
# 纯对话路径
# ---------------------------------------------------------------------------


# 验证无工具调用的纯对话路径直接以最终回复结束。
# 假客户端返回文本流，断言事件序列含 StreamText 与 LoopComplete 且只消耗一次请求。
@pytest.mark.asyncio
async def test_plain_conversation_completes_without_tools() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("Hello there")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("Hi")

    events = await _collect(agent.run(conversation, system="sys"))

    event_types = [type(e) for e in events]
    assert StreamText in event_types
    assert isinstance(events[-1], LoopComplete)
    assert events[-1].total_turns == 1
    assert len(client.requests) == 1
    assert client.tools_passed[0] == []
    assert [m.role for m in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[-1].content == "Hello there"


# ---------------------------------------------------------------------------
# 单轮工具闭环
# ---------------------------------------------------------------------------


# 验证单次工具调用后结果回灌并在第二轮给出最终回复。
# 第一流含工具调用，第二流含文本，断言事件含 ToolUse、ToolResult、TurnComplete 与 LoopComplete。
@pytest.mark.asyncio
async def test_single_tool_call_round_trip() -> None:
    tool = _MockTool(name="MockTool", result=ToolResult(content="file body"))
    agent, conversation, client = _setup(tool)
    client._outcomes = [
        _tool_call_stream("c1", "MockTool", {"input": "path"}),
        _text_stream("Done"),
    ]
    conversation.add_user_message("Read the file")

    events = await _collect(agent.run(conversation, system="sys"))

    event_types = [type(e) for e in events]
    assert ToolUseEvent in event_types
    assert ToolResultEvent in event_types
    assert TurnComplete in event_types
    assert isinstance(events[-1], LoopComplete)
    assert events[-1].total_turns == 2

    tool_result_event = next(e for e in events if isinstance(e, ToolResultEvent))
    assert tool_result_event.is_error is False
    assert tool_result_event.output == "file body"

    assert len(conversation.messages) == 4
    assert conversation.messages[1].tool_uses[0].tool_name == "MockTool"
    assert conversation.messages[2].tool_results[0].content == "file body"
    assert conversation.messages[3].content == "Done"


# ---------------------------------------------------------------------------
# 多工具顺序执行
# ---------------------------------------------------------------------------


# 验证多个工具调用按声明顺序执行并回灌结果。
# 第一流含两个工具调用，断言 ToolResultEvent 顺序与调用顺序一致。
@pytest.mark.asyncio
async def test_multiple_tool_calls_execute_in_order() -> None:
    tool_a = _MockTool(name="ToolA", result=ToolResult(content="result-a"))
    tool_b = _MockTool(name="ToolB", result=ToolResult(content="result-b"))
    registry = ToolRegistry()
    registry.register(tool_a)
    registry.register(tool_b)
    client = _FakeClient(
        [
            [
                ToolCallStart(tool_name="ToolA", tool_id="c1"),
                ToolCallComplete(
                    tool_id="c1", tool_name="ToolA", arguments={}
                ),
                ToolCallStart(tool_name="ToolB", tool_id="c2"),
                ToolCallComplete(
                    tool_id="c2", tool_name="ToolB", arguments={}
                ),
                StreamComplete(input_tokens=1, output_tokens=1),
            ],
            _text_stream("All done"),
        ]
    )
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("Run both")

    events = await _collect(agent.run(conversation, system="sys"))

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 2
    assert result_events[0].tool_name == "ToolA"
    assert result_events[0].output == "result-a"
    assert result_events[1].tool_name == "ToolB"
    assert result_events[1].output == "result-b"


# ---------------------------------------------------------------------------
# 参数校验失败
# ---------------------------------------------------------------------------


# 验证工具参数校验失败转为 is_error=True 且不中断回合。
# 用必填参数工具传入空参数，断言 ToolResultEvent 为错误并继续到最终回复。
@pytest.mark.asyncio
async def test_parameter_validation_failure_does_not_break_turn() -> None:
    tool = _StrictTool()
    agent, conversation, client = _setup(tool)
    client._outcomes = [
        _tool_call_stream("c1", "StrictTool", {}),
        _text_stream("Recovered"),
    ]
    conversation.add_user_message("Run strict")

    events = await _collect(agent.run(conversation, system="sys"))

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].is_error is True
    assert "validation" in result_events[0].output.lower()
    assert isinstance(events[-1], LoopComplete)
    assert conversation.messages[-1].content == "Recovered"


# ---------------------------------------------------------------------------
# 工具异常
# ---------------------------------------------------------------------------


# 验证工具执行异常转为 is_error=True 且不中断回合。
# 工具 execute 抛出运行时异常，断言结果为错误并继续到最终回复。
@pytest.mark.asyncio
async def test_tool_execution_exception_does_not_break_turn() -> None:
    tool = _MockTool(
        name="MockTool", error=RuntimeError("boom in tool")
    )
    agent, conversation, client = _setup(tool)
    client._outcomes = [
        _tool_call_stream("c1", "MockTool", {"input": "x"}),
        _text_stream("Recovered"),
    ]
    conversation.add_user_message("Run failing tool")

    events = await _collect(agent.run(conversation, system="sys"))

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].is_error is True
    assert "boom in tool" in result_events[0].output
    assert isinstance(events[-1], LoopComplete)
    assert conversation.messages[-1].content == "Recovered"


# ---------------------------------------------------------------------------
# 第二次模型请求失败
# ---------------------------------------------------------------------------


# 验证第二次模型请求失败时异常传播且不完整回合不写入后续消息。
# 第一流含工具调用，第二流抛异常，断言异常传播且对话只含第一轮的已提交消息。
@pytest.mark.asyncio
async def test_second_request_failure_propagates_and_keeps_committed_turn() -> None:
    tool = _MockTool(name="MockTool", result=ToolResult(content="ok"))
    agent, conversation, client = _setup(tool)
    client._outcomes = [
        _tool_call_stream("c1", "MockTool", {"input": "x"}),
        RuntimeError("model provider failed"),
    ]
    conversation.add_user_message("Run tool then fail")

    with pytest.raises(RuntimeError, match="model provider failed"):
        await _collect(agent.run(conversation, system="sys"))

    # 第一轮的助手消息与工具结果已提交，第二轮未写入任何消息。
    assert len(conversation.messages) == 3
    assert conversation.messages[0].role == "user"
    assert conversation.messages[0].content == "Run tool then fail"
    assert conversation.messages[1].role == "assistant"
    assert conversation.messages[1].tool_uses[0].tool_name == "MockTool"
    assert conversation.messages[2].role == "user"
    assert conversation.messages[2].tool_results[0].content == "ok"


# ---------------------------------------------------------------------------
# 未知工具
# ---------------------------------------------------------------------------


# 验证调用未注册工具时转为 is_error=True 且不中断回合。
# 模型请求一个未注册工具，断言结果为错误并继续到最终回复。
@pytest.mark.asyncio
async def test_unknown_tool_returns_error_result_without_breaking() -> None:
    tool = _MockTool(name="MockTool")
    agent, conversation, client = _setup(tool)
    client._outcomes = [
        _tool_call_stream("c1", "NonExistent", {}),
        _text_stream("Recovered"),
    ]
    conversation.add_user_message("Call unknown")

    events = await _collect(agent.run(conversation, system="sys"))

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].is_error is True
    assert "unknown tool" in result_events[0].output
    assert isinstance(events[-1], LoopComplete)
