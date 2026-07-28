"""Agent 工具：定义式 + Fork + Worktree 隔离 + Teammate 四路径子 Agent 调度。

按 ``params.subagent_type`` 分流：非空走定义式（从 AgentLoader 取 AgentDef，
按 ``is_background`` 过滤工具），留空走 Fork（要求 ``enable_fork=true``，
调用 ``build_forked_messages`` 复制父对话历史，工具用 ``clone_registry_for_fork``）。
``AgentDef.isolation == "worktree"`` 时走 Worktree 隔离路径：在独立 worktree
中创建子 Agent 执行任务，结束后按变更检测结果自动清理或保留。

``team_name`` 非空时走 Teammate 路径：在团队 worktree 中按后端（in-process /
tmux / iTerm2）spawn 长驻 teammate，通过邮箱与 Lead 通信。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Protocol

from pydantic import BaseModel

from seacode.agents.fork import (
    FORK_QUERY_SOURCE,
    ForkError,
    build_forked_messages,
)
from seacode.agents.parser import AgentDef
from seacode.agents.tool_filter import (
    build_teammate_tools,
    clone_registry_for_fork,
    resolve_agent_tools,
)
from seacode.tools.base import Tool, ToolCategory, ToolResult
from seacode.worktree.integration import build_worktree_notice, generate_worktree_name
from seacode.worktree.manager import WorktreeError, WorktreeManager

log = logging.getLogger(__name__)

# 别名到具体模型 id 的映射；占位字符串由 app.py 在初始化时按配置注入。
# 这里保留默认值，让单测可以不依赖外部配置直接构造。
_DEFAULT_MODEL_ALIASES: dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-5",
    "opus": "claude-opus-4-1",
}

# batch14：teammate 系统提示词附加段；告知 worker 文本回复对其它成员不可见，
# 需用 SendMessage 通信，且工作在隔离 worktree 中需用相对路径。
TEAMMATE_ADDENDUM = (
    "\n[TEAMMATE CONTEXT]\n"
    "You are a teammate in a team. Your text replies are NOT visible to other "
    "teammates — use the SendMessage tool to communicate. You are working in an "
    "isolated worktree; use relative paths for all file operations.\n"
    "[/TEAMMATE CONTEXT]"
)


class AgentDefinitionProvider(Protocol):
    """子 Agent 调度所需的定义查询能力。"""

    def get(self, name: str) -> AgentDef | None: ...

    def list_agents(self) -> list[tuple[str, str]]: ...


class TaskLauncher(Protocol):
    """后台子 Agent 调度所需的最小任务入口。"""

    async def launch(
        self,
        agent: Any,
        task: str,
        name: str,
        fork_conversation: Any = None,
    ) -> str: ...


class TraceNodeRef(Protocol):
    """子 Agent 追踪节点向调度器暴露的标识。"""

    agent_id: str


class TraceRecorder(Protocol):
    """子 Agent 调度所需的调用链记录能力。"""

    def create(
        self, agent_type: str, parent_id: str | None, trace_id: str
    ) -> TraceNodeRef: ...

    def update(self, agent_id: str, **kwargs: Any) -> None: ...

    def complete(self, agent_id: str, status: str = "completed") -> None: ...


class AgentToolParams(BaseModel):
    """Agent 工具参数；subagent_type 留空走 Fork 路径，team_name 非空走 Teammate 路径。"""

    subagent_type: str = ""
    description: str = ""
    prompt: str = ""
    run_in_background: bool = False
    model: str | None = None
    isolation: str | None = None
    # batch14：team_name 非空走 Teammate 路径；name 指定 teammate 显示名（缺省用 agent_type）。
    team_name: str | None = None
    name: str | None = None


class AgentTool(Tool):
    """子 Agent 调度工具；定义式 + Fork 双路径。"""

    name = "Agent"
    description = (
        "Launch a sub-agent to handle a task in an isolated context. "
        "Use subagent_type to select a predefined agent type (e.g. Explore, "
        "Plan, general-purpose), or leave it empty to fork the current "
        "conversation. Use team_name (together with name) to spawn a "
        "long-running teammate in an existing team — teammates persist after "
        "the lead returns and communicate via SendMessage, unlike regular "
        "sub-agents which block and return inline."
    )
    category = ToolCategory.COMMAND
    is_system_tool = False
    should_defer = False  # Agent 工具不延迟；需立即执行子 Agent 并阻塞当前回合
    # Agent 工具需要 Agent.execute(params, conversation, parent_agent) 三参签名。
    params_model = AgentToolParams

    def __init__(
        self,
        agent_loader: AgentDefinitionProvider,
        task_manager: TaskLauncher,
        trace_manager: TraceRecorder,
        parent_agent: Any,
        enable_fork: bool = False,
        provider_config: Any = None,
        worktree_manager: Any = None,
        team_manager: Any = None,
        model_aliases: dict[str, str] | None = None,
    ) -> None:
        self.agent_loader = agent_loader
        self.task_manager = task_manager
        self.trace_manager = trace_manager
        self.parent_agent = parent_agent
        self.enable_fork = enable_fork
        self.provider_config = provider_config
        # worktree_manager / team_manager 保留参数签名但不路由；第 13/14 步启用。
        self.worktree_manager = worktree_manager
        self.team_manager = team_manager
        # query_source: None 或 FORK_QUERY_SOURCE；fork 子 Agent 标记后不能再 fork。
        self.query_source: str | None = None
        self.model_aliases = dict(_DEFAULT_MODEL_ALIASES)
        if model_aliases:
            self.model_aliases.update(model_aliases)

    # 执行子 Agent；按 subagent_type 分流到定义式或 Fork 路径。
    async def execute(
        self,
        params: BaseModel,
        conversation: Any = None,
        parent_agent: Any = None,
    ) -> ToolResult:
        # 基类签名是 BaseModel，实际由 params_model 校验为 AgentToolParams。
        tool_params: AgentToolParams = params  # type: ignore[assignment]
        # 优先使用传入 parent_agent，回退到构造时保存的引用。
        parent = parent_agent if parent_agent is not None else self.parent_agent

        # batch14：team_name 非空走 Teammate 路径（与 SubAgent / Fork 互斥）。
        if getattr(tool_params, "team_name", None):
            return await self._execute_as_teammate(
                tool_params, conversation, parent
            )

        if tool_params.isolation == "worktree":
            definition = (
                self.agent_loader.get(tool_params.subagent_type)
                if tool_params.subagent_type
                else AgentDef(
                    agent_type="worktree-agent",
                    when_to_use="isolated worktree agent",
                    system_prompt="",
                    model="inherit",
                    max_turns=getattr(parent, "max_iterations", 100),
                    permission_mode="bypassPermissions",
                    isolation="worktree",
                    source="builtin",
                )
            )
            if definition is None:
                available = self.agent_loader.list_agents()
                available_str = ", ".join(
                    f"{name} ({description})" for name, description in available
                )
                return ToolResult(
                    content=(
                        f"未知子 Agent 类型: {tool_params.subagent_type}，"
                        f"可用: {available_str}"
                    ),
                    is_error=True,
                )
            return await self._execute_with_worktree(
                tool_params, conversation, parent, definition
            )

        if tool_params.subagent_type:
            return await self._execute_subagent(
                tool_params, conversation, parent, is_fork=False
            )

        # Fork 路径。
        if not self.enable_fork:
            return ToolResult(
                content="fork 未启用，请在配置中开启 enable_fork",
                is_error=True,
            )
        if self.query_source == FORK_QUERY_SOURCE:
            return ToolResult(
                content="fork 子 Agent 不能再次 fork", is_error=True
            )
        try:
            fork_messages = build_forked_messages(conversation, tool_params.prompt)
        except ForkError as e:
            return ToolResult(content=str(e), is_error=True)

        fork_registry = clone_registry_for_fork(parent._full_registry)
        # fork 子 Agent 默认 bypassPermissions，max_turns 继承父 Agent。
        fork_def = AgentDef(
            agent_type="fork",
            when_to_use="",
            system_prompt="",
            permission_mode="bypassPermissions",
            max_turns=getattr(parent, "max_iterations", 100),
        )
        return await self._execute_subagent(
            tool_params,
            conversation,
            parent,
            is_fork=True,
            fork_conversation=fork_messages,
            fork_def=fork_def,
            fork_registry=fork_registry,
        )

    # 子 Agent 实际执行；前台同步返回文本，后台异步返回 task_id。
    async def _execute_subagent(
        self,
        params: AgentToolParams,
        conversation: Any,
        parent_agent: Any,
        is_fork: bool,
        fork_conversation: Any = None,
        fork_def: AgentDef | None = None,
        fork_registry: Any = None,
    ) -> ToolResult:
        definition = (
            fork_def
            if is_fork and fork_def is not None
            else self.agent_loader.get(params.subagent_type)
        )
        if definition is None:
            available = self.agent_loader.list_agents()
            available_str = ", ".join(f"{name} ({desc})" for name, desc in available)
            return ToolResult(
                content=(
                    f"未知子 Agent 类型: {params.subagent_type}，可用: {available_str}"
                ),
                is_error=True,
            )

        # isolation=worktree 走 Worktree 隔离路径：在独立 worktree 中执行子 Agent。
        if definition.isolation == "worktree":
            return await self._execute_with_worktree(
                params, conversation, parent_agent, definition
            )

        # fork 默认后台；定义式看 run_in_background / definition.background。
        is_background = is_fork or params.run_in_background or definition.background

        # 工具过滤：fork 用 clone_registry_for_fork；定义式用 resolve_agent_tools。
        parent_registry = parent_agent._full_registry
        if is_fork and fork_registry is not None:
            sub_registry = fork_registry
        elif is_fork:
            sub_registry = clone_registry_for_fork(parent_registry)
        else:
            sub_registry = resolve_agent_tools(
                parent_registry, definition, is_background
            )

        # 子 Agent 实例化：复用父 Agent 的 protocol / work_dir / context_window。
        client = self._select_llm(params, definition, parent_agent)
        sub_agent = self._create_sub_agent(
            client=client,
            parent_agent=parent_agent,
            definition=definition,
            sub_registry=sub_registry,
        )

        # 调用链追踪：trace_id 继承父 Agent；agent_id 由 TraceManager 生成。
        parent_trace_id: str = str(
            getattr(parent_agent, "trace_id", None)
            or getattr(parent_agent, "agent_id", "root")
        )
        trace_node = self.trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=getattr(parent_agent, "agent_id", None),
            trace_id=parent_trace_id,
        )
        sub_agent.agent_id = trace_node.agent_id
        sub_agent.parent_id = getattr(parent_agent, "agent_id", None)
        sub_agent.trace_id = parent_trace_id

        # fork 子 Agent 继承 replacement_state 以命中 prompt cache。
        if is_fork:
            sub_agent.replacement_state = copy.deepcopy(
                getattr(parent_agent, "replacement_state", None)
            )

        # 前台同步路径：直接 await run_to_completion 并把结果回灌。
        if not is_background:
            try:
                result_text = await sub_agent.run_to_completion(
                    params.prompt, conversation=fork_conversation
                )
            except Exception as e:
                log.error("子 Agent 执行失败: %s", e)
                self.trace_manager.complete(
                    trace_node.agent_id, status="failed"
                )
                return ToolResult(
                    content=f"子 Agent 执行失败: {e}", is_error=True
                )
            self.trace_manager.update(
                trace_node.agent_id,
                input_tokens=getattr(sub_agent, "total_input_tokens", 0),
                output_tokens=getattr(sub_agent, "total_output_tokens", 0),
            )
            self.trace_manager.complete(trace_node.agent_id)
            return ToolResult(content=result_text or "")

        # 后台异步路径：通过 TaskManager.launch 启动；不阻塞当前回合。
        task_id = await self.task_manager.launch(
            agent=sub_agent,
            task="" if is_fork else params.prompt,
            name=definition.agent_type,
            fork_conversation=fork_conversation if is_fork else None,
        )
        return ToolResult(
            content=(
                f"后台任务已启动 (id: {task_id})。"
                "不要 wait/sleep/poll，主对话会在任务完成时收到通知。"
            )
        )

    # 创建子 Agent 实例；延迟导入避免循环。
    def _create_sub_agent(
        self,
        client: Any,
        parent_agent: Any,
        definition: AgentDef,
        sub_registry: Any,
    ) -> Any:
        from seacode.agent import Agent
        from seacode.permissions import PermissionChecker, PermissionMode

        # 权限模式映射：bypassPermissions → BYPASS；空串沿用父 Agent 当前模式。
        parent_mode = getattr(parent_agent, "permission_mode", PermissionMode.DEFAULT)
        mode_str = definition.permission_mode or parent_mode.value
        mode_map = {
            "default": PermissionMode.DEFAULT,
            "acceptEdits": PermissionMode.ACCEPT_EDITS,
            "bypassPermissions": PermissionMode.BYPASS,
            "": parent_mode,
        }
        sub_mode = mode_map.get(mode_str, PermissionMode.DEFAULT)

        # 复用父 Agent 权限组件构造子 Agent 检查器；父 checker 为 None 时子 Agent 也不启用。
        parent_checker = getattr(parent_agent, "permission_checker", None)
        if parent_checker is not None:
            sub_checker = PermissionChecker(
                detector=parent_checker.detector,
                sandbox=parent_checker.sandbox,
                rule_engine=parent_checker.rule_engine,
                mode=sub_mode,
                sandbox_enabled=parent_checker.sandbox_enabled,
            )
        else:
            sub_checker = None

        return Agent(
            client=client,
            registry=sub_registry,
            protocol=getattr(parent_agent, "protocol", "anthropic"),
            work_dir=getattr(parent_agent, "work_dir", "."),
            max_iterations=definition.max_turns,
            permission_checker=sub_checker,
            context_window=getattr(parent_agent, "context_window", 200_000),
            instructions_content=getattr(parent_agent, "instructions_content", ""),
            memory_manager=None,  # 子 Agent 不直接写长期记忆
            hook_engine=getattr(parent_agent, "hook_engine", None),
        )

    # 选择子 Agent LLM 客户端；model 别名映射或具体模型名直通；失败回退父 client。
    def _select_llm(
        self, params: AgentToolParams, definition: AgentDef, parent_agent: Any
    ) -> Any:
        # params.model 优先；definition.model 非 inherit 时次之；否则回退父 client。
        model_override = params.model
        if not model_override and definition.model != "inherit":
            model_override = definition.model
        if not model_override:
            return parent_agent.client

        # 别名映射；非别名直通模型名。
        model_id = self.model_aliases.get(
            model_override.lower(), model_override
        )

        if self.provider_config is None:
            return parent_agent.client
        try:
            # 浅拷贝配置并替换 model 字段；保留 protocol/base_url/api_key。
            new_cfg = copy.copy(self.provider_config)
            object.__setattr__(new_cfg, "model", model_id)
            from seacode.client import create_client

            return create_client(new_cfg)
        except Exception:
            # 失败回退父 client，保证调用方不中断。
            return parent_agent.client

    # 兼容基类签名；实际 execute 走三参版本。
    async def _execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
        return await self.execute(params)

    # 注入 WorktreeManager；app.py 在装配阶段调用，None 时关闭 worktree 隔离路径。
    def set_worktree_manager(self, manager: WorktreeManager | None) -> None:
        self.worktree_manager = manager

    # batch14：注入 TeamManager；app.py 在装配阶段调用，None 时关闭 Teammate 路径。
    def set_team_manager(self, manager: Any) -> None:
        self.team_manager = manager

    # batch14：为 teammate 生成唯一名称；同名时追加 -2 / -3 直到不冲突。
    def _unique_teammate_name(self, name: str, team_name: str) -> str:
        team = self.team_manager.get_team(team_name) if self.team_manager else None
        if team is None or team.get_member(name) is None:
            return name
        i = 2
        while True:
            candidate = f"{name}-{i}"
            if team.get_member(candidate) is None:
                return candidate
            i += 1

    # batch14：Teammate 路径六步——加载定义 → 建 worktree → 选 LLM → 过滤工具 →
    # 按后端 spawn → 注册名字与成员。in-process 长驻邮箱循环；pane 后端外进程执行。
    async def _execute_as_teammate(
        self,
        params: AgentToolParams,
        conversation: Any,
        parent_agent: Any,
    ) -> ToolResult:
        del conversation  # teammate 不复用父对话历史，spawn_inprocess 自建 ConversationManager
        from uuid import uuid4

        from seacode.agent import Agent
        from seacode.teams.models import BackendType, TeammateInfo
        from seacode.teams.registry import AgentNameRegistry
        from seacode.teams.spawn_inprocess import spawn_inprocess_teammate
        from seacode.teams.spawn_iterm2 import spawn_iterm2_teammate
        from seacode.teams.spawn_tmux import spawn_tmux_teammate

        team_name = params.team_name or ""
        # 前置检查：team_manager / worktree_manager 必须已装配。
        if self.team_manager is None:
            return ToolResult(content="team_manager 未初始化", is_error=True)
        if self.worktree_manager is None:
            return ToolResult(content="worktree manager 未初始化", is_error=True)

        # 第 1 步：加载 AgentDef；无 subagent_type 时构造默认 teammate 定义。
        if params.subagent_type:
            agent_def = self.agent_loader.get(params.subagent_type)
            if agent_def is None:
                available = self.agent_loader.list_agents()
                available_str = ", ".join(
                    f"{n} ({d})" for n, d in available
                )
                return ToolResult(
                    content=f"未知子 Agent 类型: {params.subagent_type}，可用: {available_str}",
                    is_error=True,
                )
        else:
            agent_def = AgentDef(
                agent_type="teammate",
                when_to_use="default teammate",
                system_prompt="",
                permission_mode="bypassPermissions",
            )

        # 第 2 步：创建 worktree；名称格式 team-<team>/<member>，便于清理时识别。
        teammate_name = self._unique_teammate_name(
            params.name or agent_def.agent_type, team_name
        )
        wt_name = f"team-{team_name}/{teammate_name}"
        try:
            wt = await self.worktree_manager.create(wt_name, "HEAD")
        except WorktreeError as e:
            return ToolResult(
                content=f"创建 worktree 失败: {e}", is_error=True
            )

        # 第 3 步：选 LLM；按 agent_def.model 或父 Agent client。
        client = self._select_llm(params, agent_def, parent_agent)

        # 第 4 步：build_teammate_tools 按后端过滤工具集并实例化绑定身份的协调工具。
        # 传入 teammate_agent 的 agent_id 与 teammate_name，让协调工具绑定正确身份。
        backend = self.team_manager.detect_backend("", True)
        parent_registry = getattr(parent_agent, "_full_registry", None)
        if parent_registry is None:
            parent_registry = parent_agent.registry
        # 先生成 agent_id 供 build_teammate_tools 绑定协调工具身份。
        teammate_agent_id = f"{teammate_name}-{uuid4().hex[:8]}"
        teammate_registry = build_teammate_tools(
            parent_registry,
            self.team_manager,
            team_name,
            teammate_agent_id,
            teammate_name,
            backend,
            definition=agent_def,
        )

        # 第 5 步：构造 teammate Agent 并按后端 spawn。
        teammate_agent = Agent(
            client=client,
            registry=teammate_registry,
            protocol=getattr(parent_agent, "protocol", "anthropic"),
            work_dir=wt.path,
            max_iterations=agent_def.max_turns,
            permission_checker=None,  # teammate 在隔离 worktree 中，bypass 权限
            context_window=getattr(parent_agent, "context_window", 200_000),
            agent_id=teammate_agent_id,
            team_name=team_name,
            team_manager=self.team_manager,
        )
        # 注入 TEAMMATE_ADDENDUM 到系统提示词；通过 _current_definition 传递。
        teammate_def = AgentDef(
            agent_type=agent_def.agent_type,
            when_to_use=agent_def.when_to_use,
            system_prompt=agent_def.system_prompt + TEAMMATE_ADDENDUM,
            tools=agent_def.tools,
            disallowed_tools=agent_def.disallowed_tools,
            model=agent_def.model,
            max_turns=agent_def.max_turns,
            permission_mode="bypassPermissions",
            background=agent_def.background,
            isolation=agent_def.isolation,
            file_path=agent_def.file_path,
            source=agent_def.source,
        )
        teammate_agent._current_definition = teammate_def

        mailbox = self.team_manager.get_mailbox(team_name)
        team = self.team_manager.get_team(team_name)
        if team is None:
            return ToolResult(
                content=f"团队不存在: {team_name}", is_error=True
            )

        if backend == BackendType.IN_PROCESS:
            handle = spawn_inprocess_teammate(
                teammate_agent,
                params.prompt,
                teammate_name,
                self.team_manager,
                mailbox=mailbox,
                lead_agent_id=team.lead_agent_id,
            )
            self.team_manager.register_inprocess_handle(
                teammate_agent_id, handle
            )
        elif backend in (BackendType.TMUX, BackendType.ITERM2):
            # pane 后端：spawn 前先把初始任务投进队友邮箱，
            # 新进程启动后第一次空闲轮询就能看到工作。
            if mailbox is not None and params.prompt:
                from seacode.teams.mailbox import create_message
                from seacode.teams.spawn_inprocess import LEAD_NAME

                mailbox.write(
                    teammate_name,
                    create_message(
                        from_agent=LEAD_NAME,
                        to_agent=teammate_name,
                        content=params.prompt,
                        summary="initial task",
                    ),
                )
            try:
                if backend == BackendType.TMUX:
                    tmux_pane = spawn_tmux_teammate(
                        team_name, teammate_name, wt.path
                    )
                    self.team_manager.register_pane_id(
                        teammate_agent_id, tmux_pane.pane_id
                    )
                else:  # BackendType.ITERM2
                    iterm_pane = spawn_iterm2_teammate(
                        team_name, teammate_name, wt.path
                    )
                    self.team_manager.register_pane_id(
                        teammate_agent_id, iterm_pane.session_id
                    )
            except Exception as e:
                log.warning("pane spawn 失败: %s", e)
                return ToolResult(
                    content=(
                        f"pane spawn 失败 ({e})，teammate 未启动。"
                        "可重试或将 teammate_mode 设为 in-process。"
                    ),
                    is_error=True,
                )

        # 第 6 步：注册名字到 AgentNameRegistry 并持久化成员到团队 config。
        AgentNameRegistry.instance().register(
            teammate_name, teammate_agent.agent_id
        )
        member = TeammateInfo(
            name=teammate_name,
            agent_id=teammate_agent.agent_id,
            agent_type=agent_def.agent_type,
            model=params.model or agent_def.model,
            worktree_path=wt.path,
            backend_type=backend,
            is_active=None,
        )
        self.team_manager.register_member(team_name, member)

        return ToolResult(
            content=(
                f"teammate {teammate_name} 已启动 "
                f"(worktree={wt.path}, backend={backend.value})"
            ),
            is_error=False,
        )

    # 在隔离 worktree 中执行子 Agent；任务文本前注入 worktree 上下文通知。
    # 子 Agent 复用父 client/protocol/context_window，work_dir 切换到 worktree 路径，
    # 权限默认 bypassPermissions（隔离环境不阻塞主循环）。执行完后 auto_cleanup 检查
    # 变更：无变更自动删除 worktree；有变更保留并在结果末尾附加路径提示。
    async def _execute_with_worktree(
        self,
        params: AgentToolParams,
        conversation: Any,
        parent_agent: Any,
        definition: AgentDef,
    ) -> ToolResult:
        del conversation  # worktree 子 Agent 不复用父对话历史
        if self.worktree_manager is None:
            return ToolResult(
                content="Worktree manager 未初始化", is_error=True
            )
        name = generate_worktree_name()
        try:
            wt = await self.worktree_manager.create(name, "HEAD")
        except WorktreeError as e:
            return ToolResult(
                content=f"创建 worktree 失败: {e}", is_error=True
            )
        # 构造注入子 Agent 任务前的 worktree 上下文通知。
        parent_cwd = str(getattr(parent_agent, "work_dir", "."))
        notice = build_worktree_notice(parent_cwd, wt.path)
        task_with_notice = notice + "\n\n" + params.prompt

        # 复用父 Agent 工具集，让子 Agent 在隔离环境中仍能用 ReadFile/WriteFile 等。
        parent_registry = getattr(parent_agent, "_full_registry", None)
        if parent_registry is not None:
            sub_registry = clone_registry_for_fork(parent_registry)
        else:
            from seacode.tools import ToolRegistry

            sub_registry = ToolRegistry()

        client = self._select_llm(params, definition, parent_agent)
        sub_agent = self._create_sub_agent(
            client=client,
            parent_agent=parent_agent,
            definition=definition,
            sub_registry=sub_registry,
        )
        # 覆盖 work_dir 到 worktree 路径，让子 Agent 的所有工具调用都在隔离环境内。
        sub_agent.work_dir = wt.path
        # 同步替换 permission_checker 的 sandbox 根到 worktree 路径，
        # 避免 sandbox 仍指向父目录导致子 Agent 文件访问越界或被误拦。
        sub_checker = getattr(sub_agent, "permission_checker", None)
        if sub_checker is not None:
            from seacode.permissions.sandbox import PathSandbox

            sub_checker.sandbox = PathSandbox(wt.path)

        # 调用链追踪：trace_id 继承父 Agent；agent_id 由 TraceManager 生成。
        parent_trace_id: str = str(
            getattr(parent_agent, "trace_id", None)
            or getattr(parent_agent, "agent_id", "root")
        )
        trace_node = self.trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=getattr(parent_agent, "agent_id", None),
            trace_id=parent_trace_id,
        )
        sub_agent.agent_id = trace_node.agent_id
        sub_agent.parent_id = getattr(parent_agent, "agent_id", None)
        sub_agent.trace_id = parent_trace_id

        try:
            result_text = await sub_agent.run_to_completion(task_with_notice)
            self.trace_manager.update(
                trace_node.agent_id,
                input_tokens=getattr(sub_agent, "total_input_tokens", 0),
                output_tokens=getattr(sub_agent, "total_output_tokens", 0),
            )
            self.trace_manager.complete(trace_node.agent_id)
        except Exception as e:
            log.error("worktree 子 Agent 执行失败: %s", e)
            self.trace_manager.complete(trace_node.agent_id, status="failed")
            return ToolResult(
                content=f"子 Agent 执行失败: {e}", is_error=True
            )

        # 无变更自动删除；有变更保留并在结果末尾附加路径提示。
        cleanup = await self.worktree_manager.auto_cleanup(name, wt.head_commit)
        if cleanup.kept:
            result_text = (result_text or "") + f"\n[Worktree preserved at {wt.path}]"
        return ToolResult(content=result_text or "", is_error=False)
