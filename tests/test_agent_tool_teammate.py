"""AgentTool Teammate 路径（_execute_as_teammate）单元测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from seacode.agents.parser import AgentDef
from seacode.teams.models import BackendType, TeammateInfo
from seacode.tools import ToolRegistry
from seacode.tools.agent_tool import (
    TEAMMATE_ADDENDUM,
    AgentTool,
    AgentToolParams,
)

# ---------------------------------------------------------------------------
# 测试辅助 fake 类
# ---------------------------------------------------------------------------


# 假 worktree：只携带 path 与 name 供 _execute_as_teammate 使用。
class _FakeWorktree:
    def __init__(self, path: str = "/tmp/wt") -> None:
        self.path = path
        self.name = "fake-wt"
        self.branch = "worktree-fake"
        self.based_on = "HEAD"
        self.head_commit = "abc123"
        self.created = None


# 假 WorktreeManager：create 返回固定 worktree，可配置抛异常。
class _FakeWorktreeManager:
    def __init__(
        self, wt: _FakeWorktree | None = None, raise_error: bool = False
    ) -> None:
        self._wt = wt or _FakeWorktree()
        self._raise = raise_error
        self.create_calls: list[tuple[str, str]] = []

    async def create(self, name: str, base_branch: str = "HEAD") -> Any:
        self.create_calls.append((name, base_branch))
        if self._raise:
            from seacode.worktree.manager import WorktreeError

            raise WorktreeError("create failed")
        return self._wt


# 假团队：持有 members 列表，支持 get_member 去重检查。
class _FakeTeam:
    def __init__(self, members: list[TeammateInfo] | None = None) -> None:
        self.members = members or []
        self.lead_agent_id = "lead-id"

    def get_member(self, name: str) -> TeammateInfo | None:
        return next((m for m in self.members if m.name == name), None)


# 假 TeamManager：记录所有调用参数，支持配置 detect_backend 返回值。
class _FakeTeamManager:
    def __init__(
        self,
        backend: BackendType = BackendType.IN_PROCESS,
        team: _FakeTeam | None = None,
    ) -> None:
        self._backend = backend
        self._team = team
        self.detect_calls: list[tuple[str, bool]] = []
        self.mailbox = MagicMock()
        self.register_inprocess_calls: list[tuple[str, str, Any]] = []
        self.register_pane_calls: list[tuple[str, str, str]] = []
        self.register_member_calls: list[tuple[str, TeammateInfo]] = []

    def detect_backend(self, teammate_mode: str, is_interactive: bool) -> BackendType:
        self.detect_calls.append((teammate_mode, is_interactive))
        return self._backend

    def get_team(self, name: str) -> Any:
        return self._team

    def get_mailbox(self, name: str) -> Any:
        return self.mailbox

    def register_inprocess_handle(
        self, team_name: str, member_name: str, handle: Any
    ) -> None:
        self.register_inprocess_calls.append((team_name, member_name, handle))

    def register_pane_id(
        self, team_name: str, member_name: str, pane_id: str
    ) -> None:
        self.register_pane_calls.append((team_name, member_name, pane_id))

    def register_member(
        self, team_name: str, member: TeammateInfo
    ) -> None:
        self.register_member_calls.append((team_name, member))


# 假父 Agent：提供 _full_registry、client、protocol 等属性。
class _FakeParent:
    def __init__(self) -> None:
        self.agent_id = "parent-id"
        self.protocol = "anthropic"
        self.context_window = 200_000
        self.client = object()
        self._full_registry = ToolRegistry()


# 假 AgentLoader：get 返回 None（默认 teammate 不走 loader）。
class _FakeLoader:
    def __init__(self, agent_def: AgentDef | None = None) -> None:
        self._agent_def = agent_def

    def get(self, name: str) -> AgentDef | None:
        return self._agent_def

    def list_agents(self) -> list[tuple[str, str]]:
        return []


# 假 TraceManager / TaskManager：AgentTool 构造需要但 teammate 路径不使用。
class _FakeTraceManager:
    def create(self, *args: Any, **kwargs: Any) -> Any:
        return MagicMock(agent_id="trace-id")

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass

    def complete(self, *args: Any, **kwargs: Any) -> None:
        pass


class _FakeTaskManager:
    pass


# 假 handle：spawn_inprocess_teammate 的返回值。
class _FakeHandle:
    def __init__(self) -> None:
        self.task = MagicMock()
        self.progress = MagicMock()


# 构造 AgentTool 实例；team_manager / worktree_manager 由调用方注入。
def _make_tool(
    *,
    team_manager: Any = None,
    worktree_manager: Any = None,
    loader: Any = None,
) -> AgentTool:
    return AgentTool(
        agent_loader=loader or _FakeLoader(),
        task_manager=_FakeTaskManager(),
        trace_manager=_FakeTraceManager(),
        parent_agent=_FakeParent(),
        enable_fork=False,
        provider_config=None,
        worktree_manager=worktree_manager,
        team_manager=team_manager,
    )


# ---------------------------------------------------------------------------
# AgentToolParams 字段测试
# ---------------------------------------------------------------------------


# 验证 AgentToolParams 的 team_name 与 name 默认为 None。
# 构造无参实例，确认两个新字段缺省值正确。
def test_agent_tool_params_defaults() -> None:
    params = AgentToolParams()
    assert params.team_name is None
    assert params.name is None


# 验证 AgentToolParams 的 team_name 与 name 可显式设置。
# 构造带参实例，确认字段值与传入一致。
def test_agent_tool_params_team_name_and_name_set() -> None:
    params = AgentToolParams(team_name="demo", name="alice")
    assert params.team_name == "demo"
    assert params.name == "alice"


# ---------------------------------------------------------------------------
# _execute_as_teammate 前置检查
# ---------------------------------------------------------------------------


# 验证 team_manager 未初始化时返回 is_error=True。
# 构造无 team_manager 的 tool，调用 execute 传 team_name，确认错误返回。
@pytest.mark.asyncio
async def test_execute_as_teammate_no_team_manager() -> None:
    tool = _make_tool(
        team_manager=None,
        worktree_manager=_FakeWorktreeManager(),
    )
    params = AgentToolParams(team_name="demo", prompt="hi")
    result = await tool.execute(params, conversation=None, parent_agent=_FakeParent())
    assert result.is_error is True
    assert "team_manager" in result.content


# 验证 worktree_manager 未初始化时返回 is_error=True。
# 构造有 team_manager 但无 worktree_manager 的 tool，确认错误返回。
@pytest.mark.asyncio
async def test_execute_as_teammate_no_worktree_manager() -> None:
    tool = _make_tool(
        team_manager=_FakeTeamManager(),
        worktree_manager=None,
    )
    params = AgentToolParams(team_name="demo", prompt="hi")
    result = await tool.execute(params, conversation=None, parent_agent=_FakeParent())
    assert result.is_error is True
    assert "worktree" in result.content.lower()


# 验证 worktree 创建失败时返回 is_error=True。
# 配置 _FakeWorktreeManager 抛 WorktreeError，确认错误返回且不含 spawn 调用。
@pytest.mark.asyncio
async def test_execute_as_teammate_worktree_create_failed() -> None:
    tool = _make_tool(
        team_manager=_FakeTeamManager(),
        worktree_manager=_FakeWorktreeManager(raise_error=True),
    )
    params = AgentToolParams(team_name="demo", prompt="hi")
    result = await tool.execute(params, conversation=None, parent_agent=_FakeParent())
    assert result.is_error is True
    assert "worktree" in result.content


# ---------------------------------------------------------------------------
# _execute_as_teammate in-process 后端全流程
# ---------------------------------------------------------------------------


# 验证 in-process 后端六步全流程：建 worktree → 过滤工具 → spawn → 注册。
# mock spawn_inprocess_teammate 返回 fake handle，
# 确认 register_inprocess_handle 与 register_member 调用。
@pytest.mark.asyncio
async def test_execute_as_teammate_in_process_full_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_manager = _FakeTeamManager(
        backend=BackendType.IN_PROCESS, team=_FakeTeam()
    )
    tool = _make_tool(
        team_manager=team_manager,
        worktree_manager=_FakeWorktreeManager(),
    )

    fake_handle = _FakeHandle()
    spawn_calls: list[dict[str, Any]] = []

    def fake_spawn(agent, task, name, tm, mailbox=None):
        spawn_calls.append(
            {"agent": agent, "task": task, "name": name, "tm": tm, "mailbox": mailbox}
        )
        return fake_handle

    monkeypatch.setattr(
        "seacode.tools.agent_tool.spawn_inprocess_teammate", fake_spawn, raising=False
    )
    # _execute_as_teammate 内部延迟导入 spawn_inprocess_teammate，需 patch 源模块。
    import seacode.teams.spawn_inprocess as spawn_mod

    monkeypatch.setattr(spawn_mod, "spawn_inprocess_teammate", fake_spawn)

    registry_calls: list[tuple[str, str]] = []

    def fake_register(name, agent_id):
        registry_calls.append((name, agent_id))

    fake_registry = MagicMock()
    fake_registry.register = fake_register
    monkeypatch.setattr(
        "seacode.teams.registry.AgentNameRegistry.instance",
        lambda: fake_registry,
    )

    params = AgentToolParams(
        team_name="demo", name="alice", prompt="read README.md"
    )
    result = await tool.execute(
        params, conversation=None, parent_agent=_FakeParent()
    )

    assert result.is_error is False
    assert "alice" in result.content
    assert "in-process" in result.content
    # 确认 spawn 调用参数正确。
    assert len(spawn_calls) == 1
    assert spawn_calls[0]["name"] == "alice"
    assert spawn_calls[0]["task"] == "read README.md"
    # 确认 register_inprocess_handle 调用。
    assert len(team_manager.register_inprocess_calls) == 1
    assert team_manager.register_inprocess_calls[0][0] == "demo"
    assert team_manager.register_inprocess_calls[0][1] == "alice"
    # 确认 register_member 调用。
    assert len(team_manager.register_member_calls) == 1
    member = team_manager.register_member_calls[0][1]
    assert member.name == "alice"
    assert member.backend_type == BackendType.IN_PROCESS
    # 确认 AgentNameRegistry.register 调用。
    assert len(registry_calls) == 1
    assert registry_calls[0][0] == "alice"


# ---------------------------------------------------------------------------
# _execute_as_teammate tmux 后端
# ---------------------------------------------------------------------------


# 验证 tmux 后端调用 spawn_tmux_teammate 与 register_pane_id。
# mock detect_backend 返回 TMUX，确认 pane spawn 路径与 pane_id 注册。
@pytest.mark.asyncio
async def test_execute_as_teammate_tmux_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from seacode.teams.spawn_tmux import TmuxPaneInfo

    team_manager = _FakeTeamManager(
        backend=BackendType.TMUX, team=_FakeTeam()
    )
    tool = _make_tool(
        team_manager=team_manager,
        worktree_manager=_FakeWorktreeManager(),
    )

    tmux_calls: list[tuple[str, str, str]] = []

    def fake_spawn_tmux(team_name, member_name, workdir):
        tmux_calls.append((team_name, member_name, workdir))
        return TmuxPaneInfo(pane_id="%5", window_name="demo-alice")

    import seacode.teams.spawn_tmux as tmux_mod

    monkeypatch.setattr(tmux_mod, "spawn_tmux_teammate", fake_spawn_tmux)

    fake_registry = MagicMock()
    monkeypatch.setattr(
        "seacode.teams.registry.AgentNameRegistry.instance",
        lambda: fake_registry,
    )

    params = AgentToolParams(team_name="demo", name="alice", prompt="hi")
    result = await tool.execute(
        params, conversation=None, parent_agent=_FakeParent()
    )

    assert result.is_error is False
    assert "tmux" in result.content
    assert len(tmux_calls) == 1
    assert tmux_calls[0][0] == "demo"
    assert tmux_calls[0][1] == "alice"
    assert len(team_manager.register_pane_calls) == 1
    assert team_manager.register_pane_calls[0][2] == "%5"
    # tmux 后端不调用 register_inprocess_handle。
    assert len(team_manager.register_inprocess_calls) == 0


# ---------------------------------------------------------------------------
# _unique_teammate_name 去重
# ---------------------------------------------------------------------------


# 验证 _unique_teammate_name 同名时追加 -2 / -3。
# 构造含 "alice" 的 team，确认第二次 spawn 返回 "alice-2"。
@pytest.mark.asyncio
async def test_unique_teammate_name_appends_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = TeammateInfo(
        name="alice",
        agent_id="id-1",
        agent_type="teammate",
        model="inherit",
        worktree_path="/tmp",
        backend_type=BackendType.IN_PROCESS,
    )
    team_manager = _FakeTeamManager(
        backend=BackendType.IN_PROCESS, team=_FakeTeam(members=[existing])
    )
    tool = _make_tool(
        team_manager=team_manager,
        worktree_manager=_FakeWorktreeManager(),
    )

    import seacode.teams.spawn_inprocess as spawn_mod

    monkeypatch.setattr(
        spawn_mod, "spawn_inprocess_teammate", lambda *a, **kw: _FakeHandle()
    )
    fake_registry = MagicMock()
    monkeypatch.setattr(
        "seacode.teams.registry.AgentNameRegistry.instance",
        lambda: fake_registry,
    )

    params = AgentToolParams(team_name="demo", name="alice", prompt="hi")
    result = await tool.execute(
        params, conversation=None, parent_agent=_FakeParent()
    )

    assert result.is_error is False
    assert "alice-2" in result.content
    member = team_manager.register_member_calls[0][1]
    assert member.name == "alice-2"


# ---------------------------------------------------------------------------
# TEAMMATE_ADDENDUM 注入
# ---------------------------------------------------------------------------


# 验证 TEAMMATE_ADDENDUM 含 [TEAMMATE CONTEXT] 标记。
# 直接检查常量内容，确认提示词附加段格式正确。
def test_teammate_addendum_contains_context_marker() -> None:
    assert "[TEAMMATE CONTEXT]" in TEAMMATE_ADDENDUM
    assert "[/TEAMMATE CONTEXT]" in TEAMMATE_ADDENDUM
    assert "SendMessage" in TEAMMATE_ADDENDUM


# 验证 teammate Agent 的 _current_definition.system_prompt 含 TEAMMATE_ADDENDUM。
# 执行 in-process 路径，捕获 spawn 的 agent 参数，检查其 _current_definition。
@pytest.mark.asyncio
async def test_teammate_addendum_injected_into_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_manager = _FakeTeamManager(
        backend=BackendType.IN_PROCESS, team=_FakeTeam()
    )
    tool = _make_tool(
        team_manager=team_manager,
        worktree_manager=_FakeWorktreeManager(),
    )

    captured_agent: list[Any] = []

    def fake_spawn(agent, task, name, tm, mailbox=None):
        captured_agent.append(agent)
        return _FakeHandle()

    import seacode.teams.spawn_inprocess as spawn_mod

    monkeypatch.setattr(spawn_mod, "spawn_inprocess_teammate", fake_spawn)
    fake_registry = MagicMock()
    monkeypatch.setattr(
        "seacode.teams.registry.AgentNameRegistry.instance",
        lambda: fake_registry,
    )

    params = AgentToolParams(team_name="demo", name="alice", prompt="hi")
    await tool.execute(params, conversation=None, parent_agent=_FakeParent())

    assert len(captured_agent) == 1
    definition = captured_agent[0]._current_definition
    assert "[TEAMMATE CONTEXT]" in definition.system_prompt


# ---------------------------------------------------------------------------
# worktree 名称格式
# ---------------------------------------------------------------------------


# 验证 worktree 名称格式为 team-<team>/<member>。
# 执行 in-process 路径，检查 WorktreeManager.create 的 name 参数。
@pytest.mark.asyncio
async def test_worktree_name_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_manager = _FakeTeamManager(
        backend=BackendType.IN_PROCESS, team=_FakeTeam()
    )
    wt_manager = _FakeWorktreeManager()
    tool = _make_tool(
        team_manager=team_manager,
        worktree_manager=wt_manager,
    )

    import seacode.teams.spawn_inprocess as spawn_mod

    monkeypatch.setattr(
        spawn_mod, "spawn_inprocess_teammate", lambda *a, **kw: _FakeHandle()
    )
    fake_registry = MagicMock()
    monkeypatch.setattr(
        "seacode.teams.registry.AgentNameRegistry.instance",
        lambda: fake_registry,
    )

    params = AgentToolParams(team_name="demo", name="alice", prompt="hi")
    await tool.execute(params, conversation=None, parent_agent=_FakeParent())

    assert len(wt_manager.create_calls) == 1
    assert wt_manager.create_calls[0][0] == "team-demo/alice"
