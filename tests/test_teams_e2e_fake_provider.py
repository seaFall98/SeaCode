"""端到端假 Provider 测试：TeamCreate → spawn → SendMessage → drain → TeamDelete 全链路。

使用真实 TeamManager / Mailbox / 团队工具，fake LLM client 与 fake WorktreeManager
避免真实 git 与网络调用。验证团队协调的关键集成路径与持久化边界。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from seacode.teams.mailbox import create_message
from seacode.teams.manager import TeamManager
from seacode.teams.models import BackendType, TeammateInfo
from seacode.teams.registry import AgentNameRegistry
from seacode.teams.spawn_inprocess import LEAD_NAME
from seacode.tools.send_message import SendMessageParams, SendMessageTool
from seacode.tools.team_create import TeamCreateParams, TeamCreateTool
from seacode.tools.team_delete import TeamDeleteParams, TeamDeleteTool

# ---------------------------------------------------------------------------
# 测试辅助 fake 类
# ---------------------------------------------------------------------------


# 假父 Agent：提供 TeamCreate/Delete/SendMessage 所需的最小属性集合。
class _FakeLeadAgent:
    def __init__(self, registry: Any = None) -> None:
        self.agent_id = "lead-agent-id"
        self.coordinator_mode = False
        self.registry = registry
        self._full_registry: Any = None
        self.team_name: str = ""


# 构造带 teammate_mode / enable_coordinator_mode 的 fake config。
def _make_config(
    teammate_mode: str = "in-process",
    enable_coordinator: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        teammate_mode=teammate_mode,
        enable_coordinator_mode=enable_coordinator,
    )


# ---------------------------------------------------------------------------
# TeamCreate → SendMessage → drain → TeamDelete 全链路
# ---------------------------------------------------------------------------


# 验证 TeamCreate 创建团队目录与配置文件。
# TeamCreateTool.execute 成功后 resolve_team_dir 应存在 config.json。
@pytest.mark.asyncio
async def test_team_create_creates_team_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    lead = _FakeLeadAgent()
    tool = TeamCreateTool(lead, mgr, _make_config())

    result = await tool.execute(TeamCreateParams(team_name="demo", description="test"))

    assert not result.is_error
    assert "demo" in result.content
    # 团队目录存在 config.json。
    team_dir = tmp_path / ".seacode" / "teams" / "demo"
    assert team_dir.exists()
    assert (team_dir / "config.json").exists()


# 验证 SendMessage 从 teammate 写入 lead 邮箱，drain_lead_mailbox 可消费。
# 注册 teammate 与 lead 名字后用 SendMessageTool 发消息，再 drain_lead_mailbox 验证 XML 格式。
@pytest.mark.asyncio
async def test_send_message_and_drain_lead_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    lead = _FakeLeadAgent()
    # 创建团队。
    create_tool = TeamCreateTool(lead, mgr, _make_config())
    await create_tool.execute(TeamCreateParams(team_name="demo"))
    # 注册 alice 与 lead 名字到 AgentNameRegistry。
    alice_id = "alice-id-123"
    registry = AgentNameRegistry.instance()
    registry.register("alice", alice_id)
    registry.register(LEAD_NAME, "lead-agent-id")
    member = TeammateInfo(
        name="alice",
        agent_id=alice_id,
        agent_type="teammate",
        model="test-model",
        worktree_path="/tmp/fake-wt-alice",
        backend_type=BackendType.IN_PROCESS,
        is_active=None,
    )
    mgr.register_member("demo", member)
    # alice 发消息给 lead；SendMessageTool 直接绑定 alice 身份。
    send_tool = SendMessageTool(mgr, "demo", alice_id, "alice")
    result = await send_tool.execute(
        SendMessageParams(
            to="lead",
            message="I read README.md",
            summary="readme done",
        )
    )
    assert not result.is_error

    # lead drain_lead_mailbox 应返回含 alice 消息的 <team-notification> XML。
    notes = mgr.drain_lead_mailbox()
    assert len(notes) >= 1
    xml = notes[0]
    assert '<team-notification team="demo">' in xml
    assert "I read README.md" in xml
    assert "alice" in xml


# 验证队友发给 Lead 的消息始终写入当前团队保存的稳定收件人。
# 即使进程级名称表没有 Lead 条目，主 Agent 仍能在后续回合读取通知。
@pytest.mark.asyncio
async def test_send_message_to_lead_uses_team_lead_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manager = TeamManager()
    lead = _FakeLeadAgent()
    create_tool = TeamCreateTool(lead, manager, _make_config())
    await create_tool.execute(TeamCreateParams(team_name="demo"))

    registry = MagicMock()
    monkeypatch.setattr(
        "seacode.tools.send_message.AgentNameRegistry.instance",
        lambda: registry,
    )
    tool = SendMessageTool(manager, "demo", "alice-id", "alice")
    result = await tool.execute(
        SendMessageParams(
            to=LEAD_NAME,
            message="work complete",
            summary="completion",
        )
    )

    assert not result.is_error
    registry.resolve.assert_not_called()
    notes = manager.drain_lead_mailbox()
    assert len(notes) == 1
    assert "work complete" in notes[0]


# 验证 TeamDelete 清理团队目录与内存缓存。
# TeamCreate → TeamDelete 后团队目录不存在，list_teams 返回空。
@pytest.mark.asyncio
async def test_team_delete_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    lead = _FakeLeadAgent()
    create_tool = TeamCreateTool(lead, mgr, _make_config())
    await create_tool.execute(TeamCreateParams(team_name="demo"))

    team_dir = tmp_path / ".seacode" / "teams" / "demo"
    assert team_dir.exists()

    delete_tool = TeamDeleteTool(lead, mgr)
    result = await delete_tool.execute(TeamDeleteParams(team_name="demo"))

    assert not result.is_error
    assert not team_dir.exists()
    assert "demo" not in mgr.list_teams()


# ---------------------------------------------------------------------------
# in-process teammate spawn 集成
# ---------------------------------------------------------------------------


# 验证 in-process teammate 的 progress 在 get_all_teammate_progress 中可收集。
# 创建团队 → 注册成员 → 附加 progress → get_all_teammate_progress 返回非空。
@pytest.mark.asyncio
async def test_inprocess_teammate_progress_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from seacode.teams.progress import TeammateProgress

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    lead = _FakeLeadAgent()
    create_tool = TeamCreateTool(lead, mgr, _make_config())
    await create_tool.execute(TeamCreateParams(team_name="demo"))

    # 注册 alice 并附加 progress。
    progress = TeammateProgress(name="alice", team_name="demo", status="running")
    member = TeammateInfo(
        name="alice",
        agent_id="alice-id",
        agent_type="teammate",
        model="test",
        worktree_path="/tmp/fake",
        backend_type=BackendType.IN_PROCESS,
        is_active=None,
        progress=progress,
    )
    mgr.register_member("demo", member)

    all_progress = mgr.get_all_teammate_progress()
    assert len(all_progress) == 1
    assert all_progress[0].name == "alice"
    assert all_progress[0].status == "running"


# ---------------------------------------------------------------------------
# 邮箱持久化与跨进程语义
# ---------------------------------------------------------------------------


# 验证 Mailbox 写入后重启 TeamManager 仍可读取。
# 写入消息 → 重建 TeamManager → get_mailbox → consume 读取。
@pytest.mark.asyncio
async def test_mailbox_persistence_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr1 = TeamManager()
    lead = _FakeLeadAgent()
    create_tool = TeamCreateTool(lead, mgr1, _make_config())
    await create_tool.execute(TeamCreateParams(team_name="demo"))

    mailbox1 = mgr1.get_mailbox("demo")
    mailbox1.write(
        "lead-agent-id",
        create_message("alice", "lead-agent-id", "hello lead", "greeting"),
    )

    # 重建 TeamManager（模拟进程重启）。
    mgr2 = TeamManager()
    mailbox2 = mgr2.get_mailbox("demo")
    msgs = mailbox2.consume("lead-agent-id")
    assert len(msgs) == 1
    assert msgs[0].content == "hello lead"
    assert msgs[0].from_agent == "alice"


# ---------------------------------------------------------------------------
# 团队持久化恢复
# ---------------------------------------------------------------------------


# 验证 TeamManager.get_team 从磁盘懒加载已持久化的团队。
# TeamCreate → 重建 TeamManager → get_team 返回非 None 且 lead_agent_id 一致。
@pytest.mark.asyncio
async def test_team_persistence_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr1 = TeamManager()
    lead = _FakeLeadAgent()
    create_tool = TeamCreateTool(lead, mgr1, _make_config())
    await create_tool.execute(TeamCreateParams(team_name="demo"))

    # 重建 TeamManager（模拟进程重启）。
    mgr2 = TeamManager()
    team = mgr2.get_team("demo")
    assert team is not None
    assert team.name == "demo"
    assert team.lead_agent_id == "lead-agent-id"
