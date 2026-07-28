"""完整 Agent Loop：消费 LLM 流、流式执行工具、回灌结果，直到命中停止条件。"""

from __future__ import annotations

import asyncio
import datetime
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from .hooks import HookContext, HookEngine
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
from .prompts import (
    build_environment_context,
    build_plan_mode_reminder,
    build_system_prompt,
)
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


@dataclass
class HookEvent:
    """Hook 执行结果事件；供 TUI 在对话区呈现 Hook [id] OK/FAIL output 状态行。"""

    hook_id: str
    event: str
    output: str
    success: bool


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
    | HookEvent
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


# 按 assistant 发出的 tool_call 顺序排列回灌结果，避免并发、拒绝与延迟路径
# 拼接出的 tool_result 顺序被兼容 Chat Completions provider 拒绝。
def _order_tool_results(
    tool_results: list[ToolResultBlock],
    tool_calls: list[ToolCallComplete],
) -> list[ToolResultBlock]:
    order = {call.tool_id: index for index, call in enumerate(tool_calls)}
    return [
        result
        for _, result in sorted(
            enumerate(tool_results),
            key=lambda item: (order.get(item[1].tool_use_id, len(order)), item[0]),
        )
    ]


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
        hook_engine: HookEngine | None = None,
        # batch12：子 Agent 与任务管理；agent_id/parent_id/trace_id 由 AgentTool 注入，
        # team_name/_team_manager 保留签名但第 14 步才启用真实路由。
        agent_id: str | None = None,
        parent_id: str | None = None,
        trace_id: str | None = None,
        team_name: str | None = None,
        team_manager: Any = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        # 循环计数器：用于记忆提取节流与 /clear、/session 重置。
        self._loop_count = 0
        # 权限检查器；为 None 时跳过权限检查（向后兼容 batch02-04 行为）。
        self.permission_checker = permission_checker
        # 当前权限模式；同步 permission_checker.mode 避免 dual source of truth。
        self.permission_mode: PermissionMode = (
            permission_checker.mode if permission_checker else PermissionMode.DEFAULT
        )
        # MCP 管理器；为 None 时跳过 MCP 连接与延迟工具搜索。
        self.mcp_manager = mcp_manager
        # MCP 初始化状态属于应用会话；新 Agent 读取 manager 状态避免重复连接。
        self._mcp_connected = bool(
            getattr(mcp_manager, "is_initialized", False)
        )
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
        # 记忆召回由应用层预取，工具执行后再把已完成结果注入当前对话。
        self.memory_recall_task: asyncio.Task[str] | None = None
        self._memory_recall_consumed = False
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
        # batch11：生命周期 Hook 引擎；为 None 时所有注入点零开销。
        # 由 __main__.py 在创建 Agent 时注入，或由 app.py 在 _run_turn 中通过
        # set_hook_engine 注入。8 个注入点在 run() 中按生命周期顺序触发。
        self.hook_engine: HookEngine | None = hook_engine
        # batch12：子 Agent 与任务管理字段。
        # agent_id 唯一标识一个 Agent 实例；parent_id 指向父 Agent（子 Agent 场景）；
        # trace_id 标识整条调用链，子 Agent 继承父 Agent 的 trace_id。
        # AgentTool 在实例化子 Agent 后通过 setattr 覆盖这三个字段。
        self.agent_id: str = agent_id or uuid4().hex[:12]
        self.parent_id: str | None = parent_id
        self.trace_id: str | None = trace_id
        # team_name / _team_manager 保留签名，第 14 步启用真实路由。
        self.team_name: str | None = team_name
        self._team_manager: Any = team_manager
        # _full_registry：子 Agent 调度时保存父 Agent 完整工具注册表引用，
        # 供 AgentTool 在 fork/定义式路径克隆或过滤工具使用。主 Agent 由 app.py 注入。
        self._full_registry: Any = None
        # batch14：Coordinator Mode 与团队邮箱消费。
        # coordinator_mode=True 时 build_system_prompt 走协调者提示词分支，工具集收敛为调度-only。
        # notification_fn 由 app.py 注入 team_manager.drain_lead_mailbox，每轮消费 lead 邮箱。
        self.coordinator_mode: bool = False
        self.notification_fn: Callable[[], list[str]] | None = None
        # 子 Agent 系统提示词与目录摘要；定义式子 Agent 由 AgentDef.system_prompt 提供，
        # fork 子 Agent 用 FORK_BOILERPLATE（已包含在 fork_messages 中，不重复注入）。
        # _agent_catalog 是注入到环境上下文的可用子 Agent 摘要文本；
        # _agent_catalog_list 保留 (name, description) 元组列表供运行时查询。
        self._current_definition: Any = None
        self._fork_conversation: Any = None
        self._current_conversation: Any = None
        self._agent_catalog: str = ""
        self._agent_catalog_list: list[tuple[str, str]] = []
        # last_output：run_to_completion 结束时保存最终文本，供 TaskManager 读取。
        self.last_output: str = ""
        # batch13：文件历史快照；app.py 在装配阶段注入，None 时跳过 make_snapshot。
        # 在每轮用户回合起点记录已跟踪文件的当前内容，供 /rewind 回滚。
        self.file_history: Any = None
        # Plan 模式下同一 Agent Loop 复用的计划文件路径。
        self._plan_path_cache: Path | None = None

    # 切换到新会话时重置所有会话级状态，保留 Provider 与工具装配。
    def reset_for_session(
        self,
        *,
        session_id: str,
        work_dir: str,
        file_history: Any = None,
        recovery_state: RecoveryState | None = None,
        compact_breaker: CompactCircuitBreaker | None = None,
        replacement_state: ContentReplacementState | None = None,
        active_skills: dict[str, str] | None = None,
    ) -> None:
        if self.memory_recall_task is not None and not self.memory_recall_task.done():
            self.memory_recall_task.cancel()

        self.work_dir = work_dir
        self.session_dir = ensure_session_dir(work_dir)
        self.session_id = session_id
        self.file_history = file_history
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._loop_count = 0
        self.compact_breaker = compact_breaker or CompactCircuitBreaker()
        self.recovery_state = recovery_state or RecoveryState()
        self.replacement_state = replacement_state or create_replacement_state()
        self.active_skills = active_skills if active_skills is not None else {}
        self.memory_recall_task = None
        self._memory_recall_consumed = False
        self._extracting = False
        self._pending_extraction = False
        self._current_definition = None
        self._fork_conversation = None
        self._current_conversation = None
        self.last_output = ""
        self._plan_path_cache = None
        self._transcript_path = ""
        self.agent_id = uuid4().hex[:12]
        # 新会话需要重新把已连接 MCP 的 instructions 注入新对话；Manager
        # 会复用已建立的连接与工具，不会重复建立外部连接。
        self._mcp_connected = False

    # 切换权限模式；同步更新 permission_checker.mode 保持一致。
    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    # 返回当前是否处于 Plan 模式；供 ExitPlanMode 工具与 TUI 状态查询使用。
    @property
    def plan_mode(self) -> bool:
        return self.permission_mode == PermissionMode.PLAN

    # 为当前 Plan 会话惰性创建唯一计划文件路径。
    def _get_plan_path(self) -> Path:
        if self._plan_path_cache is not None:
            return self._plan_path_cache

        adjectives = (
            "bold", "bright", "calm", "clear", "deep", "fair", "fast", "fine",
            "glad", "keen", "kind", "lean", "mild", "neat", "pure", "safe",
            "slim", "soft", "tall", "warm", "wise", "grand", "swift", "vivid",
        )
        nouns = (
            "sketch", "draft", "spark", "bloom", "trail", "ridge", "creek", "grove",
            "cliff", "cloud", "field", "forge", "frost", "haven", "pearl", "stone",
            "storm", "river", "tower", "delta", "flame", "orbit", "pulse", "shore",
        )
        plans_dir = Path(self.work_dir) / ".seacode" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%m%d-%H%M")
        name = f"{random.choice(adjectives)}-{random.choice(nouns)}-{timestamp}.md"
        self._plan_path_cache = plans_dir / name
        return self._plan_path_cache

    # 手动触发 Layer 2 压缩：跳过阈值检查与熔断器直接走压缩流程。
    # 成功返回 CompactNotification（携带结构化 boundary 供 /compact 持久化），
    # 失败返回 ErrorEvent；auto_compact 内部已重写 conversation.history，
    # 调用方随后通过 ConversationManager 的持久化边界继续追加新消息。
    async def manual_compact(
        self, conversation: ConversationManager
    ) -> CompactNotification | ErrorEvent:
        result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            self.session_dir,
            protocol=self.protocol,
            manual=True,
            breaker=self.compact_breaker,
            recovery=self.recovery_state,
            tool_schemas=self.registry.get_all_schemas(self.protocol),
            transcript_path=self._transcript_path,
        )
        if isinstance(result, CompactEvent):
            # 压缩成功后重新注入环境上下文与长期记忆（replace_history 已重置标记）。
            env_context = build_environment_context(
                self.work_dir, agent_catalog=self._agent_catalog
            )
            conversation.inject_environment(env_context)
            mem = self.memory_manager.load() if self.memory_manager else ""
            conversation.inject_long_term_memory(self.instructions_content, mem)
            return CompactNotification(
                before_tokens=result.before_tokens,
                message=f"上下文已压缩（压缩前 {result.before_tokens:,} tokens）",
                boundary=result.boundary,
            )
        # result 为 None（前缀太短）或 str（错误信息），统一转 ErrorEvent。
        return ErrorEvent(message=result or "压缩失败：对话历史为空或未达到压缩条件")

    # batch10：激活 Skill，把 SOP 存入 active_skills 供压缩恢复与 /skill 查看。
    def activate_skill(self, name: str, prompt: str) -> None:
        self.active_skills[name] = prompt

    # 清空已激活 Skill；/clear 与 /session new/resume 时调用，避免旧技能残留。
    def clear_active_skills(self) -> None:
        self.active_skills.clear()

    # batch10：设置 Skill 目录摘要文本，每轮注入环境上下文。
    def set_skill_catalog(self, text: str) -> None:
        self.skill_catalog = text

    # batch10：注入 SkillLoader 引用，供 /skill 命令与 LoadSkill 工具访问。
    def set_skill_loader(self, loader: Any) -> None:
        self.skill_loader = loader

    # batch11：注入 HookEngine；为 None 时关闭所有注入点（零开销）。
    def set_hook_engine(self, engine: HookEngine | None) -> None:
        self.hook_engine = engine

    # batch12：注入子 Agent 目录摘要文本与 (name, description) 列表。
    # catalog 文本由 app.py 拼装为 ## Available Sub-Agent Types 段落注入环境上下文；
    # catalog_list 保留元组列表供运行时查询（如错误提示列出可用子 Agent）。
    def set_agent_catalog(
        self, catalog: str, catalog_list: list[tuple[str, str]]
    ) -> None:
        self._agent_catalog = catalog
        self._agent_catalog_list = list(catalog_list)

    # batch12：保存父 Agent 完整工具注册表引用，供 AgentTool 克隆/过滤工具使用。
    def set_full_registry(self, registry: Any) -> None:
        self._full_registry = registry

    # batch14：消费 lead 邮箱中本 agent 的未读消息并注入对话历史。
    # _team_manager 或 team_name 缺失时静默跳过（向后兼容 batch01-13 行为）。
    def _consume_mailbox(self, conversation: ConversationManager) -> None:
        if self._team_manager is None or not self.team_name:
            return
        mailbox = self._team_manager.get_mailbox(self.team_name)
        msgs = mailbox.consume(self.agent_id)
        for msg in msgs:
            conversation.add_user_message(
                f"From {msg.from_agent}: {msg.content}",
                persist=False,
            )

    # batch14：每轮开头消费 lead 邮箱与注入 notification_fn 返回的提示。
    # notification_fn 通常为 team_manager.drain_lead_mailbox，返回 <team-notification> XML 列表。
    def _consume_team_notifications(self, conversation: ConversationManager) -> None:
        self._consume_mailbox(conversation)
        if self.notification_fn is not None:
            notes = self.notification_fn()
            for note in notes:
                conversation.add_system_reminder(note)

    # 从工具调用参数推断 file_path；支持 file_path/path 两种常见字段名。
    def _infer_file_path(self, arguments: dict[str, Any]) -> str:
        return arguments.get("file_path") or arguments.get("path") or ""

    # batch12：检查工具是否接受 conversation 与 parent_agent 扩展参数。
    # AgentTool 与 AskUserTool 的 execute 签名为 (params, conversation, parent_agent)，
    # 其它工具仍走基类 (params) 单参签名。用 inspect 检查避免 TypeError。
    def _tool_accepts_context(self, tool: Any) -> bool:
        import inspect

        try:
            sig = inspect.signature(tool.execute)
        except (ValueError, TypeError):
            return False
        params = sig.parameters
        return "conversation" in params and "parent_agent" in params

    # 构造 HookContext；event 标识事件名，其余字段按场景传入。
    def _build_hook_context(self, event: str, **kwargs: Any) -> HookContext:
        return HookContext(event_name=event, **kwargs)

    # 取出 HookEngine 累积的通知并转为 HookEvent 列表；无 engine 时返回空。
    def _drain_hook_events(self) -> list[HookEvent]:
        if not self.hook_engine:
            return []
        return [
            HookEvent(
                hook_id=n.hook_id,
                event=n.event,
                output=n.output,
                success=n.success,
            )
            for n in self.hook_engine.drain_notifications()
        ]

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

    # 工具结果回灌后的 Provider 请求若在首个流事件前失败，最多重试一次。
    # 此时工具已经执行完成，重试只重发同一消息链，不会重复执行副作用工具。
    async def _stream_with_post_tool_retry(
        self,
        messages: list[Any],
        system: str,
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        can_retry = bool(messages and messages[-1].tool_results)
        retried = False

        while True:
            emitted = False
            try:
                async for event in self.client.stream(
                    messages, system=system, tools=tools
                ):
                    emitted = True
                    yield event
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if not can_retry or retried or emitted:
                    raise
                retried = True

    # 执行 Agent 主循环：注入环境 → 长期记忆注入 → MCP 连接
    # → 每轮 prompt → 模型流 → 工具执行 → 回灌。
    async def run(
        self, conversation: ConversationManager
    ) -> AsyncIterator[AgentEvent]:
        self._current_conversation = conversation
        # batch13：用户回合起点留档；以最后一条 user 消息内容为快照文本，
        # 消息数作为 message_index 供 /rewind 列表展示。file_history 为 None
        # 时跳过（向后兼容 batch01-12 行为）。
        if self.file_history is not None:
            user_text = ""
            for msg in reversed(conversation.history):
                if msg.role == "user":
                    user_text = msg.content
                    break
            try:
                self.file_history.make_snapshot(
                    len(conversation.history), user_text
                )
            except Exception:
                # 快照失败不阻塞主循环；/rewind 仍可访问之前的快照。
                pass

        # 会话启动时注入会话级环境上下文（position 0，env_injected 标记只注入一次）。
        # batch12：附加子 Agent 目录摘要（## Available Sub-Agent Types 段落）。
        env_context = build_environment_context(
            self.work_dir, agent_catalog=self._agent_catalog
        )
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

        # batch11: session_start hook — run 开始时触发一次（env/memory/MCP 注入后）。
        if self.hook_engine:
            await self.hook_engine.run_hooks(
                "session_start", self._build_hook_context("session_start")
            )
            for he in self._drain_hook_events():
                yield he

        iteration = 0
        consecutive_unknown = 0
        max_tokens_escalated = False
        output_recoveries = 0

        while True:
            iteration += 1

            # batch14：每轮开头消费 lead 邮箱与注入 notification_fn 提示。
            # _team_manager / notification_fn 为 None 时静默跳过（向后兼容 batch01-13）。
            self._consume_team_notifications(conversation)

            # batch11: turn_start hook — 每轮迭代开头触发。
            if self.hook_engine:
                await self.hook_engine.run_hooks(
                    "turn_start", self._build_hook_context("turn_start")
                )
                for he in self._drain_hook_events():
                    yield he

            # 停止条件 1：迭代上限。
            if self.max_iterations > 0 and iteration > self.max_iterations:
                yield ErrorEvent(
                    message=f"Agent reached maximum iterations ({self.max_iterations})"
                )
                break

            # batch11: pre_send hook — LLM 调用前触发；
            # 取出 prompt 注入消息传给 build_system_prompt。
            if self.hook_engine:
                await self.hook_engine.run_hooks(
                    "pre_send", self._build_hook_context("pre_send")
                )
                for he in self._drain_hook_events():
                    yield he
            hook_prompts = (
                self.hook_engine.get_prompt_messages() if self.hook_engine else None
            )

            # 每轮动态拼装系统提示词，包含 Environment 段落与条件段落。
            # mcp_manager 装配时插入 ToolSearch 段落，引导模型先发现再调用 MCP 工具。
            # skill_catalog 非空时注入 # Skills 段落，让模型知道可用 Skill。
            # batch14：coordinator_mode=True 时走协调者提示词分支，工具集收敛为调度-only。
            system = build_system_prompt(
                work_dir=self.work_dir,
                mcp_enabled=self.mcp_manager is not None,
                skill_section=self.skill_catalog,
                hook_prompts=hook_prompts,
                coordinator_mode=self.coordinator_mode,
                agent_catalog=self._agent_catalog_list,
            )

            if self.plan_mode:
                plan_path = self._get_plan_path()
                if self.permission_checker is not None:
                    self.permission_checker.plan_file_path = str(plan_path)
                conversation.add_system_reminder(
                    build_plan_mode_reminder(
                        str(plan_path), plan_path.exists(), iteration
                    )
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
            # batch11: pre_tool_use 拒绝的工具结果累积到此，随后并入 tool_results 回灌对话。
            rejected_results: list[ToolResultBlock] = []

            messages = conversation.get_messages()
            llm_stream = self._stream_with_post_tool_retry(
                messages, system=system, tools=tools
            )

            # 流式消费：文本/思考增量转发，工具调用完整后立即提交执行。
            async for event in collector.consume(llm_stream):
                if isinstance(event, ToolUseEvent):
                    tc = collector.response.tool_calls[-1]
                    # batch11: pre_tool_use hook — 工具执行前拦截；reject 则跳过执行，
                    # 把拒绝原因作为 is_error=True 的 ToolResult 回灌，模型据此调整策略。
                    if self.hook_engine:
                        hook_ctx = self._build_hook_context(
                            "pre_tool_use",
                            tool_name=tc.tool_name,
                            tool_args=tc.arguments,
                            file_path=self._infer_file_path(tc.arguments),
                        )
                        rejection = await self.hook_engine.run_pre_tool_hooks(hook_ctx)
                        for he in self._drain_hook_events():
                            yield he
                        if rejection is not None:
                            rejection_msg = f"Hook rejected: {rejection.reason}"
                            rejected_results.append(
                                ToolResultBlock(
                                    tool_use_id=tc.tool_id,
                                    content=rejection_msg,
                                    is_error=True,
                                )
                            )
                            yield event
                            yield ToolResultEvent(
                                tool_id=tc.tool_id,
                                tool_name=tc.tool_name,
                                output=rejection_msg,
                                is_error=True,
                                elapsed=0.0,
                            )
                            continue
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
                        # batch12：传 conversation 供 AgentTool 构造 fork messages。
                        executor.submit(
                            self._execute_single_tool_direct(tc, conversation)
                        )
                yield event

            response = collector.response

            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens
            yield UsageEvent(
                input_tokens=self.total_input_tokens,
                output_tokens=self.total_output_tokens,
            )

            # batch11: post_receive hook — LLM 响应后触发，携带响应文本。
            if self.hook_engine:
                await self.hook_engine.run_hooks(
                    "post_receive",
                    self._build_hook_context(
                        "post_receive", message=response.text
                    ),
                )
                for he in self._drain_hook_events():
                    yield he

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
                            response.text, thinking_blocks=conv_thinking, persist=False
                        )
                        conversation.add_user_message(
                            "Output token limit hit. Resume directly from where you stopped. "
                            "Do not apologize or repeat previous content. "
                            "Pick up mid-thought if needed.",
                            persist=False,
                        )
                    yield RetryEvent(reason="max_tokens escalation")
                    continue
                elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:
                    output_recoveries += 1
                    conversation.add_assistant_message(
                        response.text, thinking_blocks=conv_thinking, persist=False
                    )
                    conversation.add_user_message(
                        "Output token limit hit. Resume directly from where you stopped. "
                        "Break remaining work into smaller pieces.",
                        persist=False,
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
                # 循环计数 +1，用于记忆提取节流与 /clear、/session 重置。
                self._loop_count += 1
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
            exit_plan_succeeded = False
            # batch11: 并入 pre_tool_use 拒绝的工具结果，让模型看到拒绝原因并调整策略。
            tool_results.extend(rejected_results)
            streaming_results = await executor.collect_results()
            # batch11: post_tool_use 需要原始 tool_call 的 arguments，按 tool_id 建索引。
            tool_call_by_id = {tc.tool_id: tc for tc in response.tool_calls}

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
                if br.tool_name == "ExitPlanMode" and not br.result.is_error:
                    exit_plan_succeeded = True
                # batch11: post_tool_use hook — 工具执行后触发，携带工具名与参数。
                if self.hook_engine and br.tool_id in tool_call_by_id:
                    orig_tc = tool_call_by_id[br.tool_id]
                    await self.hook_engine.run_hooks(
                        "post_tool_use",
                        self._build_hook_context(
                            "post_tool_use",
                            tool_name=orig_tc.tool_name,
                            tool_args=orig_tc.arguments,
                            file_path=self._infer_file_path(orig_tc.arguments),
                        ),
                    )
                    for he in self._drain_hook_events():
                        yield he

            # 延迟工具（需要交互式权限确认）：顺序执行，yield PermissionRequest 等待 HITL 回复。
            # ask 工具需要 HITL 同步，不能并发；并发路径在第 06 步 MCP 后启用。
            for tc in deferred_tool_calls:
                result: ToolResult | None = None
                elapsed = 0.0
                is_unknown = False

                # batch12：传 conversation 供 AskUserTool 等扩展工具使用。
                async for item in self._execute_tool_with_permission(tc, conversation):
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
                if tc.tool_name == "ExitPlanMode" and not result.is_error:
                    exit_plan_succeeded = True
                # batch11: post_tool_use hook — 延迟工具执行后同样触发。
                if self.hook_engine:
                    await self.hook_engine.run_hooks(
                        "post_tool_use",
                        self._build_hook_context(
                            "post_tool_use",
                            tool_name=tc.tool_name,
                            tool_args=tc.arguments,
                            file_path=self._infer_file_path(tc.arguments),
                        ),
                    )
                    for he in self._drain_hook_events():
                        yield he

            tool_results = _order_tool_results(tool_results, response.tool_calls)

            # 停止条件 3：连续未知工具调用达到上限。
            if consecutive_unknown >= _CONSECUTIVE_UNKNOWN_LIMIT:
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls"
                )
                break

            conversation.add_tool_results_message(tool_results)

            # 召回不会阻塞主请求；仅消费已经完成的预取结果，失败保持静默。
            if self.memory_recall_task is not None and not self._memory_recall_consumed:
                if self.memory_recall_task.done():
                    try:
                        recall = self.memory_recall_task.result()
                        if recall:
                            conversation.add_system_reminder(recall)
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._memory_recall_consumed = True
            yield TurnComplete(turn=iteration)

            if exit_plan_succeeded:
                yield LoopComplete(total_turns=iteration)
                break

            # batch11: turn_end hook — 每轮迭代结束触发（仅未命中停止条件的轮次）。
            if self.hook_engine:
                await self.hook_engine.run_hooks(
                    "turn_end", self._build_hook_context("turn_end")
                )
                for he in self._drain_hook_events():
                    yield he

        # batch11: session_end hook — run 结束时触发一次（while 循环退出后）。
        if self.hook_engine:
            await self.hook_engine.run_hooks(
                "session_end", self._build_hook_context("session_end")
            )
            for he in self._drain_hook_events():
                yield he

    # 直接执行单个工具调用，返回结构化结果与耗时；未知/禁用工具返回错误结果。
    # 权限 deny 决策在此处直接转为错误结果；ask 决策由 _execute_tool_with_permission 处理。
    # batch12：conversation 用于 AgentTool 取父对话历史构造 fork messages；
    # 不传时回退到 None，AgentTool 内部用 self.parent_agent 作为 fallback。
    async def _execute_single_tool_direct(
        self,
        tc: ToolCallComplete,
        conversation: Any = None,
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
            # batch12：扩展签名工具（AgentTool/AskUserTool）传入 conversation 与 parent_agent。
            # 基类 execute 只声明 params；通过 _tool_accepts_context 门控后传扩展参数。
            if self._tool_accepts_context(tool):
                result = await tool.execute(
                    params, conversation=conversation, parent_agent=self  # type: ignore[call-arg]
                )
            else:
                result = await tool.execute(params)
        except ValidationError as e:
            result = ToolResult(
                content=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(content=f"Tool execution error: {e}", is_error=True)

        self._snapshot_file_read_for_recovery(tc, result)
        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=result,
            elapsed=time.monotonic() - start,
            is_unknown=False,
        )

    # 执行需 HITL 确认的工具调用；yield PermissionRequest 等待 TUI 回复后继续。
    # yield 顺序：PermissionRequest（ask 时）→ (result, elapsed, is_unknown) 元组。
    # batch12：conversation 用于 AskUserTool 等扩展工具；不传时回退到 None。
    async def _execute_tool_with_permission(
        self,
        tc: ToolCallComplete,
        conversation: Any = None,
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
            # batch12：支持扩展签名的工具传入 conversation 与 parent_agent。
            if self._tool_accepts_context(tool):
                result = await tool.execute(
                    params, conversation=conversation, parent_agent=self  # type: ignore[call-arg]
                )
            else:
                result = await tool.execute(params)
        except ValidationError as e:
            result = ToolResult(
                content=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(content=f"Tool execution error: {e}", is_error=True)

        self._snapshot_file_read_for_recovery(tc, result)
        yield result, time.monotonic() - start, False

    # 成功读取文件后保存原始内容，供上下文压缩恢复时重新附加。
    def _snapshot_file_read_for_recovery(
        self, tc: ToolCallComplete, result: ToolResult
    ) -> None:
        if tc.tool_name != "ReadFile" or result.is_error:
            return
        file_path = tc.arguments.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self.recovery_state.record_file_read(file_path, content)

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

    # -----------------------------------------------------------------
    # batch12：子 Agent 非交互执行循环
    # -----------------------------------------------------------------

    # 非交互执行循环：用于子 Agent（定义式 + Fork 路径）与后台任务。
    # 与 run() 的差异：不 yield 事件、不触发 session_start/turn_start 等 Hook、
    # 不做 HITL 权限确认（子 Agent 走 bypassPermissions 或 pre_tool_use Hook 拒绝）、
    # 不做长期记忆提取与整理（子 Agent 不直接写记忆）。
    # fork 路径传入 conversation（已含 FORK_BOILERPLATE 与 task）；定义式路径传 task 字符串，
    # 内部新建 ConversationManager 并以 AgentDef.system_prompt 为系统提示词。
    # 工具调用按 self.registry 执行；max_iterations 由 AgentDef.max_turns 在构造时传入。
    async def run_to_completion(
        self,
        task: str,
        conversation: Any = None,
        event_callback: Any = None,
    ) -> str:
        # fork 路径用传入的 conversation（已含 FORK_BOILERPLATE + task user message）；
        # 定义式路径新建 ConversationManager 并把 task 作为首条 user message。
        if conversation is not None:
            conv = conversation
        else:
            conv = ConversationManager()
            if task:
                conv.add_user_message(task)

        # 子 Agent 系统提示词：定义式用 AgentDef.system_prompt；fork 用空串
        # （FORK_BOILERPLATE 已在 messages 中以 user message 形式注入）。
        system_prompt = ""
        definition = getattr(self, "_current_definition", None)
        if definition is not None:
            # definition 为 Any 类型；直接取 system_prompt 属性避免 getattr 默认值类型推断。
            sp = getattr(definition, "system_prompt", None)
            if sp:
                system_prompt = str(sp)

        iteration = 0
        consecutive_unknown = 0

        while True:
            iteration += 1

            # batch14：每轮开头消费 lead 邮箱与注入 notification_fn 提示。
            # 子 Agent（teammate）路径通常 _team_manager 为 None，此处静默跳过。
            self._consume_team_notifications(conv)

            # 停止条件 1：迭代上限。
            if self.max_iterations > 0 and iteration > self.max_iterations:
                break

            # 每轮重新获取工具 Schema（mark_discovered 后新工具立即纳入）。
            tools = self.registry.get_all_schemas(self.protocol)
            messages = conv.get_messages()
            llm_stream = self._stream_with_post_tool_retry(
                messages, system=system_prompt, tools=tools
            )

            collector = StreamCollector()
            executor = StreamingExecutor()
            # pre_tool_use Hook 拒绝的工具结果累积，随后并入 tool_results 回灌对话。
            rejected_results: list[ToolResultBlock] = []

            # 消费 LLM 流：文本/思考增量累积，工具调用完整后立即提交执行。
            async for event in collector.consume(llm_stream):
                if isinstance(event, ToolUseEvent):
                    tc = collector.response.tool_calls[-1]
                    # pre_tool_use Hook 拒绝的工具构造 is_error ToolResult，
                    # 累积到 rejected_results 在流结束后与 streaming_results 合并回灌。
                    if self.hook_engine:
                        hook_ctx = self._build_hook_context(
                            "pre_tool_use",
                            tool_name=tc.tool_name,
                            tool_args=tc.arguments,
                            file_path=self._infer_file_path(tc.arguments),
                        )
                        rejection = await self.hook_engine.run_pre_tool_hooks(hook_ctx)
                        if rejection is not None:
                            rejected_results.append(
                                ToolResultBlock(
                                    tool_use_id=tc.tool_id,
                                    content=f"Hook rejected: {rejection.reason}",
                                    is_error=True,
                                )
                            )
                            continue
                    # 子 Agent 默认 bypassPermissions，权限检查只走 deny/allow，
                    # ask 决策在此路径直接当 allow 处理（不阻塞等待 HITL）。
                    tool = self.registry.get(tc.tool_name)
                    needs_deny = False
                    deny_reason = ""
                    if tool and self.permission_checker:
                        decision = self.permission_checker.check(tool, tc.arguments)
                        if decision.effect == "deny":
                            needs_deny = True
                            deny_reason = decision.reason
                    if needs_deny:
                        # deny 决策构造错误结果回灌；通过 executor 提交保持顺序。
                        executor.submit(self._make_denied_result(tc, deny_reason))
                    else:
                        # batch12：传 conv 供 AgentTool 构造 fork messages。
                        executor.submit(
                            self._execute_single_tool_direct(tc, conv)
                        )

            response = collector.response
            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens

            # 可选进度上报：供 TaskManager 在 adopt_running 场景读取部分输出。
            if event_callback is not None:
                try:
                    event_callback(
                        {
                            "text": response.text,
                            "tool_calls": len(response.tool_calls),
                            "iteration": iteration,
                        }
                    )
                except Exception:
                    pass

            conv_thinking = [
                ThinkingBlock(thinking=tb.thinking, signature=tb.signature)
                for tb in response.thinking_blocks
            ]

            # 停止条件 2：无工具调用，模型给出最终回复。
            if not response.tool_calls:
                conv.add_assistant_message(response.text, thinking_blocks=conv_thinking)
                self.last_output = response.text
                break

            # 有工具调用：提交助手消息、执行工具、回灌结果。
            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conv.add_assistant_message(
                response.text, tool_uses=tool_uses, thinking_blocks=conv_thinking
            )

            streaming_results = await executor.collect_results()
            tool_results: list[ToolResultBlock] = []
            # 并入 pre_tool_use Hook 拒绝的工具结果，让模型看到拒绝原因并调整策略。
            tool_results.extend(rejected_results)
            for br in streaming_results:
                if br.is_unknown:
                    consecutive_unknown += 1
                else:
                    consecutive_unknown = 0
                content = self._maybe_persist_or_truncate(br.tool_id, br.result.content)
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=br.tool_id,
                        content=content,
                        is_error=br.result.is_error,
                    )
                )

            tool_results = _order_tool_results(tool_results, response.tool_calls)

            # 停止条件 3：连续未知工具调用达到上限。
            if consecutive_unknown >= _CONSECUTIVE_UNKNOWN_LIMIT:
                break

            conv.add_tool_results_message(tool_results)

        return self.last_output

    # 构造一个被 deny 的工具执行协程，返回 is_error=True 的 _ToolExecResult。
    # 用于 run_to_completion 中权限 deny 决策的直接回灌，避免阻塞主循环。
    async def _make_denied_result(
        self, tc: ToolCallComplete, reason: str
    ) -> _ToolExecResult:
        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=ToolResult(
                content=f"Permission denied: {reason}", is_error=True
            ),
            elapsed=0.0,
            is_unknown=False,
        )
