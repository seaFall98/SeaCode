"""tool_filter + teams/__init__ 单测：Coordinator 过滤、teammate 工具构造、子包导出。"""

from __future__ import annotations

from pydantic import BaseModel

from seacode.agents.tool_filter import (
    COORDINATOR_MODE_ALLOWED_TOOLS,
    apply_coordinator_filter,
    build_teammate_tools,
)
from seacode.teams.models import BackendType
from seacode.tools import ToolRegistry
from seacode.tools.base import Tool, ToolCategory, ToolResult


# 占位工具：用于测试过滤行为。
class _FakeParams(BaseModel):
    pass


class _FakeTool(Tool):
    params_model = _FakeParams
    category = ToolCategory.READ

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"fake {name}"

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(content="")


# 验证 COORDINATOR_MODE_ALLOWED_TOOLS 含 14 个调度/只读工具。
# 断言关键工具名都在白名单中。
def test_coordinator_mode_allowed_tools_contents() -> None:
    expected = {
        "Agent", "SendMessage", "TaskCreate", "TaskGet", "TaskList",
        "TaskUpdate", "TaskStop", "SyntheticOutput", "TeamCreate",
        "TeamDelete", "ReadFile", "Glob", "Grep", "Bash",
    }
    assert expected.issubset(COORDINATOR_MODE_ALLOWED_TOOLS)


# 验证 apply_coordinator_filter 保留 mcp__ 前缀 + 白名单工具。
# 构造含 ReadFile / EditFile / mcp__foo / WriteFile 的 registry，过滤后含 ReadFile / mcp__foo。
def test_apply_coordinator_filter_keeps_whitelist_and_mcp() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("ReadFile"))
    registry.register(_FakeTool("EditFile"))
    registry.register(_FakeTool("WriteFile"))
    registry.register(_FakeTool("mcp__foo"))
    registry.register(_FakeTool("Agent"))

    filtered = apply_coordinator_filter(registry)
    names = {t.name for t in filtered.list_tools()}
    assert "ReadFile" in names
    assert "mcp__foo" in names
    assert "Agent" in names
    assert "EditFile" not in names
    assert "WriteFile" not in names


# 验证 apply_coordinator_filter 空 registry 返回空。
def test_apply_coordinator_filter_empty() -> None:
    registry = ToolRegistry()
    filtered = apply_coordinator_filter(registry)
    assert list(filtered.list_tools()) == []


# 验证 build_teammate_tools(IN_PROCESS) 用 IN_PROCESS_TEAMMATE_ALLOWED_TOOLS 过滤，
# 并实例化 5 个绑定身份的协调工具（TaskCreate/Get/List/Update + SendMessage）。
# 含 ReadFile / EditFile / TaskCreate / SendMessage，不含 TeamCreate / TeamDelete。
def test_build_teammate_tools_in_process() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("ReadFile"))
    registry.register(_FakeTool("EditFile"))
    registry.register(_FakeTool("TaskCreate"))
    registry.register(_FakeTool("SendMessage"))
    registry.register(_FakeTool("TeamCreate"))
    registry.register(_FakeTool("TeamDelete"))

    # team_manager 用 None；协调工具构造时不访问 team_manager，仅 execute 时才用。
    filtered = build_teammate_tools(
        registry,
        team_manager=None,
        team_name="t1",
        agent_id="aid-1",
        agent_name="alice",
        backend_type=BackendType.IN_PROCESS,
    )
    names = {t.name for t in filtered.list_tools()}
    assert "ReadFile" in names
    assert "EditFile" in names
    # 协调工具由 build_teammate_tools 实例化注册，覆盖父注册表中的同名占位。
    assert "TaskCreate" in names
    assert "TaskGet" in names
    assert "TaskList" in names
    assert "TaskUpdate" in names
    assert "SendMessage" in names
    assert "TeamCreate" not in names
    assert "TeamDelete" not in names


# 验证 build_teammate_tools(TMUX) 去掉 TeamCreate/TeamDelete，保留其它 + 协调工具。
# 含 ReadFile / EditFile / TaskCreate，不含 TeamCreate / TeamDelete。
def test_build_teammate_tools_tmux() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("ReadFile"))
    registry.register(_FakeTool("EditFile"))
    registry.register(_FakeTool("TaskCreate"))
    registry.register(_FakeTool("TeamCreate"))
    registry.register(_FakeTool("TeamDelete"))

    filtered = build_teammate_tools(
        registry,
        team_manager=None,
        team_name="t1",
        agent_id="aid-1",
        agent_name="alice",
        backend_type=BackendType.TMUX,
    )
    names = {t.name for t in filtered.list_tools()}
    assert "ReadFile" in names
    assert "EditFile" in names
    assert "TaskCreate" in names
    assert "TaskGet" in names
    assert "TaskList" in names
    assert "TaskUpdate" in names
    assert "SendMessage" in names
    assert "TeamCreate" not in names
    assert "TeamDelete" not in names


# 验证 teams.__init__ 导出所有公开类与函数。
# from seacode.teams import 成功导入关键符号。
def test_teams_init_exports() -> None:
    from seacode.teams import (
        AgentNameRegistry,
        AgentTeam,
        BackendType,
        Mailbox,
        SharedTaskStore,
        TeamManager,
        create_message,
        resolve_team_dir,
        unique_team_name,
    )
    # 确认都是可调用或类型。
    assert callable(AgentNameRegistry)
    assert callable(AgentTeam)
    assert callable(BackendType)
    assert callable(Mailbox)
    assert callable(SharedTaskStore)
    assert callable(TeamManager)
    assert callable(create_message)
    assert callable(resolve_team_dir)
    assert callable(unique_team_name)
