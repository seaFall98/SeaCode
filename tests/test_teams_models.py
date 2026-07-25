"""teams/models.py 单测：BackendType / TeammateInfo / AgentTeam / sanitize / resolve / unique。"""

from __future__ import annotations

from pathlib import Path

import pytest

from seacode.teams.models import (
    AgentTeam,
    BackendType,
    TeammateInfo,
    _sanitize_name,
    resolve_team_dir,
    unique_team_name,
)


# 验证 BackendType 三值枚举。
# 枚举三个成员 TMUX / ITERM2 / IN_PROCESS，值分别为 "tmux" / "iterm2" / "in-process"。
def test_backend_type_values() -> None:
    assert BackendType.TMUX.value == "tmux"
    assert BackendType.ITERM2.value == "iterm2"
    assert BackendType.IN_PROCESS.value == "in-process"
    assert len(BackendType) == 3


# ---------------------------------------------------------------------------
# TeammateInfo
# ---------------------------------------------------------------------------


# 验证 TeammateInfo.to_dict 排除 progress 字段。
# 构造含 progress=None 的 TeammateInfo，断言 to_dict 结果不含 "progress" 键。
def test_teammate_info_to_dict_excludes_progress(tmp_path: Path) -> None:
    info = TeammateInfo(
        name="alice",
        agent_id="agent-1",
        agent_type="general-purpose",
        model="claude-sonnet-4-5",
        worktree_path=str(tmp_path / "wt"),
        backend_type=BackendType.IN_PROCESS,
        is_active=None,
        progress=None,
    )
    d = info.to_dict()
    assert "progress" not in d
    assert d["name"] == "alice"
    assert d["backend_type"] == "in-process"


# 验证 TeammateInfo.from_dict 与 to_dict 往返一致。
# 构造 TeammateInfo 序列化后反序列化，断言所有持久化字段一致。
def test_teammate_info_round_trip(tmp_path: Path) -> None:
    info = TeammateInfo(
        name="bob",
        agent_id="agent-2",
        agent_type="Verification",
        model="claude-haiku-4-5",
        worktree_path=str(tmp_path / "wt-bob"),
        backend_type=BackendType.TMUX,
        is_active=True,
    )
    restored = TeammateInfo.from_dict(info.to_dict())
    assert restored.name == info.name
    assert restored.agent_id == info.agent_id
    assert restored.agent_type == info.agent_type
    assert restored.model == info.model
    assert restored.worktree_path == info.worktree_path
    assert restored.backend_type == BackendType.TMUX
    assert restored.is_active is True


# ---------------------------------------------------------------------------
# AgentTeam
# ---------------------------------------------------------------------------


# 验证 AgentTeam.add_member / remove_member / set_member_active / all_idle / active_members。
# 构造空团队，添加两个成员，测试活跃态与列表查询方法的全分支。
def test_agent_team_member_operations() -> None:
    team = AgentTeam(name="demo", lead_agent_id="lead-1")
    alice = TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    bob = TeammateInfo(
        name="bob", agent_id="b1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS,
    )
    team.add_member(alice)
    team.add_member(bob)
    assert len(team.members) == 2
    assert team.get_member("alice") is alice
    assert team.get_member("nobody") is None

    # 初始 is_active=None：active_members 含全部，all_idle=False。
    assert len(team.active_members()) == 2
    assert not team.all_idle()

    # alice idle，bob 未知。
    team.set_member_active("alice", False)
    assert team.all_idle() is False
    assert len(team.active_members()) == 1
    assert team.active_members()[0].name == "bob"

    # 全部 idle。
    team.set_member_active("bob", False)
    assert team.all_idle() is True
    assert team.active_members() == []

    # 未知成员 set_member_active 不抛错。
    team.set_member_active("nobody", True)

    # 移除成员。
    team.remove_member("alice")
    assert team.get_member("alice") is None
    assert len(team.members) == 1


# 验证空团队的 all_idle 返回 True。
# 空团队没有成员，all_idle 应返回 True（vacuously true）。
def test_agent_team_all_idle_empty() -> None:
    team = AgentTeam(name="empty", lead_agent_id="lead-1")
    assert team.all_idle() is True
    assert team.active_members() == []


# 验证 AgentTeam.to_dict / from_dict 往返一致。
# 构造含成员的团队序列化后反序列化，断言字段一致。
def test_agent_team_round_trip(tmp_path: Path) -> None:
    team = AgentTeam(
        name="demo", lead_agent_id="lead-1",
        description="demo team",
        config_path=str(tmp_path / "config.json"),
    )
    team.add_member(TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS, is_active=False,
    ))
    d = team.to_dict()
    restored = AgentTeam.from_dict(d, config_path=str(tmp_path / "config.json"))
    assert restored.name == "demo"
    assert restored.lead_agent_id == "lead-1"
    assert restored.description == "demo team"
    assert len(restored.members) == 1
    assert restored.members[0].name == "alice"
    assert restored.members[0].is_active is False


# 验证 AgentTeam.save / load 往返。
# 把团队写入 config.json，再 load 回来，断言字段一致。
def test_agent_team_save_load(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    team = AgentTeam(
        name="demo", lead_agent_id="lead-1",
        config_path=str(cfg), description="persisted",
    )
    team.add_member(TeammateInfo(
        name="alice", agent_id="a1", agent_type="t", model="m",
        worktree_path="/wt", backend_type=BackendType.IN_PROCESS, is_active=None,
    ))
    team.save()
    assert cfg.exists()
    loaded = AgentTeam.load(cfg)
    assert loaded is not None
    assert loaded.name == "demo"
    assert loaded.lead_agent_id == "lead-1"
    assert loaded.description == "persisted"
    assert len(loaded.members) == 1
    assert loaded.members[0].name == "alice"


# 验证 AgentTeam.load 文件不存在时返回 None。
# 传入不存在的路径，断言返回 None 而不抛异常。
def test_agent_team_load_missing(tmp_path: Path) -> None:
    assert AgentTeam.load(tmp_path / "nope.json") is None


# 验证 AgentTeam.load 文件损坏时返回 None。
# 写入非法 JSON，断言 load 返回 None 并记 warning（不抛异常）。
def test_agent_team_load_corrupt(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json", encoding="utf-8")
    assert AgentTeam.load(cfg) is None


# ---------------------------------------------------------------------------
# _sanitize_name
# ---------------------------------------------------------------------------


# 验证 _sanitize_name 处理空格、中文、标点与空串。
# 四种输入分别断言：空格变 -、中文合并为 -、标点变 -、空串回退 "team"。
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("my team", "my-team"),
        ("团队", "-"),  # 两个中文字符都变 -，再合并为一个 -
        ("a.b", "a-b"),
        ("", "team"),
        ("Demo-Team", "demo-team"),
    ],
)
def test_sanitize_name(raw: str, expected: str) -> None:
    assert _sanitize_name(raw) == expected


# ---------------------------------------------------------------------------
# resolve_team_dir / unique_team_name
# ---------------------------------------------------------------------------


# 验证 resolve_team_dir 返回 ~/.seacode/teams/<slug>。
# 用 monkeypatch 替换 Path.home()，断言路径拼装正确。
def test_resolve_team_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = resolve_team_dir("demo")
    assert result == tmp_path / ".seacode" / "teams" / "demo"


# 验证 unique_team_name 同名追加 -2 / -3。
# mock Path.exists 让 base 与 -2 都存在，断言返回 -3。
def test_unique_team_name_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_exists(self: Path) -> bool:
        # 用 name 字段做匹配，避免平台路径分隔符差异。
        # base、demo-2 都"存在"，demo-3 不存在。
        return self.name in ("demo", "demo-2")

    monkeypatch.setattr(Path, "exists", fake_exists)
    assert unique_team_name("demo") == "demo-3"


# 验证 unique_team_name 不冲突时原样返回。
# mock Path.exists 全返回 False，断言返回 base slug。
def test_unique_team_name_no_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert unique_team_name("alpha") == "alpha"
