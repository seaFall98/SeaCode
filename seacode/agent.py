"""完整 Agent Loop：消费 LLM 流、流式执行工具、回灌结果，直到命中停止条件。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
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
from .context import (
    CompactBoundary,
    CompactCircuitBreaker,
    CompactEvent,
    ContentReplacementState,
    RecoveryState,
    append_replacement_records,
    apply_tool_result_budget,
    auto_compact,
    create_replacement_state,
    ensure_session_dir,
)
from .conversation import (
    ConversationManager,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .mcp import ConnectResult, MCPManager
from .memory.auto_memory import MemoryManager
from .memory.consolidation import MemoryConsolidator
from .memory.recall import (
    SelectorFn,
    find_relevant_memories,
    render_reminder,
)
from .permissions import PermissionChecker, PermissionMode
from .permissions.rules import Rule, extract_content
from .prompts import build_environment_context, build_system_prompt
from .tools import ToolRegistry
from .tools.base import MAX_OUTPUT_CHARS, ToolResult
from .tools.tool_search import ToolSearchTool

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


@dataclass
class MCPConnectEvent:
    """MCP 服务器批量连接完成事件；携带连接摘要供 TUI 状态栏展示。"""

    server_count: int
    tool_count: int
    errors: list[str]


@dataclass
class CompactNotification:
    """Layer 2 压缩完成事件；携带压缩前 token 数与可选结构化边界供 TUI/session 层消费。"""

    before_tokens: int
    message: str
    # 结构化 boundary（摘要 + 原文保留尾部），UI/session 层用它持久化 compact_boundary 记录。
    # 失败路径下为 None。
    boundary: CompactBoundary | None = None


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
    | MCPConnectEvent
    | CompactNotification
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
    # 缓存用量分量，供 record_usage_anchor 计算 baseline 使用。
    cache_read: int = 0
    cache_creation: int = 0

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
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation


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
        mcp_manager: MCPManager | None = None,
        context_window: int = 200_000,
        instructions_content: str = "",
        memory_manager: MemoryManager | None = None,
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
        # MCP 管理器；为 None 时跳过 MCP 连接与延迟工具搜索。
        self.mcp_manager = mcp_manager
        # MCP 连接是否已完成；run() 首轮触发一次，避免重复连接。
        self._mcp_connected = False
        # 注册 ToolSearchTool 让模型可发现延迟加载的 MCP 工具；
        # 传入 registry 引用形成运行时循环，通过 TYPE_CHECKING 注解避免导入循环。
        if mcp_manager is not None:
            self.registry.register(
                ToolSearchTool(registry=self.registry, protocol=protocol)
            )
        # 上下文治理：context_window 用于阈值判断；session_dir 隔离落盘文件；
        # compact_breaker 防止压缩连续失败卡死；replacement_state 保证 prompt cache 前缀稳定；
        # recovery_state 记录文件读取快照，压缩后重新附加到摘要 user 消息。
        self.context_window = context_window
        self.session_dir: Path = ensure_session_dir(work_dir)
        self.compact_breaker = CompactCircuitBreaker()
        self.replacement_state: ContentReplacementState = create_replacement_state()
        self.recovery_state: RecoveryState = RecoveryState()
        # 会话记录路径只作为字符串拼进压缩后摘要提示，本步不读写会话 JSONL（第 08 步交付）。
        self._transcript_path: str = ""
        # 跨会话记忆：instructions_content 是 load_instructions 的拼接结果，
        # memory_manager 提供 MEMORY.md 索引加载与裸 LLM 提取能力。
        # 为 None 时跳过长期记忆注入与提取（向后兼容 batch01-07 行为）。
        self.instructions_content = instructions_content
        self.memory_manager = memory_manager
        # 记忆提取合并策略（对齐 v1 既定 inProgress + pendingContext）：
        # _extracting: 标记是否有提取正在进行
        # _pending_extraction: 提取期间又触发了新请求，标记需要尾随提取
        self._extracting = False
        self._pending_extraction = False
        # 记忆整理器：仅在 memory_manager 装配时启用；门控由 consolidator 自管。
        self._consolidator: MemoryConsolidator | None = None
        if memory_manager is not None:
            self._consolidator = MemoryConsolidator(work_dir)
        # 当前会话 ID；由 app.py 在创建/恢复会话时设置，仅用于 transcript_path 提示。
        self.session_id: str = ""
        # batch10：Skill 系统。
        # active_skills 记录已激活 Skill 的 SOP（用于 /skill 查看与压缩恢复）。
        # skill_catalog 是 Skill 目录摘要文本（name: description 列表），注入环境上下文。
        # skill_loader 由 app.py 注入，供 /skill 命令与 LoadSkill 工具访问。
        self.active_skills: dict[str, str] = {}
        self.skill_catalog: str = ""
        self.skill_loader: Any = None

    # 切换权限模式；同步更新 permission_checker.mode 保持一致。
    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    # 返回当前是否处于 Plan 模式；供 ExitPlanMode 工具与 TUI 状态查询使用。
    @property
    def plan_mode(self) -> bool:
        return self.permission_mode == PermissionMode.PLAN

    # batch10：激活 Skill，把 SOP 存入 active_skills 供压缩恢复与 /skill 查看。
    def activate_skill(self, name: str, prompt: str) -> None:
        self.active_skills[name] = prompt

    # batch10：设置 Skill 目录摘要文本，每轮注入环境上下文。
    def set_skill_catalog(self, text: str) -> None:
        self.skill_catalog = text

    # batch10：注入 SkillLoader 引用，供 /skill 命令与 LoadSkill 工具访问。
    def set_skill_loader(self, loader: Any) -> None:
        self.skill_loader = loader

    # 为 HITL 确认生成人类可读的工具操作描述。
    def _build_permission_description(self, tc: ToolCallComplete) -> str:
        return PermissionChecker.describe_tool_action(tc.tool_name, tc.arguments)

    # 连接所有 MCP 服务器并注册工具；把服务器 instructions 注入对话历史。
    # 单 Server 失败由 MCPManager 收集到 errors，不阻断其它 Server 与主流程。
    async def _setup_mcp(self, conversation: ConversationManager) -> ConnectResult:
        assert self.mcp_manager is not None
        result = await self.mcp_manager.register_all_tools(self.registry)

        # 把每个成功连接的 Server 的 instructions 注入对话历史，供模型参考。
        for server in result.servers:
            if server.instructions:
                conversation.add_system_reminder(
                    f"MCP server '{server.name}' instructions:\n{server.instructions}"
                )

        return result

    # 执行 Agent 主循环：注入环境 → 长期记忆注入 → MCP 连接
    # → 每轮 prompt → 模型流 → 工具执行 → 回灌。
    async def run(
        self, conversation: ConversationManager
    ) -> AsyncIterator[AgentEvent]:
        # 会话启动时注入会话级环境上下文（position 0，env_injected 标记只注入一次）。
        env_context = build_environment_context(self.work_dir)
        conversation.inject_environment(env_context)

        # 长期记忆注入：load_instructions 拼接的指令 + MEMORY.md 索引内容 + 当前日期，
        # 整体 <system-reminder> 包裹后插在 env 之后。ltm_injected 标记只注入一次。
        # memory_manager 为 None 时跳过（向后兼容 batch01-07 行为）。
        memory_content = self.memory_manager.load() if self.memory_manager else ""
        conversation.inject_long_term_memory(self.instructions_content, memory_content)

        # MCP 连接：首轮启动时一次性连接所有 Server，注册工具并注入 instructions。
        # 单 Server 失败不阻断其它；连接结果通过 MCPConnectEvent 通知 TUI 展示摘要。
        if self.mcp_manager is not None and not self._mcp_connected:
            self._mcp_connected = True
            connect_result = await self._setup_mcp(conversation)
            yield MCPConnectEvent(
                server_count=len(connect_result.servers),
                tool_count=len(connect_result.tools),
                errors=connect_result.errors,
            )

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
            # mcp_manager 装配时插入 ToolSearch 段落，引导模型先发现再调用 MCP 工具。
            # skill_catalog 非空时注入 # Skills 段落，让模型知道可用 Skill。
            system = build_system_prompt(
                work_dir=self.work_dir,
                mcp_enabled=self.mcp_manager is not None,
                skill_section=self.skill_catalog,
            )

            # 延迟工具名 reminder：每轮注入未发现的延迟工具名，引导模型用 ToolSearch 发现。
            # mark_discovered 后下一轮 get_all_schemas 包含完整 Schema，此 reminder 自动缩短。
            deferred_names = self.registry.get_deferred_tool_names()
            if deferred_names:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query \"select:<name>[,<name>...]\" to load tool schemas '
                    "before calling them:\n"
                    + "\n".join(deferred_names)
                )

            # 每轮重新获取工具 Schema，使 mark_discovered 后的新工具立即纳入。
            tools = self.registry.get_all_schemas(self.protocol)

            # Layer 1：工具结果预算——就地修改 conversation 中超限的 ToolResultBlock.content，
            # 替换为落盘预览。返回本轮新产生的替换记录，追加到 jsonl 支持 resume 重建。
            new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if new_records:
                append_replacement_records(self.session_dir, new_records)

            # Layer 2：接近 context window 上限时自动 compact。
            # tool-result budget 已就地修改 conversation，直接用 conversation.history 估算。
            # 压缩成功后重新注入环境并重新 apply budget（保留的尾部 tool_result 仍可能超限）。
            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self.registry.get_all_schemas(self.protocol),
                transcript_path=self._transcript_path,
            )
            if isinstance(compact_result, CompactEvent):
                yield CompactNotification(
                    before_tokens=compact_result.before_tokens,
                    message=f"上下文已压缩（压缩前 {compact_result.before_tokens:,} tokens）",
                    boundary=compact_result.boundary,
                )
                # 压缩后重新注入环境上下文（replace_history 已重置 env_injected）。
                conversation.inject_environment(env_context)
                # 压缩后重新注入长期记忆（replace_history 已重置 ltm_injected）。
                # 重新加载 MEMORY.md 以反映整理或新增记忆后的最新索引。
                mem = self.memory_manager.load() if self.memory_manager else ""
                conversation.inject_long_term_memory(self.instructions_content, mem)
                # 压缩后重新应用 budget，保证尾部保留的 tool_result 也走预算替换。
                apply_tool_result_budget(
                    conversation, self.session_dir, self.replacement_state
                )
            elif isinstance(compact_result, str):
                yield ErrorEvent(message=compact_result)

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
                # Loop 结束时异步触发记忆提取与整理门控检查。
                # 提取用裸 LLM 调用不带工具，合并策略见 _extract_memories。
                # 整理门控由 consolidator 自管，5 级门控全通过才 fork 子 Agent。
                # 两者都不阻塞主循环，失败静默不影响用户回复。
                if self.memory_manager is not None:
                    asyncio.ensure_future(self._extract_memories(conversation))
                if self._consolidator is not None:
                    asyncio.ensure_future(
                        self._consolidator.maybe_run(
                            self.client, conversation, self.protocol
                        )
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
            # 在 assistant 回复加入历史后锚定实际用量：基准值（input + cache_read +
            # cache_creation + output）覆盖到当前位置，因此下一轮迭代顶部的 auto-compact
            # 检查只需对接下来追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            # 收集流式执行器中已提交的工具结果（工具在 LLM 流式输出期间已开始执行）。
            tool_results: list[ToolResultBlock] = []
            streaming_results = await executor.collect_results()

            for br in streaming_results:
                if br.is_unknown:
                    consecutive_unknown += 1
                else:
                    consecutive_unknown = 0
                # 即时落盘：超过 MAX_OUTPUT_CHARS 的输出持久化到磁盘，对话里只保留预览。
                content = self._maybe_persist_or_truncate(br.tool_id, br.result.content)
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=br.tool_id,
                        content=content,
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
                # 即时落盘：延迟执行的工具结果同样走 MAX_OUTPUT_CHARS 即时落盘。
                content = self._maybe_persist_or_truncate(tc.tool_id, result.content)
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=tc.tool_id,
                        content=content,
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

    # 工具结果进入历史前的即时落盘：超过 MAX_OUTPUT_CHARS 的输出持久化到 session 目录，
    # 对话里只保留 <persisted-output> 预览。这是 Layer 1 的早期拦截线，发生在
    # apply_tool_result_budget 的预算替换之前；未被即时落盘的结果（如流式执行器收集
    # 的结果在收集阶段未走此路径）由循环顶部的预算替换兜底。
    def _maybe_persist_or_truncate(self, tool_use_id: str, text: str) -> str:
        from seacode.context.manager import make_persisted_preview, persist_tool_result

        if len(text) > MAX_OUTPUT_CHARS:
            fp = persist_tool_result(tool_use_id, text, self.session_dir)
            return make_persisted_preview(text, fp)
        return text

    # -----------------------------------------------------------------
    # 跨会话记忆：提取合并策略 + 召回选择器注入点
    # -----------------------------------------------------------------

    # 触发记忆提取，使用 _extracting + _pending_extraction 双标志合并。
    # 正在提取时新触发只标记 pending，当前提取完成后检查 pending 并尾随执行一次，
    # 防止多个触发器同时执行导致重复提取污染索引。
    async def _extract_memories(self, conversation: ConversationManager) -> None:
        if not self.memory_manager:
            return

        # 合并策略：正在提取时暂存新请求，等当前提取完成后尾随执行。
        if self._extracting:
            self._pending_extraction = True
            return

        self._extracting = True
        try:
            await self.memory_manager.extract(
                self.client, conversation, self.protocol
            )
        except Exception:
            # 提取失败不阻塞主循环，下次 LoopComplete 会再次触发。
            pass
        finally:
            self._extracting = False
            # 检查是否有尾随提取请求；若有则递归处理。
            if self._pending_extraction:
                self._pending_extraction = False
                await self._extract_memories(conversation)

    # 把 LLM 客户端封装为 recall 模块使用的异步 SelectorFn。
    # SelectorFn 接收 (system_prompt, user_message) 返回 LLM 原始输出文本；
    # 这里调用 client.stream 裸流式调用并累积 TextDelta。
    def _make_memory_selector(self) -> SelectorFn:
        from seacode.client import StreamComplete, TextDelta

        async def selector(system_prompt: str, user_message: str) -> str:
            from seacode.conversation import Message

            messages = [Message(role="user", content=user_message)]
            collected = ""
            async for event in self.client.stream(messages, system=system_prompt):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamComplete):
                    pass
            return collected

        return selector

    # 召回入口：扫描双目录、调用选择器挑选相关记忆、渲染 reminder。
    # 失败静默返回空串；调用方（app.py）按需注入 system-reminder。
    async def recall_memories(
        self,
        query: str,
        recent_tools: list[str] | None,
        already_surfaced: set[str] | None,
    ) -> str:
        if not self.memory_manager:
            return ""
        user_dir = self.memory_manager.user_mem_dir
        proj_dir = self.memory_manager.project_mem_dir
        memories = await find_relevant_memories(
            query=query,
            user_mem_dir=user_dir if user_dir else None,
            project_mem_dir=proj_dir if proj_dir else None,
            recent_tools=recent_tools,
            already_surfaced=already_surfaced,
            selector=self._make_memory_selector(),
        )
        if not memories:
            return ""
        try:
            return render_reminder(memories)
        except Exception:
            return ""
