"""完整 Agent Loop：消费 LLM 流、流式执行工具、回灌结果，直到命中停止条件。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .client import (
    LLMClient,
    StreamComplete,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)
from .conversation import (
    ConversationManager,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .tools import ToolRegistry, partition_tool_calls
from .tools.base import ToolResult

# ---------------------------------------------------------------------------
# AgentEvent 事件类型
# ---------------------------------------------------------------------------


@dataclass
class StreamText:
    """一段助手正文文本增量。"""

    text: str


@dataclass
class ThinkingText:
    """一段思考文本增量。"""

    text: str


@dataclass
class RetryEvent:
    """max_tokens 截断恢复或限流重试时发射，UI 展示重试提示。"""

    reason: str
    wait: float = 0.0


@dataclass
class ToolUseEvent:
    """模型请求一次工具调用。"""

    tool_name: str
    tool_id: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent:
    """工具执行结果。"""

    tool_id: str
    tool_name: str
    output: str
    is_error: bool
    elapsed: float


@dataclass
class TurnComplete:
    """一轮模型交互完成（含工具调用与结果回灌）。"""

    turn: int


@dataclass
class LoopComplete:
    """整个 Agent Loop 结束，模型已给出最终回复或命中停止条件。"""

    total_turns: int


@dataclass
class UsageEvent:
    """累积用量更新。"""

    input_tokens: int
    output_tokens: int


@dataclass
class ErrorEvent:
    """调度过程中遇到的错误。"""

    message: str


type AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
    | UsageEvent
    | ErrorEvent
)


# ---------------------------------------------------------------------------
# LLM 响应收集器
# ---------------------------------------------------------------------------


@dataclass
class _ThinkingBlockCollected:
    """流式收集的思考块中间表示。"""

    thinking: str
    signature: str


@dataclass
class LLMResponse:
    """一次 LLM 流的完整收集结果。"""

    text: str = ""
    tool_calls: list[ToolCallComplete] = field(default_factory=list)
    thinking_blocks: list[_ThinkingBlockCollected] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class StreamCollector:
    """消费 LLM 流事件并累积成完整响应，同时转发给上层 UI。"""

    def __init__(self) -> None:
        self.response = LLMResponse()

    # 消费流事件：文本与思考增量立即转发，工具调用与完成事件累积后转发。
    async def consume(
        self, stream: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[AgentEvent]:
        async for event in stream:
            if isinstance(event, TextDelta):
                self.response.text += event.text
                yield StreamText(text=event.text)
            elif isinstance(event, ThinkingDelta):
                yield ThinkingText(text=event.text)
            elif isinstance(event, ThinkingComplete):
                self.response.thinking_blocks.append(
                    _ThinkingBlockCollected(
                        thinking=event.thinking, signature=event.signature
                    )
                )
            elif isinstance(event, ToolCallStart):
                # 工具调用增量不转发，等 complete 时统一发出。
                pass
            elif isinstance(event, ToolCallDelta):
                pass
            elif isinstance(event, ToolCallComplete):
                self.response.tool_calls.append(event)
                yield ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_id=event.tool_id,
                    arguments=event.arguments,
                )
            elif isinstance(event, StreamComplete):
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens


# ---------------------------------------------------------------------------
# 流式工具执行器 — 在 LLM streaming 期间启动 tool 执行
# ---------------------------------------------------------------------------


@dataclass
class _ToolExecResult:
    """单次工具执行的完整结果，含未知工具标记用于连续未知工具停止判断。"""

    tool_id: str
    tool_name: str
    result: ToolResult
    elapsed: float
    is_unknown: bool


class StreamingExecutor:
    """在 LLM 流式输出期间并发提交工具执行，按提交顺序汇总结果。"""

    def __init__(self) -> None:
        self._tasks: list[tuple[int, asyncio.Task[_ToolExecResult]]] = []
        self._order = 0

    # 提交一个工具执行协程，立即开始并发执行。
    def submit(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.append((self._order, task))
        self._order += 1

    # 按提交顺序汇总所有已提交任务的结果，异常转为错误结果。
    async def collect_results(self) -> list[_ToolExecResult]:
        if not self._tasks:
            return []
        tasks = [t for _, t in sorted(self._tasks, key=lambda x: x[0])]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[_ToolExecResult] = []
        for r in results:
            if isinstance(r, BaseException):
                out.append(
                    _ToolExecResult(
                        tool_id="",
                        tool_name="",
                        result=ToolResult(
                            content=f"Tool execution error: {r}", is_error=True
                        ),
                        elapsed=0.0,
                        is_unknown=False,
                    )
                )
            else:
                out.append(r)
        return out


# ---------------------------------------------------------------------------
# Agent 主循环
# ---------------------------------------------------------------------------

# Agent Loop 默认最大迭代次数，防止模型陷入工具调用循环。
_DEFAULT_MAX_ITERATIONS: int = 100

# max_tokens 截断恢复的两阶段上限。
MAX_TOKENS_CEILING: int = 64000
MAX_OUTPUT_TOKENS_RECOVERIES: int = 3

# 连续未知工具调用次数达到此阈值时停止循环。
_CONSECUTIVE_UNKNOWN_LIMIT: int = 3


class Agent:
    """完整 Agent Loop：发起 LLM 调用、流式执行工具、回灌结果直至命中停止条件。"""

    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        protocol: str,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.max_iterations = max_iterations
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # 执行 Agent 主循环：用户消息 → 模型流 → 流式工具执行 → 结果回灌 → 直到停止。
    async def run(
        self, conversation: ConversationManager, system: str
    ) -> AsyncIterator[AgentEvent]:
        tools = self.registry.get_all_schemas(self.protocol)
        iteration = 0
        consecutive_unknown = 0
        max_tokens_escalated = False
        output_recoveries = 0

        while True:
            iteration += 1

            # 停止条件 1：迭代上限。
            if self.max_iterations > 0 and iteration > self.max_iterations:
                yield ErrorEvent(
                    message=f"Agent reached maximum iterations ({self.max_iterations})"
                )
                break

            collector = StreamCollector()
            executor = StreamingExecutor()
            deferred_tool_calls: list[ToolCallComplete] = []

            messages = conversation.get_messages()
            llm_stream = self.client.stream(messages, system=system, tools=tools)

            # 流式消费：文本/思考增量转发，工具调用完整后立即提交执行。
            async for event in collector.consume(llm_stream):
                if isinstance(event, ToolUseEvent):
                    tc = collector.response.tool_calls[-1]
                    # 需交互权限的工具延迟到流后执行；本步无权限系统，始终立即提交。
                    executor.submit(self._execute_single_tool_direct(tc))
                yield event

            response = collector.response

            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens
            yield UsageEvent(
                input_tokens=self.total_input_tokens,
                output_tokens=self.total_output_tokens,
            )

            conv_thinking = [
                ThinkingBlock(thinking=tb.thinking, signature=tb.signature)
                for tb in response.thinking_blocks
            ]

            # max_tokens 截断恢复：两阶段提升上限 + 注入续写指令。
            if response.stop_reason == "max_tokens":
                if not max_tokens_escalated:
                    self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
                    max_tokens_escalated = True
                    if response.text:
                        conversation.add_assistant_message(
                            response.text, thinking_blocks=conv_thinking
                        )
                        conversation.add_user_message(
                            "Output token limit hit. Resume directly from where you stopped. "
                            "Do not apologize or repeat previous content. "
                            "Pick up mid-thought if needed."
                        )
                    yield RetryEvent(reason="max_tokens escalation")
                    continue
                elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:
                    output_recoveries += 1
                    conversation.add_assistant_message(
                        response.text, thinking_blocks=conv_thinking
                    )
                    conversation.add_user_message(
                        "Output token limit hit. Resume directly from where you stopped. "
                        "Break remaining work into smaller pieces."
                    )
                    yield RetryEvent(
                        reason=f"max_tokens recovery {output_recoveries}"
                        f"/{MAX_OUTPUT_TOKENS_RECOVERIES}"
                    )
                    continue
            else:
                output_recoveries = 0

            # 停止条件 2：无工具调用，模型给出最终回复。
            if not response.tool_calls:
                conversation.add_assistant_message(
                    response.text, thinking_blocks=conv_thinking
                )
                yield LoopComplete(total_turns=iteration)
                break

            # 有工具调用：提交助手消息（含工具调用），执行工具，回灌结果。
            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(
                response.text, tool_uses=tool_uses, thinking_blocks=conv_thinking
            )

            # 收集流式执行器中已提交的工具结果（工具在 LLM 流式输出期间已开始执行）。
            tool_results: list[ToolResultBlock] = []
            streaming_results = await executor.collect_results()

            for br in streaming_results:
                if br.is_unknown:
                    consecutive_unknown += 1
                else:
                    consecutive_unknown = 0
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=br.tool_id,
                        content=br.result.content,
                        is_error=br.result.is_error,
                    )
                )
                yield ToolResultEvent(
                    tool_id=br.tool_id,
                    tool_name=br.tool_name,
                    output=br.result.content,
                    is_error=br.result.is_error,
                    elapsed=br.elapsed,
                )

            # 延迟工具（需要交互式权限确认）：按并发安全属性分批执行。
            # 本步无权限系统，此路径为空但保留 partition_tool_calls 调用结构。
            for batch in partition_tool_calls(deferred_tool_calls, self.registry):
                if batch.concurrent:
                    batch_results = await self._execute_batch_parallel(batch.calls)
                else:
                    batch_results = [
                        await self._execute_single_tool_direct(tc) for tc in batch.calls
                    ]
                for br in batch_results:
                    if br.is_unknown:
                        consecutive_unknown += 1
                    else:
                        consecutive_unknown = 0
                    tool_results.append(
                        ToolResultBlock(
                            tool_use_id=br.tool_id,
                            content=br.result.content,
                            is_error=br.result.is_error,
                        )
                    )
                    yield ToolResultEvent(
                        tool_id=br.tool_id,
                        tool_name=br.tool_name,
                        output=br.result.content,
                        is_error=br.result.is_error,
                        elapsed=br.elapsed,
                    )

            # 停止条件 3：连续未知工具调用达到上限。
            if consecutive_unknown >= _CONSECUTIVE_UNKNOWN_LIMIT:
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls"
                )
                break

            conversation.add_tool_results_message(tool_results)
            yield TurnComplete(turn=iteration)

    # 直接执行单个工具调用，返回结构化结果与耗时；未知/禁用工具返回错误结果。
    async def _execute_single_tool_direct(
        self, tc: ToolCallComplete
    ) -> _ToolExecResult:
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()

        if tool is None:
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(
                    content=f"Error: unknown tool '{tc.tool_name}'", is_error=True
                ),
                elapsed=time.monotonic() - start,
                is_unknown=True,
            )

        if not self.registry.is_enabled(tc.tool_name):
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(
                    content=f"Error: tool '{tc.tool_name}' is disabled", is_error=True
                ),
                elapsed=time.monotonic() - start,
                is_unknown=False,
            )

        try:
            params = tool.params_model.model_validate(tc.arguments)
            result = await tool.execute(params)
        except ValidationError as e:
            result = ToolResult(
                content=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(content=f"Tool execution error: {e}", is_error=True)

        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=result,
            elapsed=time.monotonic() - start,
            is_unknown=False,
        )

    # 并发执行一批工具调用，用于 partition_tool_calls 切分的并发批次。
    async def _execute_batch_parallel(
        self, calls: list[ToolCallComplete]
    ) -> list[_ToolExecResult]:
        tasks = [self._execute_single_tool_direct(tc) for tc in calls]
        return list(await asyncio.gather(*tasks))
