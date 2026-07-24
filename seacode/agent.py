"""单轮工具调度器：消费 LLM 流、执行工具、回灌结果直至模型给出最终回复。"""

from __future__ import annotations

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
from .tools import ToolRegistry
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
    """整个单轮调度循环完成，模型已给出最终回复。"""

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
# 单轮调度器
# ---------------------------------------------------------------------------


# 单轮调度最大迭代次数，防止模型陷入工具调用循环。
_MAX_ITERATIONS: int = 10


class Agent:
    """单轮工具调度器：发起 LLM 调用、执行工具、回灌结果直至模型给出最终回复。"""

    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        protocol: str,
        max_iterations: int = _MAX_ITERATIONS,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.max_iterations = max_iterations
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # 执行单轮调度循环：用户消息 → 模型流 → 工具执行 → 结果回灌 → 最终回复。
    async def run(
        self, conversation: ConversationManager, system: str
    ) -> AsyncIterator[AgentEvent]:
        tools = self.registry.get_all_schemas(self.protocol)
        iteration = 0

        while True:
            iteration += 1
            if iteration > self.max_iterations:
                yield ErrorEvent(
                    message=f"Agent reached maximum iterations ({self.max_iterations})"
                )
                break

            collector = StreamCollector()
            messages = conversation.get_messages()
            llm_stream = self.client.stream(messages, system=system, tools=tools)
            async for event in collector.consume(llm_stream):
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

            # 无工具调用：模型给出最终回复，提交并完成整个单轮调度。
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

            tool_results: list[ToolResultBlock] = []
            for tc in response.tool_calls:
                result, elapsed = await self._execute_tool(tc)
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=tc.tool_id,
                        content=result.content,
                        is_error=result.is_error,
                    )
                )
                yield ToolResultEvent(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    output=result.content,
                    is_error=result.is_error,
                    elapsed=elapsed,
                )

            conversation.add_tool_results_message(tool_results)
            yield TurnComplete(turn=iteration)

    # 执行单个工具调用，返回结构化结果与耗时；未知/禁用工具返回错误结果。
    async def _execute_tool(self, tc: ToolCallComplete) -> tuple[ToolResult, float]:
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()

        if tool is None:
            return (
                ToolResult(
                    content=f"Error: unknown tool '{tc.tool_name}'", is_error=True
                ),
                time.monotonic() - start,
            )

        if not self.registry.is_enabled(tc.tool_name):
            return (
                ToolResult(
                    content=f"Error: tool '{tc.tool_name}' is disabled", is_error=True
                ),
                time.monotonic() - start,
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

        return result, time.monotonic() - start
