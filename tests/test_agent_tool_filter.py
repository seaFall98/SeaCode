"""工具过滤单元测试：覆盖五层过滤常量、resolve_agent_tools、clone_registry_for_fork。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from seacode.agents.fork import FORK_QUERY_SOURCE
from seacode.agents.parser import AgentDef
from seacode.agents.tool_filter import (
    ALL_AGENT_DISALLOWED_TOOLS,
    ASYNC_AGENT_ALLOWED_TOOLS,
    CUSTOM_AGENT_DISALLOWED_TOOLS,
    FORK_DISALLOWED_TOOLS,
    clone_registry_for_fork,
    resolve_agent_tools,
)
from seacode.tools import ToolRegistry
from seacode.tools.base import Tool, ToolCategory, ToolResult


# 假工具基类：用最小实现注册到 ToolRegistry 供过滤测试使用。
class _FakeTool(Tool):
    # 用类变量设置名称；description 与 params_model 提供最小默认。
    name = "FakeTool"
    description = "fake"
    category = ToolCategory.READ
    params_model = BaseModel

    async def execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
        return ToolResult(content="")


# 构造一个指定名称的假工具类并返回实例。
def _make_tool(name: str) -> Tool:
    cls = type(
        f"_Tool_{name}",
        (_FakeTool,),
        {"name": name, "description": f"tool {name}"},
    )
    return cls()


# 构造含若干工具的父注册表。
def _make_parent_registry(tool_names: list[str]) -> ToolRegistry:
    reg = ToolRegistry()
    for name in tool_names:
        reg.register(_make_tool(name))
    return reg


# 构造 AgentDef；source 控制自定义限制是否生效。
def _make_def(
    *,
    source: str = "project",
    tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    isolation: str = "",
) -> AgentDef:
    return AgentDef(
        agent_type="x",
        when_to_use="y",
        system_prompt="z",
        tools=tools or [],
        disallowed_tools=disallowed_tools or [],
        source=source,
        isolation=isolation,
    )


# ---------------------------------------------------------------------------
# 过滤常量
# ---------------------------------------------------------------------------


# 验证 ALL_AGENT_DISALLOWED_TOOLS 含规格定义的 7 个工具名。
# 直接断言集合含 7 个工具名。
def test_all_agent_disallowed_tools_contains_seven_tools() -> None:
    expected = {
        "TaskOutput",
        "ExitPlanMode",
        "EnterPlanMode",
        "Agent",
        "AskUserQuestion",
        "TaskStop",
        "Workflow",
    }
    for name in expected:
        assert name in ALL_AGENT_DISALLOWED_TOOLS


# 验证 CUSTOM_AGENT_DISALLOWED_TOOLS 与全局禁用集合相同。
# 直接断言两个集合相等。
def test_custom_agent_disallowed_tools_equals_all_agent_disallowed() -> None:
    assert ALL_AGENT_DISALLOWED_TOOLS == CUSTOM_AGENT_DISALLOWED_TOOLS


# 验证 ASYNC_AGENT_ALLOWED_TOOLS 含规格定义的 16 个工具名。
# 直接断言集合含 16 个工具名。
def test_async_agent_allowed_tools_contains_sixteen_tools() -> None:
    expected = {
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
    for name in expected:
        assert name in ASYNC_AGENT_ALLOWED_TOOLS


# ---------------------------------------------------------------------------
# resolve_agent_tools 五层过滤
# ------------------------------------------------------------------


# 验证 MCP 工具（mcp__ 前缀）始终放行。
# 父注册表含 mcp__server__tool 工具，resolve 后断言新注册表保留该工具。
def test_resolve_agent_tools_passes_mcp_tools() -> None:
    reg = _make_parent_registry(["mcp__server__tool", "ReadFile"])
    definition = _make_def()
    new_reg = resolve_agent_tools(reg, definition, is_background=False)
    assert new_reg.get("mcp__server__tool") is not None
    assert new_reg.get("ReadFile") is not None


# 验证 ALL_AGENT_DISALLOWED_TOOLS 全局禁用生效。
# 父注册表含 Agent 工具，resolve 后断言 Agent 不在新注册表中。
def test_resolve_agent_tools_filters_global_disallowed() -> None:
    reg = _make_parent_registry(["Agent", "ReadFile"])
    definition = _make_def()
    new_reg = resolve_agent_tools(reg, definition, is_background=False)
    assert new_reg.get("Agent") is None
    assert new_reg.get("ReadFile") is not None


# 验证 CUSTOM_AGENT_DISALLOWED_TOOLS 对 project 来源生效。
# 父注册表含 TaskOutput 工具，source=project，resolve 后断言 TaskOutput 不在。
def test_resolve_agent_tools_custom_disallowed_for_project_source() -> None:
    reg = _make_parent_registry(["TaskOutput", "ReadFile"])
    definition = _make_def(source="project")
    new_reg = resolve_agent_tools(reg, definition, is_background=False)
    assert new_reg.get("TaskOutput") is None


# 验证 CUSTOM_AGENT_DISALLOWED_TOOLS 对 builtin 来源不额外生效。
# 父注册表含 ReadFile 工具，source=builtin，resolve 后断言 ReadFile 保留。
def test_resolve_agent_tools_builtin_source_skips_custom_disallowed() -> None:
    reg = _make_parent_registry(["ReadFile"])
    definition = _make_def(source="builtin")
    new_reg = resolve_agent_tools(reg, definition, is_background=False)
    # builtin 来源不应用 CUSTOM_AGENT_DISALLOWED_TOOLS；ReadFile 不在该集合中所以保留。
    assert new_reg.get("ReadFile") is not None


# 验证后台白名单收拢非白名单工具。
# 父注册表含 ReadFile（白名单）与 SomeOtherTool（非白名单），is_background=True，
# 断言 SomeOtherTool 不在。
def test_resolve_agent_tools_background_whitelist_filters_non_whitelist() -> None:
    reg = _make_parent_registry(["ReadFile", "SomeOtherTool"])
    definition = _make_def()
    new_reg = resolve_agent_tools(reg, definition, is_background=True)
    assert new_reg.get("ReadFile") is not None
    assert new_reg.get("SomeOtherTool") is None


# 验证后台白名单收拢 Agent 工具。
# 父注册表含 Agent 工具，is_background=True，断言 Agent 不在（被全局禁用+白名单双重过滤）。
def test_resolve_agent_tools_background_filters_agent() -> None:
    reg = _make_parent_registry(["Agent", "ReadFile"])
    definition = _make_def()
    new_reg = resolve_agent_tools(reg, definition, is_background=True)
    assert new_reg.get("Agent") is None
    assert new_reg.get("ReadFile") is not None


# 验证定义层 disallowed_tools 应用。
# 父注册表含 Bash 工具，definition.disallowed_tools=[Bash]，resolve 后断言 Bash 不在。
def test_resolve_agent_tools_applies_definition_disallowed() -> None:
    reg = _make_parent_registry(["Bash", "ReadFile"])
    definition = _make_def(disallowed_tools=["Bash"])
    new_reg = resolve_agent_tools(reg, definition, is_background=False)
    assert new_reg.get("Bash") is None
    assert new_reg.get("ReadFile") is not None


# 验证定义层 tools 白名单应用。
# 父注册表含 ReadFile 与 Bash，definition.tools=[ReadFile]，resolve 后断言只保留 ReadFile。
def test_resolve_agent_tools_applies_definition_tools_whitelist() -> None:
    reg = _make_parent_registry(["ReadFile", "Bash"])
    definition = _make_def(tools=["ReadFile"])
    new_reg = resolve_agent_tools(reg, definition, is_background=False)
    assert new_reg.get("ReadFile") is not None
    assert new_reg.get("Bash") is None


# 验证五层过滤顺序：MCP 放行 + 全局禁用 + 自定义限制 + 后台白名单 + 定义层。
# 父注册表含 mcp__x / Agent / ReadFile / SomeOther，断言 mcp__x 保留 / Agent 禁用 /
# ReadFile 保留 / SomeOther 禁用。
def test_resolve_agent_tools_five_layer_order() -> None:
    reg = _make_parent_registry(["mcp__x", "Agent", "ReadFile", "SomeOther"])
    definition = _make_def(source="project")
    new_reg = resolve_agent_tools(reg, definition, is_background=True)
    assert new_reg.get("mcp__x") is not None  # MCP 放行
    assert new_reg.get("Agent") is None  # 全局禁用
    assert new_reg.get("ReadFile") is not None  # 白名单
    assert new_reg.get("SomeOther") is None  # 白名单收拢


# 验证 resolve_agent_tools 返回新注册表而非修改原注册表。
# resolve 后断言原父注册表工具数不变。
def test_resolve_agent_tools_returns_new_registry() -> None:
    reg = _make_parent_registry(["Agent", "ReadFile"])
    definition = _make_def()
    new_reg = resolve_agent_tools(reg, definition, is_background=False)
    # 原注册表保留 Agent。
    assert reg.get("Agent") is not None
    # 新注册表不含 Agent。
    assert new_reg.get("Agent") is None


# ---------------------------------------------------------------------------
# clone_registry_for_fork
# ------------------------------------------------------------------


# 验证 Fork 注册表剥离主界面交互控制工具，并保留普通读写工具。
# 父注册表含全局控制工具与普通工具，clone 后断言控制工具全部不可见。
def test_clone_registry_for_fork_filters_interactive_controls() -> None:
    controls = sorted(FORK_DISALLOWED_TOOLS | {"Agent"})
    reg = _make_parent_registry(["ReadFile", "Bash", *controls])
    new_reg = clone_registry_for_fork(reg)
    assert new_reg.get("ReadFile") is not None
    assert new_reg.get("Bash") is not None
    for name in controls:
        assert new_reg.get(name) is None


# 验证 clone_registry_for_fork 把 AgentTool 实例标记 FORK_QUERY_SOURCE。
# 父注册表含真实 AgentTool 实例，clone 后断言其 query_source 为 FORK_QUERY_SOURCE。
def test_clone_registry_for_fork_marks_agent_tool_with_query_source() -> None:
    # 延迟导入避免循环。
    from seacode.tools.agent_tool import AgentTool

    # 构造最小 AgentTool 实例；不依赖真实 loader/tm/trm/parent。
    loader = _FakeLoader()
    tm = _FakeTaskManager()
    trm = _FakeTraceManager()
    parent = _FakeParent()
    agent_tool = AgentTool(loader, tm, trm, parent, enable_fork=False)
    reg = ToolRegistry()
    reg.register(agent_tool)
    new_reg = clone_registry_for_fork(reg)
    fork_tool = new_reg.get("Agent")
    assert fork_tool is not None
    assert fork_tool is not agent_tool  # 是浅复制
    assert isinstance(fork_tool, AgentTool)
    assert fork_tool.query_source == FORK_QUERY_SOURCE


# 验证 clone_registry_for_fork 非 AgentTool 工具不标记 query_source。
# 父注册表含 _FakeTool，clone 后断言是同一对象且不带 query_source。
def test_clone_registry_for_fork_keeps_non_agent_tool_as_is() -> None:
    reg = ToolRegistry()
    fake_tool = _make_tool("ReadFile")
    reg.register(fake_tool)
    new_reg = clone_registry_for_fork(reg)
    cloned = new_reg.get("ReadFile")
    assert cloned is fake_tool  # 非 AgentTool 直接复用原对象
    assert not hasattr(cloned, "query_source") or getattr(cloned, "query_source", None) is None


# 验证 clone_registry_for_fork 返回新注册表。
# clone 后断言原父注册表与新注册表是不同对象。
def test_clone_registry_for_fork_returns_new_registry() -> None:
    reg = _make_parent_registry(["ReadFile", "Bash"])
    new_reg = clone_registry_for_fork(reg)
    assert new_reg is not reg


# ---------------------------------------------------------------------------
# 测试辅助 fake 类
# ---------------------------------------------------------------------------


class _FakeLoader:
    """假 AgentLoader；提供 get / list_agents 最小实现。"""

    def get(self, name: str) -> Any:
        return None

    def list_agents(self) -> list[tuple[str, str]]:
        return []


class _FakeTaskManager:
    """假 TaskManager；提供 launch 最小实现。"""

    async def launch(
        self, agent: Any, task: str, name: str, fork_conversation: Any = None
    ) -> str:
        return "fake-id"


class _FakeTraceManager:
    """假 TraceManager；提供 create / update / complete 最小实现。"""

    def create(self, agent_type: str, parent_id: Any, trace_id: str) -> Any:
        class _FakeNode:
            agent_id = "fake-agent-id"

        return _FakeNode()

    def update(self, agent_id: str, **kwargs: Any) -> None:
        pass

    def complete(self, agent_id: str, status: str = "completed") -> None:
        pass


class _FakeParent:
    """假父 Agent；提供 AgentTool 所需的最小属性。"""

    def __init__(self) -> None:
        self.agent_id = "parent-id"
        self.trace_id = "trace-id"
        self.max_iterations = 100
        self.protocol = "anthropic"
        self.work_dir = "."
        self.context_window = 200_000
        self.instructions_content = ""
        self.permission_mode = None
        self.permission_checker = None
        self.hook_engine = None
        self.client = object()
        self._full_registry = ToolRegistry()
        self.replacement_state = None
