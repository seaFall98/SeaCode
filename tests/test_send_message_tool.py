"""tools/send_message.py 单测：消息类型校验、广播、单发、pane 唤醒。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from seacode.teams.models import AgentTeam, BackendType, TeammateInfo
from seacode.teams.registry import AgentNameRegistry
from seacode.tools.send_message import SendMessageParams, SendMessageTool


# 验证 text 消息无 summary 返回 is_error=True。
@pytest.mark.asyncio
async def test_send_message_text_requires_summary() -> None:
    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_mgr = MagicMock()
    tool = SendMessageTool(fake_agent, fake_mgr)
    params = SendMessageParams(to="alice", message="hello", summary="")
    result = await tool.execute(params)
    assert result.is_error
    assert "summary" in result.content


# 验证非法 message_type 返回 is_error=True。
@pytest.mark.asyncio
async def test_send_message_invalid_type() -> None:
    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_mgr = MagicMock()
    tool = SendMessageTool(fake_agent, fake_mgr)
    params = SendMessageParams(
        to="alice", message="hello", summary="s",
        message_type="invalid",
    )
    result = await tool.execute(params)
    assert result.is_error
    assert "非法 message_type" in result.content


# 验证 to="*" 广播：排除发送者、非 lead 发送时带 lead。
# fake team 含 2 个成员，断言 broadcast 调用 recipients 不含发送者但含 lead。
@pytest.mark.asyncio
async def test_send_message_broadcast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    # 手动构造内存团队。
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    team.add_member(TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    ))
    team.add_member(TeammateInfo(
        name="bob", agent_id="b1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    ))
    mgr._teams["demo"] = team
    mailbox = mgr.get_mailbox("demo")

    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_agent.team_name = "demo"

    tool = SendMessageTool(fake_agent, mgr)
    params = SendMessageParams(
        to="*", message="hello team", summary="greeting",
    )
    result = await tool.execute(params)

    assert not result.is_error
    # alice 与 bob 各收到一条消息；lead 自己排除。
    alice_msgs = mailbox.read("a1")
    bob_msgs = mailbox.read("b1")
    assert len(alice_msgs) == 1
    assert len(bob_msgs) == 1
    assert "hello team" in alice_msgs[0].content


# 验证 to="具体名称" 通过 AgentNameRegistry.resolve 解析。
# register alice → a1，断言 a1 邮箱收到消息。
@pytest.mark.asyncio
async def test_send_message_single_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    AgentNameRegistry.instance().reset()
    AgentNameRegistry.instance().register("alice", "a1")

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    mgr._teams["demo"] = team
    mailbox = mgr.get_mailbox("demo")

    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_agent.team_name = "demo"

    tool = SendMessageTool(fake_agent, mgr)
    params = SendMessageParams(
        to="alice", message="hello alice", summary="greeting",
    )
    result = await tool.execute(params)

    assert not result.is_error
    alice_msgs = mailbox.read("a1")
    assert len(alice_msgs) == 1
    assert "hello alice" in alice_msgs[0].content
    AgentNameRegistry.instance().reset()


# 验证未知名称返回 is_error=True。
@pytest.mark.asyncio
async def test_send_message_unknown_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    AgentNameRegistry.instance().reset()

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    mgr._teams["demo"] = team

    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_agent.team_name = "demo"

    tool = SendMessageTool(fake_agent, mgr)
    params = SendMessageParams(
        to="nobody", message="hello", summary="s",
    )
    result = await tool.execute(params)
    assert result.is_error
    assert "未知名称" in result.content
    AgentNameRegistry.instance().reset()


# 验证 _wake_pane 无 pane_id 时跳过 send_keys_to_pane。
@pytest.mark.asyncio
async def test_send_message_no_pane_id_skips_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    AgentNameRegistry.instance().reset()
    AgentNameRegistry.instance().register("alice", "a1")

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    mgr._teams["demo"] = team

    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_agent.team_name = "demo"

    tool = SendMessageTool(fake_agent, mgr)
    params = SendMessageParams(
        to="alice", message="hello", summary="s",
    )
    with patch(
        "seacode.teams.spawn_tmux.send_keys_to_pane"
    ) as mock_send:
        await tool.execute(params)
    # 无 pane_id，不调用 send_keys_to_pane。
    mock_send.assert_not_called()
    AgentNameRegistry.instance().reset()


# 验证 _wake_pane 有 pane_id 时调用 send_keys_to_pane。
@pytest.mark.asyncio
async def test_send_message_wakes_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    AgentNameRegistry.instance().reset()
    AgentNameRegistry.instance().register("alice", "a1")

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    mgr._teams["demo"] = team
    mgr.register_pane_id("demo", "alice", "%5")

    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_agent.team_name = "demo"

    tool = SendMessageTool(fake_agent, mgr)
    params = SendMessageParams(
        to="alice", message="hello", summary="s",
    )
    with patch(
        "seacode.teams.spawn_tmux.send_keys_to_pane"
    ) as mock_send:
        await tool.execute(params)
    mock_send.assert_called_once_with("%5", "")
    AgentNameRegistry.instance().reset()
