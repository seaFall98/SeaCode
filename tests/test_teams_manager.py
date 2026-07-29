"""teams/manager.py 单测：TeamManager 全方法全分支与 6 步清理。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from seacode.teams.manager import TeamError, TeamManager
from seacode.teams.models import BackendType, TeammateInfo
from seacode.teams.progress import TeammateProgress


# 验证 detect_backend 首次调用后缓存；第二次不调用底层 detect_backend 函数。
# mock backend_detect.detect_backend，断言只调用一次。
def test_detect_backend_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = [0]

    def fake_detect(teammate_mode: str, is_interactive: bool) -> BackendType:
        call_count[0] += 1
        return BackendType.IN_PROCESS

    monkeypatch.setattr(
        "seacode.teams.manager.detect_backend", fake_detect
    )
    mgr = TeamManager()
    first = mgr.detect_backend("in-process", True)
    second = mgr.detect_backend("tmux", True)
    assert first == BackendType.IN_PROCESS
    assert second == BackendType.IN_PROCESS
    assert call_count[0] == 1


# 验证 create_team 创建目录与 config.json / tasks.json / mailbox/ 三件套。
# 用 monkeypatch 重定向 Path.home() 到 tmp_path，断言文件存在。
@pytest.mark.asyncio
async def test_create_team_creates_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    team = await mgr.create_team("demo", "lead-1", "test team")

    team_dir = tmp_path / ".seacode" / "teams" / "demo"
    assert team_dir.exists()
    assert (team_dir / "config.json").exists()
    assert (team_dir / "tasks.json").exists()
    assert (team_dir / "mailbox").is_dir()
    assert team.name == "demo"
    assert team.lead_agent_id == "lead-1"
    assert team.description == "test team"
    assert team.config_path == str(team_dir / "config.json")


# 验证 create_team 同名追加 -2。
# mock unique_team_name 返回 "demo-2"，断言团队名与目录使用 demo-2。
@pytest.mark.asyncio
async def test_create_team_dedup_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "seacode.teams.manager.unique_team_name", lambda name, root: "demo-2"
    )
    mgr = TeamManager()
    team = await mgr.create_team("demo", "lead-1")
    assert team.name == "demo-2"
    team_dir = tmp_path / ".seacode" / "teams" / "demo-2"
    assert team_dir.exists()


# 验证 get_team 内存优先：先 create_team 后 get_team 返回同一实例。
@pytest.mark.asyncio
async def test_get_team_in_memory_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    created = await mgr.create_team("demo", "lead-1")
    fetched = mgr.get_team("demo")
    assert fetched is created


# 验证 get_team 磁盘懒加载：清空 _teams 后 get_team 从 config.json 重建。
@pytest.mark.asyncio
async def test_get_team_lazy_load_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1", "persisted")
    # 清空内存缓存，强制从磁盘加载。
    mgr._teams.clear()
    team = mgr.get_team("demo")
    assert team is not None
    assert team.lead_agent_id == "lead-1"
    assert team.description == "persisted"


# 验证 get_team 不存在时返回 None。
def test_get_team_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    assert mgr.get_team("nope") is None


# 验证 get_task_store / get_mailbox 内存优先与懒加载。
@pytest.mark.asyncio
async def test_get_task_store_and_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    store1 = mgr.get_task_store("demo")
    store2 = mgr.get_task_store("demo")
    assert store1 is store2
    mb1 = mgr.get_mailbox("demo")
    mb2 = mgr.get_mailbox("demo")
    assert mb1 is mb2
    # 懒加载：清空缓存后仍可获取。
    mgr._task_stores.clear()
    mgr._mailboxes.clear()
    assert mgr.get_task_store("demo") is not None
    assert mgr.get_mailbox("demo") is not None


# 验证 register_member 持久化到 config.json 且记录 agent_id→team 映射。
@pytest.mark.asyncio
async def test_register_member_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    member = TeammateInfo(
        name="alice", agent_id="a1", agent_type="general-purpose",
        model="m", worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    mgr.register_member("demo", member)

    # config.json 含成员。
    import json

    cfg = json.loads((tmp_path / ".seacode" / "teams" / "demo" / "config.json").read_text("utf-8"))
    assert len(cfg["members"]) == 1
    assert cfg["members"][0]["name"] == "alice"
    # agent_id→team 映射。
    assert mgr.get_team_for_teammate("a1") == "demo"


# 验证 register_member 团队不存在时抛 TeamError。
def test_register_member_team_not_found() -> None:
    mgr = TeamManager()
    member = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    with pytest.raises(TeamError, match="not found"):
        mgr.register_member("nope", member)


# 验证 set_member_idle 标记 idle 并写 idle 通知到团队保存的 Lead 邮箱。
@pytest.mark.asyncio
async def test_set_member_idle_writes_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    member = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    mgr.register_member("demo", member)

    mgr.set_member_idle("demo", "alice", "completed")
    # lead 邮箱应收到 idle 通知。
    mailbox = mgr.get_mailbox("demo")
    msgs = mailbox.read("lead-1")
    assert len(msgs) >= 1
    assert any("[idle]" in m.content and "alice" in m.content for m in msgs)
    # 成员 is_active 应为 False。
    team = mgr.get_team("demo")
    assert team is not None
    stored_member = team.get_member("alice")
    assert stored_member is not None
    assert stored_member.is_active is False


# 验证 set_member_active 在 idle 成员收到续写前持久化为 active。
# 先将 alice 标记 idle，再调用 set_member_active，断言内存成员和 config.json 均为 True。
@pytest.mark.asyncio
async def test_set_member_active_persists_continuation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manager = TeamManager()
    await manager.create_team("demo", "lead-1")
    manager.register_member(
        "demo",
        TeammateInfo(
            name="alice", agent_id="a1", agent_type="t", model="m",
            worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
            is_active=False,
        ),
    )

    manager.set_member_active("demo", "alice")

    team = manager.get_team("demo")
    assert team is not None
    member = team.get_member("alice")
    assert member is not None
    assert member.is_active is True
    reloaded = TeamManager().get_team("demo")
    assert reloaded is not None
    restored_member = reloaded.get_member("alice")
    assert restored_member is not None
    assert restored_member.is_active is True


# 验证 register_inprocess_handle / register_pane_id / get_pane_id 存取。
@pytest.mark.asyncio
async def test_register_handles_and_pane_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")

    fake_handle = MagicMock()
    fake_handle.progress = TeammateProgress(name="alice", team_name="demo")
    mgr.register_inprocess_handle("a1", fake_handle)
    assert mgr._inprocess_handles["a1"] is fake_handle

    mgr.register_pane_id("b1", "%5")
    assert mgr.get_pane_id("b1") == "%5"
    assert mgr.get_pane_id("a1") is None


# 验证 register_inprocess_handle 在成员已注册时附加 progress。
@pytest.mark.asyncio
async def test_register_inprocess_handle_attaches_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    member = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    mgr.register_member("demo", member)

    fake_handle = MagicMock()
    fake_progress = TeammateProgress(name="alice", team_name="demo")
    fake_handle.progress = fake_progress
    mgr.register_inprocess_handle("a1", fake_handle)

    team = mgr.get_team("demo")
    assert team is not None
    stored_member = team.get_member("alice")
    assert stored_member is not None
    assert stored_member.progress is fake_progress


# 验证 delete_team 活跃成员存在时抛 TeamError。
@pytest.mark.asyncio
async def test_delete_team_active_members_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    member = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
        is_active=True,
    )
    mgr.register_member("demo", member)

    with pytest.raises(TeamError, match="active members"):
        await mgr.delete_team("demo")


# 验证 delete_team 6 步全执行：cancel handle / kill pane / cleanup
# worktree / cleanup mailbox / remove dir。
@pytest.mark.asyncio
async def test_delete_team_full_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    member = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/tmp/wt-demo-alice", backend_type=BackendType.IN_PROCESS,
        is_active=False,
    )
    mgr.register_member("demo", member)

    # 注册 handle 与 pane_id。
    fake_handle = MagicMock()
    mgr.register_inprocess_handle("a1", fake_handle)
    mgr.register_pane_id("a1", "%5")

    # mock _kill_pane / _cleanup_worktree 避免真实调用。
    with patch.object(mgr, "_kill_pane") as mock_kill, \
         patch.object(mgr, "_cleanup_worktree") as mock_cleanup:
        await mgr.delete_team("demo")

    # handle 被 cancel。
    fake_handle.cancel.assert_called_once()
    # pane 被 kill。
    mock_kill.assert_called_once_with("%5")
    # worktree 被 cleanup。
    mock_cleanup.assert_called_once_with("/tmp/wt-demo-alice")
    # 团队目录被删除。
    team_dir = tmp_path / ".seacode" / "teams" / "demo"
    assert not team_dir.exists()
    # 内存缓存被清理。
    assert "demo" not in mgr._teams
    assert "demo" not in mgr._mailboxes
    assert mgr.get_team_for_teammate("a1") is None


# 验证 delete_team 团队不存在时抛 TeamError。
@pytest.mark.asyncio
async def test_delete_team_not_found() -> None:
    mgr = TeamManager()
    with pytest.raises(TeamError, match="not found"):
        await mgr.delete_team("nope")


# 验证 list_teams / get_team_for_teammate 存取。
@pytest.mark.asyncio
async def test_list_teams_and_get_team_for_teammate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    assert mgr.list_teams() == []
    await mgr.create_team("demo", "lead-1")
    await mgr.create_team("alpha", "lead-2")
    assert set(mgr.list_teams()) == {"demo", "alpha"}

    member = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    mgr.register_member("demo", member)
    assert mgr.get_team_for_teammate("a1") == "demo"
    assert mgr.get_team_for_teammate("b1") is None


# 验证 drain_lead_mailbox 拼成 <team-notification> XML。
# 写消息到 lead 邮箱后 drain，断言返回含 XML 与消息内容。
@pytest.mark.asyncio
async def test_drain_lead_mailbox_xml_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    mailbox = mgr.get_mailbox("demo")
    from seacode.teams.mailbox import create_message

    mailbox.write("lead-1", create_message("alice", "lead-1", "task done", "done"))

    notes = mgr.drain_lead_mailbox()
    assert len(notes) == 1
    assert '<team-notification team="demo">' in notes[0]
    assert "task done" in notes[0]
    assert "From alice:" in notes[0]
    # 二次 drain 应为空（已 consume）。
    assert mgr.drain_lead_mailbox() == []


# 验证 get_all_teammate_progress 收集所有团队所有成员的 progress。
@pytest.mark.asyncio
async def test_get_all_teammate_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    await mgr.create_team("alpha", "lead-2")

    m1 = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    m2 = TeammateInfo(
        name="bob", agent_id="b1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    mgr.register_member("demo", m1)
    mgr.register_member("alpha", m2)

    # 附加 progress。
    p1 = TeammateProgress(name="alice", team_name="demo")
    p2 = TeammateProgress(name="bob", team_name="alpha")
    m1.progress = p1
    m2.progress = p2

    all_progress = mgr.get_all_teammate_progress()
    assert len(all_progress) == 2
    names = {p.name for p in all_progress}
    assert names == {"alice", "bob"}


# 验证 on_teammate_completed 设置 idle。
@pytest.mark.asyncio
async def test_on_teammate_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mgr = TeamManager()
    await mgr.create_team("demo", "lead-1")
    member = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    mgr.register_member("demo", member)

    mgr.on_teammate_completed("demo", "alice")
    team = mgr.get_team("demo")
    assert team is not None
    stored_member = team.get_member("alice")
    assert stored_member is not None
    assert stored_member.is_active is False


# 验证 _cleanup_worktree 在 git 失败时回退 shutil.rmtree。
def test_cleanup_worktree_falls_back_to_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = TeamManager()
    # 创建一个临时 worktree 目录。
    wt = tmp_path / "wt-demo"
    wt.mkdir()
    (wt / "file.txt").write_text("content")

    # mock subprocess.run 抛异常，强制走 rmtree。
    def fake_run(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr("seacode.teams.manager.subprocess.run", fake_run)
    mgr._cleanup_worktree(str(wt))
    # rmtree 应删除目录。
    assert not wt.exists()


# 验证 _cleanup_worktree 会删除 SeaCode 自动生成的 worktree 分支。
# 模拟完整 Git 成功路径，断言 remove 后以 git common dir 删除 worktree-* 分支。
def test_cleanup_worktree_deletes_generated_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subprocess import CompletedProcess

    manager = TeamManager()
    worktree = tmp_path / "team-demo-alice"
    worktree.mkdir()
    common_dir = tmp_path / ".git"
    branch = "worktree-team-demo+alice"
    calls: list[list[str]] = []
    results = [
        CompletedProcess([], 0, stdout=f"{branch}\n", stderr=""),
        CompletedProcess([], 0, stdout=f"{common_dir}\n", stderr=""),
        CompletedProcess([], 0, stdout="", stderr=""),
        CompletedProcess([], 0, stdout="", stderr=""),
    ]

    def fake_run(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        del kwargs
        calls.append(args)
        return results.pop(0)

    monkeypatch.setattr("seacode.teams.manager.subprocess.run", fake_run)

    manager._cleanup_worktree(str(worktree))

    assert not worktree.exists()
    assert calls[2] == ["git", "worktree", "remove", str(worktree), "--force"]
    assert calls[3] == [
        "git",
        f"--git-dir={common_dir}",
        "branch",
        "-D",
        branch,
    ]


# 验证 TeamManager 的显式团队根自动创建项目 .seacode/teams，且不会读取遗留用户级团队。
# 分别在 legacy 与 project 根创建团队，断言 project 只加载自身团队并保持目录隔离。
@pytest.mark.asyncio
async def test_team_manager_uses_explicit_project_root_and_ignores_legacy(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "user-home" / ".seacode" / "teams"
    legacy_manager = TeamManager(teams_root=legacy_root)
    await legacy_manager.create_team("legacy", "legacy-lead")

    project_root = tmp_path / "project" / ".seacode" / "teams"
    manager = TeamManager(teams_root=project_root)
    assert manager.get_team("legacy") is None
    team = await manager.create_team("demo", "lead-1")

    assert team.config_path == str(project_root / "demo" / "config.json")
    assert (project_root / "demo" / "tasks.json").exists()
    assert (legacy_root / "legacy" / "config.json").exists()
    assert not (legacy_root / "demo").exists()


# 验证两个项目可创建同名团队，重启后的 Manager 仍按自身根加载和按 Lead 查询。
# 在两个不同根创建 demo，再新建 Manager，断言各自的 description 和 Lead 互不串扰。
@pytest.mark.asyncio
async def test_team_manager_isolates_same_name_teams_between_projects(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "project-a" / ".seacode" / "teams"
    root_b = tmp_path / "project-b" / ".seacode" / "teams"
    manager_a = TeamManager(teams_root=root_a)
    manager_b = TeamManager(teams_root=root_b)
    await manager_a.create_team("demo", "lead-a", description="A")
    await manager_b.create_team("demo", "lead-b", description="B")

    reloaded_a = TeamManager(teams_root=root_a)
    reloaded_b = TeamManager(teams_root=root_b)
    team_a = reloaded_a.get_team("demo")
    team_b = reloaded_b.get_team("demo")

    assert team_a is not None
    assert team_b is not None
    assert team_a.description == "A"
    assert team_b.description == "B"
    assert [team.name for team in reloaded_a.get_teams_for_lead("lead-a")] == [
        "demo"
    ]
    assert reloaded_a.get_teams_for_lead("lead-b") == []
