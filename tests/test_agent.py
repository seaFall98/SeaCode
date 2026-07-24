from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from seacode.agent import (
    Agent,
    ErrorEvent,
    LoopComplete,
    RetryEvent,
    StreamingExecutor,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
    _ToolExecResult,
)
from seacode.client import (
    LLMClient,
    StreamComplete,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallStart,
)
from seacode.conversation import ConversationManager, Message
from seacode.tools import ToolRegistry, partition_tool_calls
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
        concurrency_safe: bool = False,
    ) -> None:
        self.name = name
        self._result = result or ToolResult(content="mock output")
        self._error = error
        self.is_concurrency_safe = concurrency_safe

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
        self.systems_passed: list[str] = []
        self.max_output_tokens_calls: list[int] = []

    # 记录 max_tokens 升级恢复对上限的调整，供测试断言。
    def set_max_output_tokens(self, n: int) -> None:
        self.max_output_tokens_calls.append(n)

    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(tuple(messages))
        self.systems_passed.append(system)
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

    events = await _collect(agent.run(conversation))

    event_types = [type(e) for e in events]
    assert StreamText in event_types
    assert isinstance(events[-1], LoopComplete)
    assert events[-1].total_turns == 1
    assert len(client.requests) == 1
    assert client.tools_passed[0] == []
    # inject_environment 在 position 0 插入环境上下文，故角色序列为 user/env、user/Hi、assistant。
    assert [m.role for m in conversation.messages] == ["user", "user", "assistant"]
    assert "Current working directory" in conversation.messages[0].content
    assert conversation.messages[1].content == "Hi"
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

    events = await _collect(agent.run(conversation))

    event_types = [type(e) for e in events]
    assert ToolUseEvent in event_types
    assert ToolResultEvent in event_types
    assert TurnComplete in event_types
    assert isinstance(events[-1], LoopComplete)
    assert events[-1].total_turns == 2

    tool_result_event = next(e for e in events if isinstance(e, ToolResultEvent))
    assert tool_result_event.is_error is False
    assert tool_result_event.output == "file body"

    # inject_environment 在 position 0 插入环境上下文消息，故用户消息索引为 1。
    assert len(conversation.messages) == 5
    assert conversation.messages[0].role == "user"  # 环境上下文
    assert conversation.messages[1].content == "Read the file"
    assert conversation.messages[2].tool_uses[0].tool_name == "MockTool"
    assert conversation.messages[3].tool_results[0].content == "file body"
    assert conversation.messages[4].content == "Done"


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

    events = await _collect(agent.run(conversation))

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

    events = await _collect(agent.run(conversation))

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

    events = await _collect(agent.run(conversation))

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
        await _collect(agent.run(conversation))

    # 第一轮的助手消息与工具结果已提交，第二轮未写入任何消息。
    # inject_environment 在 position 0 插入环境上下文，故用户消息索引为 1。
    assert len(conversation.messages) == 4
    assert conversation.messages[0].role == "user"  # 环境上下文
    assert conversation.messages[1].role == "user"
    assert conversation.messages[1].content == "Run tool then fail"
    assert conversation.messages[2].role == "assistant"
    assert conversation.messages[2].tool_uses[0].tool_name == "MockTool"
    assert conversation.messages[3].role == "user"
    assert conversation.messages[3].tool_results[0].content == "ok"


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

    events = await _collect(agent.run(conversation))

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].is_error is True
    assert "unknown tool" in result_events[0].output
    assert isinstance(events[-1], LoopComplete)


# ---------------------------------------------------------------------------
# max_iterations 上限停止
# ---------------------------------------------------------------------------


# 验证达到 max_iterations 上限时发射 ErrorEvent 并停止循环。
# 设置 max_iterations=2 且模型连续返回工具调用，断言第 3 次迭代被上限拦截。
@pytest.mark.asyncio
async def test_max_iterations_limit_emits_error_event() -> None:
    tool = _MockTool(name="MockTool")
    registry = ToolRegistry()
    registry.register(tool)
    outcomes = [
        _tool_call_stream("c1", "MockTool", {"input": "x"}),
        _tool_call_stream("c2", "MockTool", {"input": "x"}),
        _tool_call_stream("c3", "MockTool", {"input": "x"}),
    ]
    client = _FakeClient(outcomes)
    agent = Agent(
        client=client, registry=registry, protocol="anthropic", max_iterations=2
    )
    conversation = ConversationManager()
    conversation.add_user_message("Loop forever")

    events = await _collect(agent.run(conversation))

    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert "maximum iterations" in error_events[0].message
    assert "2" in error_events[0].message
    # 上限拦截发生在第 3 次迭代发起请求之前，因此只会有 2 次模型请求。
    assert len(client.requests) == 2


# ---------------------------------------------------------------------------
# 连续未知工具停止
# ---------------------------------------------------------------------------


# 验证连续 3 次未知工具调用触发 ErrorEvent 并停止循环。
# 三轮均调用未注册工具名，断言第 3 轮回灌前停止并发射错误。
@pytest.mark.asyncio
async def test_consecutive_unknown_tools_stops_loop() -> None:
    tool = _MockTool(name="MockTool")
    registry = ToolRegistry()
    registry.register(tool)
    outcomes = [
        _tool_call_stream("c1", "UnknownA", {}),
        _tool_call_stream("c2", "UnknownB", {}),
        _tool_call_stream("c3", "UnknownC", {}),
    ]
    client = _FakeClient(outcomes)
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("Call unknowns")

    events = await _collect(agent.run(conversation))

    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert "too many consecutive unknown" in error_events[0].message
    assert len(client.requests) == 3


# ---------------------------------------------------------------------------
# max_tokens 两阶段恢复
# ---------------------------------------------------------------------------


# 验证 max_tokens 首次截断提升上限、注入续写指令并发射 RetryEvent。
# 首轮流文本被截断，断言 set_max_output_tokens(64000) 调用与续写用户消息注入。
@pytest.mark.asyncio
async def test_max_tokens_escalation_first_stage() -> None:
    tool = _MockTool(name="MockTool")
    agent, conversation, client = _setup(tool)
    client._outcomes = [
        [
            TextDelta("Partial answer"),
            StreamComplete(input_tokens=1, output_tokens=1, stop_reason="max_tokens"),
        ],
        _text_stream("Done"),
    ]
    conversation.add_user_message("Hit limit")

    events = await _collect(agent.run(conversation))

    retry_events = [e for e in events if isinstance(e, RetryEvent)]
    assert len(retry_events) == 1
    assert retry_events[0].reason == "max_tokens escalation"
    assert client.max_output_tokens_calls == [64000]
    # 续写指令作为新用户消息注入，且位于截断助手消息之后。
    # inject_environment 在 position 0 插入环境上下文，故助手消息索引为 2、续写指令为 3。
    assert "Resume" in conversation.messages[3].content
    assert conversation.messages[2].content == "Partial answer"
    assert isinstance(events[-1], LoopComplete)


# 验证 max_tokens 再次截断进入 recovery 分支，注入拆小指令并发射 RetryEvent。
# 首次 escalation 后再次截断，断言 recovery 1/3 与拆小指令注入。
@pytest.mark.asyncio
async def test_max_tokens_recovery_branch_injects_break_instruction() -> None:
    tool = _MockTool(name="MockTool")
    agent, conversation, client = _setup(tool)
    client._outcomes = [
        [StreamComplete(input_tokens=1, output_tokens=1, stop_reason="max_tokens")],
        [
            TextDelta("Partial two"),
            StreamComplete(input_tokens=1, output_tokens=1, stop_reason="max_tokens"),
        ],
        _text_stream("Done"),
    ]
    conversation.add_user_message("Hit limit twice")

    events = await _collect(agent.run(conversation))

    retry_events = [e for e in events if isinstance(e, RetryEvent)]
    assert [r.reason for r in retry_events] == [
        "max_tokens escalation",
        "max_tokens recovery 1/3",
    ]
    # recovery 注入的拆小指令位于第二轮截断助手消息之后。
    # inject_environment 在 position 0 插入环境上下文，故 Partial two 索引为 2、Break 指令为 3。
    assert "Break" in conversation.messages[3].content
    assert conversation.messages[2].content == "Partial two"
    assert isinstance(events[-1], LoopComplete)


# ---------------------------------------------------------------------------
# partition_tool_calls 切批
# ---------------------------------------------------------------------------


# 验证 partition_tool_calls 把连续并发安全工具合并为并发批，不安全工具独立串行批。
# 构造 [Read, Read, Edit, Read, Read]，断言切为三批且写工具隔离。
def test_partition_tool_calls_batches_concurrent_and_serial() -> None:
    read_tool = _MockTool(name="ReadFile", concurrency_safe=True)
    edit_tool = _MockTool(name="EditFile", concurrency_safe=False)
    registry = ToolRegistry()
    registry.register(read_tool)
    registry.register(edit_tool)

    names = ["ReadFile", "ReadFile", "EditFile", "ReadFile", "ReadFile"]
    calls = [
        ToolCallComplete(tool_id=str(i), tool_name=name, arguments={})
        for i, name in enumerate(names)
    ]
    batches = partition_tool_calls(calls, registry)

    assert len(batches) == 3
    assert batches[0].concurrent is True
    assert [tc.tool_name for tc in batches[0].calls] == ["ReadFile", "ReadFile"]
    assert batches[1].concurrent is False
    assert [tc.tool_name for tc in batches[1].calls] == ["EditFile"]
    assert batches[2].concurrent is True
    assert [tc.tool_name for tc in batches[2].calls] == ["ReadFile", "ReadFile"]


# 验证禁用工具即使并发安全也降级为串行批次。
# 注册并发安全工具但禁用它，断言切出的批次 concurrent=False。
def test_partition_tool_calls_disables_fall_back_to_serial() -> None:
    read_tool = _MockTool(name="ReadFile", concurrency_safe=True)
    registry = ToolRegistry()
    registry.register(read_tool)
    registry.disable("ReadFile")

    calls = [ToolCallComplete(tool_id="0", tool_name="ReadFile", arguments={})]
    batches = partition_tool_calls(calls, registry)

    assert len(batches) == 1
    assert batches[0].concurrent is False


# ---------------------------------------------------------------------------
# StreamingExecutor 顺序与异常隔离
# ---------------------------------------------------------------------------


# 验证 StreamingExecutor 按提交顺序汇总结果，单个异常转为错误结果不炸整批。
# 提交三个协程，中间一个抛异常，断言顺序保持且异常转为 is_error 结果。
@pytest.mark.asyncio
async def test_streaming_executor_preserves_order_and_isolates_exceptions() -> None:
    executor = StreamingExecutor()

    async def succeed(order: int) -> _ToolExecResult:
        return _ToolExecResult(
            tool_id=f"t{order}",
            tool_name="X",
            result=ToolResult(content=f"r{order}"),
            elapsed=0.0,
            is_unknown=False,
        )

    async def fail() -> _ToolExecResult:
        raise RuntimeError("boom in executor")

    executor.submit(succeed(1))
    executor.submit(fail())
    executor.submit(succeed(3))

    results = await executor.collect_results()

    assert len(results) == 3
    assert results[0].tool_id == "t1"
    assert results[1].result.is_error is True
    assert "Tool execution error" in results[1].result.content
    assert results[2].tool_id == "t3"


# ---------------------------------------------------------------------------
# UsageEvent 累计
# ---------------------------------------------------------------------------


# 验证 UsageEvent 跨多轮累计 input/output tokens。
# 两轮流分别消耗 5/7 与 3/4，断言最后一轮累计为 8/11。
@pytest.mark.asyncio
async def test_usage_event_accumulates_tokens_across_turns() -> None:
    tool = _MockTool(name="MockTool")
    agent, conversation, client = _setup(tool)
    client._outcomes = [
        [
            ToolCallStart(tool_name="MockTool", tool_id="c1"),
            ToolCallComplete(tool_id="c1", tool_name="MockTool", arguments={}),
            StreamComplete(input_tokens=5, output_tokens=7),
        ],
        [TextDelta("Done"), StreamComplete(input_tokens=3, output_tokens=4)],
    ]
    conversation.add_user_message("Run")

    events = await _collect(agent.run(conversation))

    usage_events = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usage_events) == 2
    assert usage_events[-1].input_tokens == 8
    assert usage_events[-1].output_tokens == 11


# ---------------------------------------------------------------------------
# ThinkingText 转发
# ---------------------------------------------------------------------------


# 验证 thinking 增量转发为 ThinkingText 事件，最终文本与思考并存。
# 假流交替发射 thinking 与 text 增量，断言 ThinkingText 拼接完整。
@pytest.mark.asyncio
async def test_thinking_text_event_forwarded() -> None:
    registry = ToolRegistry()
    client = _FakeClient(
        [
            [
                ThinkingDelta("Hello "),
                ThinkingDelta("world."),
                TextDelta("Answer"),
                StreamComplete(input_tokens=1, output_tokens=1),
            ]
        ]
    )
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("Think")

    events = await _collect(agent.run(conversation))

    thinking_events = [e for e in events if isinstance(e, ThinkingText)]
    assert "".join(e.text for e in thinking_events) == "Hello world."
    assert isinstance(events[-1], LoopComplete)


# ---------------------------------------------------------------------------
# 用户取消
# ---------------------------------------------------------------------------


# 验证 task.cancel() 触发 CancelledError 自然退出 agent.run 生成器。
# 用阻塞流让生成器等待释放信号，取消任务后断言 CancelledError 传播。
@pytest.mark.asyncio
async def test_cancellation_exits_generator() -> None:
    class _BlockingClient(LLMClient):
        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def stream(
            self,
            messages: Sequence[Message],
            system: str,
            tools: list[dict[str, Any]] | None = None,
        ) -> AsyncIterator[StreamEvent]:
            del messages, system, tools
            await self.release.wait()
            yield TextDelta("Done")
            yield StreamComplete(input_tokens=1, output_tokens=1)

    client = _BlockingClient()
    registry = ToolRegistry()
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("Block then cancel")

    task = asyncio.create_task(_collect(agent.run(conversation)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# batch04：build_system_prompt 与 inject_environment 集成
# ---------------------------------------------------------------------------


# 验证会话启动时 inject_environment 在 position 0 注入环境上下文。
# 假客户端返回纯文本流，断言首条消息为 user 角色且含 "Current working directory"。
@pytest.mark.asyncio
async def test_inject_environment_inserts_context_at_head() -> None:
    registry = ToolRegistry()
    client = _FakeClient([_text_stream("Hello")])
    agent = Agent(client=client, registry=registry, protocol="anthropic", work_dir="/custom")
    conversation = ConversationManager()
    conversation.add_user_message("Hi")

    await _collect(agent.run(conversation))

    assert len(conversation.messages) >= 2
    assert conversation.messages[0].role == "user"
    assert "Current working directory: /custom" in conversation.messages[0].content
    assert conversation.env_injected is True


# 验证每轮 build_system_prompt 被调用且返回包含 Environment 段落。
# 假客户端记录 system 参数，断言 system 含 "# Environment" 与工作目录。
@pytest.mark.asyncio
async def test_build_system_prompt_called_each_turn_with_environment() -> None:
    tool = _MockTool(name="MockTool", result=ToolResult(content="ok"))
    agent, conversation, client = _setup(tool)
    agent.work_dir = "/custom-workdir"
    client._outcomes = [
        _tool_call_stream("c1", "MockTool", {"input": "x"}),
        _text_stream("Done"),
    ]
    conversation.add_user_message("Run")

    await _collect(agent.run(conversation))

    # 两轮流均调用 build_system_prompt，system 含 Environment 段落与工作目录。
    assert len(client.systems_passed) == 2
    for system in client.systems_passed:
        assert "# Environment" in system
        assert "Working directory: /custom-workdir" in system
        assert "You are SeaCode" in system


# 验证 inject_environment 只注入一次。
# 连续两次 agent.run 复用同一 conversation，断言 env_injected 标记后不再插入。
@pytest.mark.asyncio
async def test_inject_environment_only_once_across_runs() -> None:
    registry = ToolRegistry()
    client = _FakeClient(
        [_text_stream("First"), _text_stream("Second")]
    )
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("First")

    await _collect(agent.run(conversation))
    first_count = len(conversation.messages)
    # 第二次 run 时 env_injected 已为 True，不再重复注入。
    conversation.add_user_message("Second")
    await _collect(agent.run(conversation))
    # 第二次 run 只新增助手消息与用户消息，不再插入环境上下文。
    assert conversation.env_injected is True
    # 第一次 run 后 3 条；第二次 run 后 5 条（env 已注入不再重复）。
    assert len(conversation.messages) == first_count + 2


# 验证 replace_history 重置 env_injected 允许重新注入。
# 注入环境后 replace_history，断言 env_injected 为 False 且可再次注入。
@pytest.mark.asyncio
async def test_replace_history_resets_env_injected() -> None:
    from seacode.conversation import Message as ConvMessage

    registry = ToolRegistry()
    client = _FakeClient([_text_stream("Hello")])
    agent = Agent(client=client, registry=registry, protocol="anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("Hi")
    await _collect(agent.run(conversation))
    assert conversation.env_injected is True

    # 模拟第 07 步压缩后 replace_history，env_injected 应重置。
    conversation.replace_history([ConvMessage(role="user", content="summarized")])
    assert conversation.env_injected is False

    # 再次 run 时应重新注入环境上下文。
    client._outcomes = [_text_stream("After compact")]
    await _collect(agent.run(conversation))
    assert conversation.env_injected is True
    assert "Current working directory" in conversation.messages[0].content
