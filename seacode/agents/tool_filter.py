"""子 Agent 工具过滤：五层防线与 Fork 工具克隆。

五层过滤顺序固定：(0) MCP 工具始终放行 → (1) 全局禁用 → (2) 自定义 agent 来源
额外禁用 → (3) 后台白名单收拢 → (4) 定义层 disallowedTools / tools 应用。

``clone_registry_for_fork`` 不做任何过滤，复制父注册表全部工具；遇到 AgentTool
实例时浅复制并标记 ``FORK_QUERY_SOURCE``，保持工具定义字节一致以命中 prompt cache。
"""

from __future__ import annotations

import copy
from typing import Any

from seacode.agents.fork import FORK_QUERY_SOURCE
from seacode.agents.parser import AgentDef
from seacode.tools import ToolRegistry

# 全局禁用工具集合；这些是主 Agent 专属的对话控制工具，子 Agent 不应调度。
ALL_AGENT_DISALLOWED_TOOLS: set[str] = {
    "TaskOutput",
    "ExitPlanMode",
    "EnterPlanMode",
    "Agent",
    "AskUserQuestion",
    "TaskStop",
    "Workflow",
}

# 自定义 agent 来源（project / user / plugin）额外禁用集合；内置子 Agent 不受限。
# 本步与 ALL_AGENT_DISALLOWED_TOOLS 相同；保留为独立常量以便后续差异化扩展。
CUSTOM_AGENT_DISALLOWED_TOOLS: set[str] = {
    "TaskOutput",
    "ExitPlanMode",
    "EnterPlanMode",
    "Agent",
    "AskUserQuestion",
    "TaskStop",
    "Workflow",
}

# 后台任务允许的工具白名单；包含 v1 既定的全部工具名。
# 列表中部分工具（如 NotebookEdit / Skill / SyntheticOutput 等）本步可能尚未实现，
# 调用未注册工具时由 ToolRegistry.get 返回 None 走既有"未知工具"路径处理。
ASYNC_AGENT_ALLOWED_TOOLS: set[str] = {
    "ReadFile",
    "WebSearch",
    "TodoWrite",
    "Grep",
    "WebFetch",
    "Glob",
    "Bash",
    "EditFile",
    "WriteFile",
    "NotebookEdit",
    "Skill",
    "LoadSkill",
    "SyntheticOutput",
    "ToolSearch",
    "EnterWorktree",
    "ExitWorktree",
}

# Coordinator 模式下 Lead 可用的工具白名单；只保留调度、只读探索与任务管理工具。
# mcp__ 前缀工具由 apply_coordinator_filter 单独放行，不在此集合中。
COORDINATOR_MODE_ALLOWED_TOOLS: set[str] = {
    "Agent",
    "SendMessage",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TaskStop",
    "SyntheticOutput",
    "TeamCreate",
    "TeamDelete",
    "ReadFile",
    "Glob",
    "Grep",
    "Bash",
}

# teammate 间协调工具；in-process 与 pane 后端均可用。
TEAMMATE_COORDINATION_TOOLS: set[str] = {
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "SendMessage",
}

# in-process teammate 允许的工具白名单；在 ASYNC_AGENT_ALLOWED_TOOLS 基础上
# 增加 TEAMMATE_COORDINATION_TOOLS 与 Cron 工具。
IN_PROCESS_TEAMMATE_ALLOWED_TOOLS: set[str] = (
    ASYNC_AGENT_ALLOWED_TOOLS
    | TEAMMATE_COORDINATION_TOOLS
    | {"CronCreate", "CronDelete", "CronList"}
)


# 按五层防线过滤父注册表工具，返回新的 ToolRegistry。
# is_background=True 时应用 ASYNC_AGENT_ALLOWED_TOOLS 白名单收拢。
def resolve_agent_tools(
    parent_registry: ToolRegistry,
    definition: AgentDef,
    is_background: bool,
) -> ToolRegistry:
    all_tools = list(parent_registry.list_tools())

    # (0) MCP 工具（mcp__ 前缀）始终放行，从后续过滤中分离。
    mcp_tools = [t for t in all_tools if t.name.startswith("mcp__")]
    other_tools = [t for t in all_tools if not t.name.startswith("mcp__")]

    # (1) 全局禁用：剥离 ALL_AGENT_DISALLOWED_TOOLS。
    other_tools = [
        t for t in other_tools if t.name not in ALL_AGENT_DISALLOWED_TOOLS
    ]

    # (2) 自定义限制：project / user / plugin 来源额外禁用。
    if definition.source in ("project", "user", "plugin"):
        other_tools = [
            t for t in other_tools if t.name not in CUSTOM_AGENT_DISALLOWED_TOOLS
        ]

    # (3) 后台白名单收拢：is_background=True 时只保留白名单内工具。
    if is_background:
        other_tools = [
            t for t in other_tools if t.name in ASYNC_AGENT_ALLOWED_TOOLS
        ]

    # (4) 定义层：disallowed_tools 黑名单优先，tools 白名单后置（白名单为空表示不限）。
    if definition.disallowed_tools:
        disallowed_set = set(definition.disallowed_tools)
        other_tools = [t for t in other_tools if t.name not in disallowed_set]
    if definition.tools:
        allowed_set = set(definition.tools)
        other_tools = [t for t in other_tools if t.name in allowed_set]

    new_registry = ToolRegistry()
    for tool in mcp_tools:
        new_registry.register(tool)
    for tool in other_tools:
        new_registry.register(tool)
    return new_registry


# 复制父注册表全部工具不过滤；AgentTool 实例浅复制并标记 FORK_QUERY_SOURCE。
# 保持工具定义字节一致以命中 prompt cache；fork 子 Agent 不能再次 fork。
def clone_registry_for_fork(parent_registry: ToolRegistry) -> ToolRegistry:
    # 延迟导入避免循环：AgentTool 引用本模块，本模块引用 FORK_QUERY_SOURCE 已无循环。
    from seacode.tools.agent_tool import AgentTool

    new_registry = ToolRegistry()
    for tool in parent_registry.list_tools():
        if isinstance(tool, AgentTool):
            fork_tool = copy.copy(tool)
            fork_tool.query_source = FORK_QUERY_SOURCE
            new_registry.register(fork_tool)
        else:
            new_registry.register(tool)
    return new_registry


# Coordinator 模式工具收敛：只保留 mcp__ 前缀工具 + COORDINATOR_MODE_ALLOWED_TOOLS 白名单。
# 返回新的 ToolRegistry；原 registry 不变。
def apply_coordinator_filter(parent_registry: ToolRegistry) -> ToolRegistry:
    new_registry = ToolRegistry()
    for tool in parent_registry.list_tools():
        if tool.name.startswith("mcp__"):
            new_registry.register(tool)
        elif tool.name in COORDINATOR_MODE_ALLOWED_TOOLS:
            new_registry.register(tool)
    return new_registry


# 按后端类型构造 teammate 工具注册表。
# IN_PROCESS 后端用 IN_PROCESS_TEAMMATE_ALLOWED_TOOLS 白名单；
# pane 后端（TMUX/ITERM2）保留全量工具但去掉 TeamCreate/TeamDelete，
# 附加 TEAMMATE_COORDINATION_TOOLS。
def build_teammate_tools(
    parent_registry: ToolRegistry, backend_type: Any
) -> ToolRegistry:
    from seacode.teams.models import BackendType

    new_registry = ToolRegistry()
    if backend_type == BackendType.IN_PROCESS:
        for tool in parent_registry.list_tools():
            if tool.name.startswith("mcp__"):
                new_registry.register(tool)
            elif tool.name in IN_PROCESS_TEAMMATE_ALLOWED_TOOLS:
                new_registry.register(tool)
    else:
        # pane 后端：去掉 TeamCreate/TeamDelete，保留其它 + 协调工具。
        for tool in parent_registry.list_tools():
            if tool.name in ("TeamCreate", "TeamDelete"):
                continue
            if tool.name.startswith("mcp__"):
                new_registry.register(tool)
            elif tool.name in TEAMMATE_COORDINATION_TOOLS:
                new_registry.register(tool)
            elif tool.name in ASYNC_AGENT_ALLOWED_TOOLS:
                new_registry.register(tool)
    return new_registry
