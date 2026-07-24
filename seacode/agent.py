"""完整 Agent Loop：消费 LLM 流、流式执行工具、回灌结果，直到命中停止条件。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
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
from .permissions import PermissionChecker, PermissionMode
from .permissions.rules import Rule, extract_content
from .prompts import build_environment_context, build_system_prompt
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


class PermissionResponse(Enum):
    """用户对权限请求的三种回复；由 TUI 通过 future.resolve 传回 Agent。"""

    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "allow_always"


@dataclass
class PermissionRequest:
    """请求用户确认工具执行的事件；携带 future 供 TUI resolve。"""

    tool_name: str
    description: str
    future: asyncio.Future[PermissionResponse]


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
    | PermissionRequest
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
        work_dir: str = ".",
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        # 权限检查器；为 None 时跳过权限检查（向后兼容 batch02-04 行为）。
        self.permission_checker = permission_checker
        # 当前权限模式；同步 permission_checker.mode 避免 dual source of truth。
        self.permission_mode: PermissionMode = (
            permission_checker.mode if permission_checker else PermissionMode.DEFAULT
        )

    # 切换权限模式；同步更新 permission_checker.mode 保持一致。
    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    # 返回当前是否处于 Plan 模式；供 ExitPlanMode 工具与 TUI 状态查询使用。
    @property
    def plan_mode(self) -> bool:
        return self.permission_mode == PermissionMode.PLAN

    # 为 HITL 确认生成人类可读的工具操作描述。
    def _build_permission_description(self, tc: ToolCallComplete) -> str:
        return PermissionChecker.describe_tool_action(tc.tool_name, tc.arguments)

    # 执行 Agent 主循环：注入环境 → 每轮 build_system_prompt → 模型流 → 工具执行 → 回灌。
    async def run(
        self, conversation: ConversationManager
    ) -> AsyncIterator[AgentEvent]:
        # 会话启动时注入会话级环境上下文（position 0，env_injected 标记只注入一次）。
        env_context = build_environment_context(self.work_dir)
        conversation.inject_environment(env_context)

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

            # 每轮动态拼装系统提示词，包含 Environment 段落与条件段落。
            system = build_system_prompt(work_dir=self.work_dir)

            collector = StreamCollector()
            executor = StreamingExecutor()
            deferred_tool_calls: list[ToolCallComplete] = []

            messages = conversation.get_messages()
            llm_stream = self.client.stream(messages, system=system, tools=tools)

            # 流式消费：文本/思考增量转发，工具调用完整后立即提交执行。
            async for event in collector.consume(llm_stream):
                if isinstance(event, ToolUseEvent):
                    tc = collector.response.tool_calls[-1]
                    # ask 决策的工具延迟到流后顺序执行（需要 HITL 同步）；
                    # allow/deny 立即提交，deny 在 _execute_single_tool_direct 内处理。
                    tool = self.registry.get(tc.tool_name)
                    needs_ask = False
                    if tool and self.permission_checker:
                        decision = self.permission_checker.check(tool, tc.arguments)
                        needs_ask = decision.effect == "ask"
                    if needs_ask:
                        deferred_tool_calls.append(tc)
                    else:
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

            # 延迟工具（需要交互式权限确认）：顺序执行，yield PermissionRequest 等待 HITL 回复。
            # ask 工具需要 HITL 同步，不能并发；并发路径在第 06 步 MCP 后启用。
            for tc in deferred_tool_calls:
                result: ToolResult | None = None
                elapsed = 0.0
                is_unknown = False

                async for item in self._execute_tool_with_permission(tc):
                    if isinstance(item, PermissionRequest):
                        yield item
                    else:
                        result, elapsed, is_unknown = item

                if result is None:
                    result = ToolResult(
                        content="Error: no result from tool", is_error=True
                    )

                if is_unknown:
                    consecutive_unknown += 1
                else:
                    consecutive_unknown = 0
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

            # 停止条件 3：连续未知工具调用达到上限。
            if consecutive_unknown >= _CONSECUTIVE_UNKNOWN_LIMIT:
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls"
                )
                break

            conversation.add_tool_results_message(tool_results)
            yield TurnComplete(turn=iteration)

    # 直接执行单个工具调用，返回结构化结果与耗时；未知/禁用工具返回错误结果。
    # 权限 deny 决策在此处直接转为错误结果；ask 决策由 _execute_tool_with_permission 处理。
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

        # 权限检查：deny 直接返回错误结果；allow 继续执行。
        # ask 决策不应进入此路径（由 run 主循环延迟到 deferred_tool_calls）。
        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)
            if decision.effect == "deny":
                return _ToolExecResult(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    result=ToolResult(
                        content=f"Permission denied: {decision.reason}",
                        is_error=True,
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

    # 执行需 HITL 确认的工具调用；yield PermissionRequest 等待 TUI 回复后继续。
    # yield 顺序：PermissionRequest（ask 时）→ (result, elapsed, is_unknown) 元组。
    async def _execute_tool_with_permission(
        self, tc: ToolCallComplete
    ) -> AsyncIterator[PermissionRequest | tuple[ToolResult, float, bool]]:
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()

        if tool is None:
            yield ToolResult(
                content=f"Error: unknown tool '{tc.tool_name}'", is_error=True
            ), time.monotonic() - start, True
            return

        if not self.registry.is_enabled(tc.tool_name):
            yield ToolResult(
                content=f"Error: tool '{tc.tool_name}' is disabled", is_error=True
            ), time.monotonic() - start, False
            return

        # 权限检查：deny 直接错误回灌；ask yield PermissionRequest 等待 HITL 回复。
        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)
            if decision.effect == "deny":
                yield ToolResult(
                    content=f"Permission denied: {decision.reason}", is_error=True
                ), time.monotonic() - start, False
                return

            if decision.effect == "ask":
                loop = asyncio.get_running_loop()
                future: asyncio.Future[PermissionResponse] = loop.create_future()
                desc = self._build_permission_description(tc)
                yield PermissionRequest(
                    tool_name=tc.tool_name,
                    description=desc,
                    future=future,
                )
                response = await future

                if response == PermissionResponse.DENY:
                    yield ToolResult(
                        content="Permission denied: 用户拒绝了此操作",
                        is_error=True,
                    ), time.monotonic() - start, False
                    return

                # ALLOW_ALWAYS：写入本地规则文件 + 加入会话级放行集合，本轮立即生效。
                if response == PermissionResponse.ALLOW_ALWAYS:
                    content = extract_content(tc.tool_name, tc.arguments)
                    pattern = (
                        f"{content[:60]}*" if len(content) > 60 else f"{content}*"
                    )
                    rule = Rule(
                        tool_name=tc.tool_name, pattern=pattern, effect="allow"
                    )
                    self.permission_checker.rule_engine.append_local_rule(rule)
                    self.permission_checker.add_session_allow(tc.tool_name, content)

        try:
            params = tool.params_model.model_validate(tc.arguments)
            result = await tool.execute(params)
        except ValidationError as e:
            result = ToolResult(
                content=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(content=f"Tool execution error: {e}", is_error=True)

        yield result, time.monotonic() - start, False

    # 并发执行一批工具调用；第 06 步 MCP 延迟工具的并发路径会消费。
    async def _execute_batch_parallel(
        self, calls: list[ToolCallComplete]
    ) -> list[_ToolExecResult]:
        tasks = [self._execute_single_tool_direct(tc) for tc in calls]
        return list(await asyncio.gather(*tasks))
