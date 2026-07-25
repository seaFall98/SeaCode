"""Coordinator Mode 端到端测试：TeamCreate 激活工具收敛 → 多 teammate spawn → TeamDelete 恢复全量。

使用真实 TeamManager / 工具过滤逻辑，fake Lead Agent 与 fake registry
验证 Coordinator Mode 的激活、工具收敛与恢复语义。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from seacode.agents.tool_filter import (
    COORDINATOR_MODE_ALLOWED_TOOLS,
    apply_coordinator_filter,
)
from seacode.teams.manager import TeamManager
from seacode.teams.models import BackendType, TeammateInfo
from seacode.teams.progress import TeammateProgress
from seacode.tools.base import Tool, ToolCategory, ToolResult
from seacode.tools.team_create import TeamCreateParams, TeamCreateTool
from seacode.tools.team_delete import TeamDeleteParams, TeamDeleteTool

# ---------------------------------------------------------------------------
# 测试辅助 fake 类
# ---------------------------------------------------------------------------


# 假工具：用于构造父注册表，验证 Coordinator 工具过滤。
class _FakeTool(Tool):
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.category = ToolCategory.READ
        self.params_model = BaseModel

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(content="")


# 假 ToolRegistry：list_tools 返回注入的工具列表，register 记录调用。
class _FakeRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self._tools[t.name] = t
        self.register_calls: list[Tool] = []

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        self.register_calls.append(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())


# 假父 Agent：提供 TeamCreate/Delete 所需的最小属性集合 + registry 可替换。
class _FakeLeadAgent:
    def __init__(self, registry: Any) -> None:
        self.agent_id = "lead-agent-id"
        self.coordinator_mode = False
        self.registry = registry
        self._full_registry: Any = None


# 构造带 enable_coordinator_mode 的 fake config。
def _make_config(enable_coordinator: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        teammate_mode="in-process",
        enable_coordinator_mode=enable_coordinator,
    )


# 构造包含白名单与非白名单工具的 registry。
def _make_full_registry() -> _FakeRegistry:
    # 白名单工具。
    tools = [
        _FakeTool("Agent"),
        _FakeTool("SendMessage"),
        _FakeTool("TaskCreate"),
        _FakeTool("ReadFile"),
        _FakeTool("Glob"),
        _FakeTool("Grep"),
        _FakeTool("Bash"),
    ]
    # 非白名单工具（应被 Coordinator 过滤掉）。
    tools.append(_FakeTool("WriteFile"))
    tools.append(_FakeTool("EditFile"))
    tools.append(_FakeTool("NonWhitelistTool"))
    return _FakeRegistry(tools)


# ---------------------------------------------------------------------------
# Coordinator Mode 激活测试
# ---------------------------------------------------------------------------


# 验证 TeamCreate 激活 Coordinator Mode：coordinator_mode=True，registry 收敛为白名单。
# enable_coordinator_mode=True 时 TeamCreateTool 保存 _full_registry 并 apply_coordinator_filter。
@pytest.mark.asyncio
async def test_team_create_activates_coordinator_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    full_registry = _make_full_registry()
    lead = _FakeLeadAgent(registry=full_registry)
    mgr = TeamManager()
    tool = TeamCreateTool(lead, mgr, _make_config(enable_coordinator=True))

    result = await tool.execute(TeamCreateParams(team_name="demo"))

    assert not result.is_error
    assert "coordinator mode 已激活" in result.content
    # coordinator_mode 设为 True。
    assert lead.coordinator_mode is True
    # _full_registry 保存原 registry。
    assert lead._full_registry is full_registry
    # registry 替换为过滤后的新 registry。
    assert lead.registry is not full_registry
    # 过滤后只含白名单工具。
    tool_names = {t.name for t in lead.registry.list_tools()}
    # 白名单工具应保留。
    assert "Agent" in tool_names
    assert "SendMessage" in tool_names
    assert "ReadFile" in tool_names
    assert "Bash" in tool_names
    # 非白名单工具应被过滤掉。
    assert "WriteFile" not in tool_names
    assert "EditFile" not in tool_names
    assert "NonWhitelistTool" not in tool_names


# 验证 enable_coordinator_mode=False 时不激活 Coordinator Mode。
# TeamCreateTool 不修改 registry，coordinator_mode 保持 False。
@pytest.mark.asyncio
async def test_team_create_no_coordinator_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    full_registry = _make_full_registry()
    lead = _FakeLeadAgent(registry=full_registry)
    mgr = TeamManager()
    tool = TeamCreateTool(lead, mgr, _make_config(enable_coordinator=False))

    result = await tool.execute(TeamCreateParams(team_name="demo"))

    assert not result.is_error
    assert "coordinator mode" not in result.content
    # coordinator_mode 保持 False。
    assert lead.coordinator_mode is False
    # registry 未被替换。
    assert lead.registry is full_registry
    # _full_registry 未被设置。
    assert lead._full_registry is None


# ---------------------------------------------------------------------------
# Coordinator Mode 恢复测试
# ---------------------------------------------------------------------------


# 验证 TeamDelete 恢复 Lead 全量工具集：coordinator_mode=False，registry 恢复。
# TeamCreate 激活 → TeamDelete 后 _full_registry 恢复到 registry，coordinator_mode=False。
@pytest.mark.asyncio
async def test_team_delete_restores_full_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    full_registry = _make_full_registry()
    lead = _FakeLeadAgent(registry=full_registry)
    mgr = TeamManager()
    create_tool = TeamCreateTool(lead, mgr, _make_config(enable_coordinator=True))
    await create_tool.execute(TeamCreateParams(team_name="demo"))

    # 激活后 registry 应为过滤后。
    assert lead.coordinator_mode is True
    assert lead.registry is not full_registry

    # TeamDelete 恢复全量。
    delete_tool = TeamDeleteTool(lead, mgr)
    result = await delete_tool.execute(TeamDeleteParams(team_name="demo"))

    assert not result.is_error
    assert "工具集恢复全量" in result.content
    # coordinator_mode 恢复 False。
    assert lead.coordinator_mode is False
    # registry 恢复为 full_registry。
    assert lead.registry is full_registry
    # _full_registry 清空。
    assert lead._full_registry is None


# 验证多团队场景：删除一个团队不恢复工具集，全部删除后才恢复。
# TeamCreate demo → TeamCreate demo2 → TeamDelete demo → coordinator 仍 True
# → TeamDelete demo2 → 恢复。
@pytest.mark.asyncio
async def test_multiple_teams_delete_all_restores_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    full_registry = _make_full_registry()
    lead = _FakeLeadAgent(registry=full_registry)
    mgr = TeamManager()
    create_tool = TeamCreateTool(lead, mgr, _make_config(enable_coordinator=True))

    # 创建两个团队。
    await create_tool.execute(TeamCreateParams(team_name="demo"))
    await create_tool.execute(TeamCreateParams(team_name="demo2"))
    assert lead.coordinator_mode is True
    assert "demo" in mgr.list_teams()
    assert "demo2" in mgr.list_teams()

    # 删除第一个团队：coordinator 仍 True（还有 demo2）。
    delete_tool = TeamDeleteTool(lead, mgr)
    await delete_tool.execute(TeamDeleteParams(team_name="demo"))
    assert lead.coordinator_mode is True
    assert lead.registry is not full_registry

    # 删除第二个团队：coordinator 恢复 False。
    await delete_tool.execute(TeamDeleteParams(team_name="demo2"))
    assert lead.coordinator_mode is False
    assert lead.registry is full_registry


# ---------------------------------------------------------------------------
# 工具过滤白名单验证
# ---------------------------------------------------------------------------


# 验证 apply_coordinator_filter 只保留白名单 + mcp__ 前缀工具。
# 构造含白名单、非白名单与 mcp__ 前缀工具的 registry，过滤后应保留白名单与 mcp__。
def test_apply_coordinator_filter_whitelist_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 构造含 mcp__ 前缀工具的 registry。
    tools = [
        _FakeTool("Agent"),
        _FakeTool("SendMessage"),
        _FakeTool("WriteFile"),  # 非白名单
        _FakeTool("mcp__custom_tool"),  # mcp__ 前缀应保留
        _FakeTool("mcp__another_tool"),  # mcp__ 前缀应保留
        _FakeTool("EditFile"),  # 非白名单
    ]
    registry = _FakeRegistry(tools)

    filtered = apply_coordinator_filter(registry)

    tool_names = {t.name for t in filtered.list_tools()}
    assert "Agent" in tool_names
    assert "SendMessage" in tool_names
    assert "mcp__custom_tool" in tool_names
    assert "mcp__another_tool" in tool_names
    # 非白名单且非 mcp__ 工具应被过滤。
    assert "WriteFile" not in tool_names
    assert "EditFile" not in tool_names


# 验证 COORDINATOR_MODE_ALLOWED_TOOLS 包含调度与只读探索工具。
# 白名单应含 Agent / SendMessage / TaskCreate / ReadFile / Glob / Grep / Bash。
def test_coordinator_mode_allowed_tools_contains_required() -> None:
    required = {
        "Agent",
        "SendMessage",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "TaskStop",
        "ReadFile",
        "Glob",
        "Grep",
        "Bash",
    }
    assert required.issubset(COORDINATOR_MODE_ALLOWED_TOOLS)


# ---------------------------------------------------------------------------
# 多 teammate progress 收集测试
# ---------------------------------------------------------------------------


# 验证多 teammate spawn 后 get_all_teammate_progress 返回多个。
# 创建团队 → 注册 alice 与 bob → 附加 progress → 收集应返回 2 个。
@pytest.mark.asyncio
async def test_multiple_teammates_progress_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    lead = _FakeLeadAgent(registry=_make_full_registry())
    create_tool = TeamCreateTool(lead, mgr, _make_config(enable_coordinator=False))
    await create_tool.execute(TeamCreateParams(team_name="demo"))

    # 注册 alice 与 bob，各自附加 progress。
    for name in ("alice", "bob"):
        progress = TeammateProgress(name=name, team_name="demo", status="running")
        member = TeammateInfo(
            name=name,
            agent_id=f"{name}-id",
            agent_type="teammate",
            model="test",
            worktree_path=f"/tmp/fake-{name}",
            backend_type=BackendType.IN_PROCESS,
            is_active=None,
            progress=progress,
        )
        mgr.register_member("demo", member)

    all_progress = mgr.get_all_teammate_progress()
    assert len(all_progress) == 2
    names = {p.name for p in all_progress}
    assert names == {"alice", "bob"}
