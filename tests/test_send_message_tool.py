"""tools/send_message.py 单测：消息类型校验、广播、单发、pane 唤醒。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from seacode.teams.models import AgentTeam, BackendType, TeammateInfo
from seacode.tools.send_message import SendMessageParams, SendMessageTool


# 验证 text 消息无 summary 返回 is_error=True。
@pytest.mark.asyncio
async def test_send_message_text_requires_summary() -> None:
    fake_mgr = MagicMock()
    tool = SendMessageTool(fake_mgr, "demo", "lead-1", "lead")
    params = SendMessageParams(to="alice", message="hello", summary="")
    result = await tool.execute(params)
    assert result.is_error
    assert "summary" in result.content


# 验证非法 message_type 返回 is_error=True。
@pytest.mark.asyncio
async def test_send_message_invalid_type() -> None:
    fake_mgr = MagicMock()
    tool = SendMessageTool(fake_mgr, "demo", "lead-1", "lead")
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

    tool = SendMessageTool(mgr, "demo", "lead-1", "lead")
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


# 验证 to="具体名称" 只通过当前团队成员列表解析。
# 在团队加入 alice 后发送，断言 a1 邮箱收到消息。
@pytest.mark.asyncio
async def test_send_message_single_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    team.add_member(TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    ))
    mgr._teams["demo"] = team
    mailbox = mgr.get_mailbox("demo")

    tool = SendMessageTool(mgr, "demo", "lead-1", "lead")
    params = SendMessageParams(
        to="alice", message="hello alice", summary="greeting",
    )
    result = await tool.execute(params)

    assert not result.is_error
    alice_msgs = mailbox.read("a1")
    assert len(alice_msgs) == 1
    assert "hello alice" in alice_msgs[0].content


# 验证未知名称返回 is_error=True。
@pytest.mark.asyncio
async def test_send_message_unknown_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    mgr._teams["demo"] = team

    tool = SendMessageTool(mgr, "demo", "lead-1", "lead")
    params = SendMessageParams(
        to="nobody", message="hello", summary="s",
    )
    result = await tool.execute(params)
    assert result.is_error
    assert "团队 demo 中不存在收件人" in result.content


# 验证 _wake_pane 无 pane_id 时跳过 send_keys_to_pane。
@pytest.mark.asyncio
async def test_send_message_no_pane_id_skips_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    team.add_member(TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    ))
    mgr._teams["demo"] = team

    tool = SendMessageTool(mgr, "demo", "lead-1", "lead")
    params = SendMessageParams(
        to="alice", message="hello", summary="s",
    )
    with patch(
        "seacode.teams.spawn_tmux.send_keys_to_pane"
    ) as mock_send:
        result = await tool.execute(params)
    # 无 pane_id，不调用 send_keys_to_pane。
    assert not result.is_error
    mock_send.assert_not_called()


# 验证 _wake_pane 有 pane_id 时调用 send_keys_to_pane。
@pytest.mark.asyncio
async def test_send_message_wakes_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    team.add_member(TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.TMUX,
    ))
    mgr._teams["demo"] = team
    # pane_id 按 agent_id 索引；send_message 通过当前团队成员解析得到 a1 后唤醒。
    mgr.register_pane_id("a1", "%5")

    tool = SendMessageTool(mgr, "demo", "lead-1", "lead")
    params = SendMessageParams(
        to="alice", message="hello", summary="s",
    )
    with patch(
        "seacode.teams.spawn_tmux.send_keys_to_pane"
    ) as mock_send:
        result = await tool.execute(params)
    assert not result.is_error
    mock_send.assert_called_once_with("%5", "")


# 验证广播路径会唤醒每个有 pane_id 的收件人，包括 lead。
# 构造 alice + bob + lead-1 三个 agent_id，仅 alice 与 bob 有 pane_id；
# 断言 send_keys_to_pane 被调用两次（lead 主进程无 pane_id 时跳过）。
@pytest.mark.asyncio
async def test_send_message_broadcast_wakes_all_recipients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    # bob 作为团队成员发送广播；recipients 应为 alice + lead-1（排除 bob 自己）。
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    team.add_member(TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.TMUX,
    ))
    team.add_member(TeammateInfo(
        name="bob", agent_id="b1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.TMUX,
    ))
    mgr._teams["demo"] = team
    # alice 与 lead 都有 pane_id（lead 在跨进程 pane 后端时也会被注册）。
    mgr.register_pane_id("a1", "%5")
    mgr.register_pane_id("lead-1", "%9")

    tool = SendMessageTool(mgr, "demo", "b1", "bob")
    params = SendMessageParams(
        to="*", message="standup", summary="daily",
    )
    with patch(
        "seacode.teams.spawn_tmux.send_keys_to_pane"
    ) as mock_send:
        result = await tool.execute(params)

    assert not result.is_error
    # alice 与 lead 各被唤醒一次；bob 自己被排除。
    assert mock_send.call_count == 2
    called_panes = {call.args[0] for call in mock_send.call_args_list}
    assert called_panes == {"%5", "%9"}


# 验证应用装配期为空的 Lead 工具会在执行时按当前 Lead 身份找到唯一团队。
# 构造空绑定 SendMessageTool 与一个成员，断言成员邮箱收到 Lead 的消息。
@pytest.mark.asyncio
async def test_send_message_dynamic_lead_resolves_single_owned_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from seacode.teams.manager import TeamManager

    manager = TeamManager()
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    team.add_member(TeammateInfo(
        name="alice", agent_id="alice-1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    ))
    manager._teams["demo"] = team
    tool = SendMessageTool(
        manager, parent_agent=SimpleNamespace(agent_id="lead-1")
    )

    result = await tool.execute(
        SendMessageParams(to="alice", message="继续处理", summary="next task")
    )

    assert not result.is_error
    messages = manager.get_mailbox("demo").read("alice-1")
    assert len(messages) == 1
    assert messages[0].from_agent == "lead"


# 验证同一 Lead 的多团队消息必须显式选队，且显式选择只写目标团队邮箱。
# 先省略 team_name 断言错误，再选择 alpha 向其中的 bob 成功发送。
@pytest.mark.asyncio
async def test_send_message_dynamic_lead_requires_explicit_team_for_multiple_teams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from seacode.teams.manager import TeamManager

    manager = TeamManager()
    demo = AgentTeam(name="demo", lead_agent_id="lead-1")
    demo.add_member(TeammateInfo(
        name="alice", agent_id="alice-1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    ))
    alpha = AgentTeam(name="alpha", lead_agent_id="lead-1")
    alpha.add_member(TeammateInfo(
        name="bob", agent_id="bob-1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    ))
    manager._teams.update({"demo": demo, "alpha": alpha})
    tool = SendMessageTool(
        manager, parent_agent=SimpleNamespace(agent_id="lead-1")
    )

    ambiguous = await tool.execute(
        SendMessageParams(to="alice", message="继续处理", summary="next task")
    )
    sent = await tool.execute(
        SendMessageParams(
            to="bob",
            message="处理 alpha",
            summary="alpha task",
            team_name="alpha",
        )
    )

    assert ambiguous.is_error
    assert "多个团队" in ambiguous.content
    assert not sent.is_error
    assert manager.get_mailbox("alpha").read("bob-1")
    assert manager.get_mailbox("demo").read("alice-1") == []


# 验证当前 Lead 不能显式选取其他 Lead 的团队，也不能跨已选团队寻址成员。
# 分别向 foreign 团队和 alpha 中不存在的 alice 发送，断言两个调用都失败且无投递。
@pytest.mark.asyncio
async def test_send_message_dynamic_lead_rejects_unowned_and_cross_team_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from seacode.teams.manager import TeamManager

    manager = TeamManager()
    alpha = AgentTeam(name="alpha", lead_agent_id="lead-1")
    alpha.add_member(TeammateInfo(
        name="bob", agent_id="bob-1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    ))
    foreign = AgentTeam(name="foreign", lead_agent_id="lead-2")
    manager._teams.update({"alpha": alpha, "foreign": foreign})
    tool = SendMessageTool(
        manager, parent_agent=SimpleNamespace(agent_id="lead-1")
    )

    unowned = await tool.execute(
        SendMessageParams(
            to="*", message="不能发送", summary="invalid", team_name="foreign"
        )
    )
    cross_team = await tool.execute(
        SendMessageParams(
            to="alice", message="不能发送", summary="invalid", team_name="alpha"
        )
    )

    assert unowned.is_error
    assert "不属于团队" in unowned.content
    assert cross_team.is_error
    assert "不存在收件人" in cross_team.content
    assert manager.get_mailbox("alpha").read("bob-1") == []
