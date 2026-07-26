"""WorktreeManager 生命周期与 read_worktree_head_sha 的单元测试。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from seacode.worktree.changes import Changes
from seacode.worktree.manager import WorktreeError, WorktreeManager, read_worktree_head_sha
from seacode.worktree.models import Worktree, WorktreeSession


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_worktree(name: str = "feat-x", head_commit: str = "abc123") -> Worktree:
    """构造一个完整 Worktree 实例。"""
    return Worktree(
        name=name,
        path=f"/wt/{name}",
        branch=f"worktree-{name}",
        based_on="HEAD",
        head_commit=head_commit,
        created=__import__("datetime").datetime.now(),
    )


def _make_session(name: str = "feat-x") -> WorktreeSession:
    """构造一个完整 WorktreeSession 实例。"""
    return WorktreeSession(
        original_cwd="/repo",
        worktree_path=f"/wt/{name}",
        worktree_name=name,
        original_branch="main",
        original_head_commit="abc123",
        session_id="sess-1",
        hook_based=False,
    )


# ---------------------------------------------------------------------------
# read_worktree_head_sha
# ---------------------------------------------------------------------------


# 验证 read_worktree_head_sha 在 .git 不存在时返回 None。
# 在空目录调用，断言返回 None。
def test_read_head_sha_no_git_file(tmp_path: Path) -> None:
    assert read_worktree_head_sha(tmp_path) is None


# 验证 read_worktree_head_sha 解析 gitdir 指针并读取 HEAD。
# 构造 .git 含 gitdir 指针与 HEAD 文件，断言返回 SHA。
def test_read_head_sha_parses_gitdir_pointer(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    gitdir = tmp_path / ".git" / "worktrees" / "wt"
    wt.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    (gitdir / "HEAD").write_text("abcdef123456", encoding="utf-8")

    assert read_worktree_head_sha(wt) == "abcdef123456"


# 验证 read_worktree_head_sha 通过 commondir 解析 loose ref。
# 构造 gitdir + commondir + loose ref，断言返回 SHA。
def test_read_head_sha_parses_commondir_loose_ref(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    gitdir = tmp_path / ".git" / "worktrees" / "wt"
    common = tmp_path / ".git"
    wt.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    (gitdir / "commondir").write_text(str(common), encoding="utf-8")
    (gitdir / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (common / "refs" / "heads").mkdir(parents=True)
    (common / "refs" / "heads" / "main").write_text("loose_sha_123", encoding="utf-8")

    assert read_worktree_head_sha(wt) == "loose_sha_123"


# 验证 read_worktree_head_sha 回退到 packed-refs。
# 构造 gitdir + commondir + HEAD ref + packed-refs，断言返回 packed-refs 中的 SHA。
def test_read_head_sha_falls_back_to_packed_refs(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    gitdir = tmp_path / ".git" / "worktrees" / "wt"
    common = tmp_path / ".git"
    wt.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    (gitdir / "commondir").write_text(str(common), encoding="utf-8")
    (gitdir / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    # 不创建 loose ref
    (common / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "packed_sha_456 refs/heads/main\n",
        encoding="utf-8",
    )

    assert read_worktree_head_sha(wt) == "packed_sha_456"


# 验证 read_worktree_head_sha 在 OSError 时返回 None。
# mock Path.read_text 抛 OSError，断言返回 None。
def test_read_head_sha_handles_oserror(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nonexistent", encoding="utf-8")

    # 路径不存在会触发 OSError，返回 None
    result = read_worktree_head_sha(wt)
    assert result is None


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


# 验证 create 全新创建走 git worktree add 路径。
# mock _run_git 与 perform_post_creation_setup，断言 active 含新 worktree。
async def test_create_new_worktree(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)

    with (
        patch("seacode.worktree.manager.read_worktree_head_sha", return_value=None),
        patch("seacode.worktree.manager.perform_post_creation_setup") as m_setup,
        patch.object(manager, "_run_git", new_callable=AsyncMock, return_value=_completed()),
    ):
        wt = await manager.create("feat-x")

    assert wt.name == "feat-x"
    assert wt.branch == "worktree-feat-x"
    assert "feat-x" in manager.active
    m_setup.assert_called_once()


# 验证 create 在已存在目录且 HEAD SHA 可读时走快速恢复路径。
# mock read_worktree_head_sha 返回 SHA，断言不调用 _run_git。
async def test_create_fast_restore_when_head_sha_available(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)

    with (
        patch("seacode.worktree.manager.read_worktree_head_sha", return_value="abc123"),
        patch.object(manager, "_run_git", new_callable=AsyncMock) as m_git,
        patch("seacode.worktree.manager.perform_post_creation_setup") as m_setup,
    ):
        wt = await manager.create("feat-x")

    assert wt.head_commit == "abc123"
    m_git.assert_not_called()
    m_setup.assert_not_called()


# 验证 create 非法 slug 抛 WorktreeError。
# 传入含空格的 name，断言抛错且消息含 "invalid"。
async def test_create_invalid_slug_raises(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    with pytest.raises(WorktreeError, match="invalid"):
        await manager.create("foo bar")


# 验证 create 重名抛 WorktreeError。
# 先创建一个 worktree，再创建同名，断言抛错。
async def test_create_duplicate_raises(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.active["feat-x"] = _make_worktree()
    with pytest.raises(WorktreeError, match="already exists"):
        await manager.create("feat-x")


# ---------------------------------------------------------------------------
# enter
# ---------------------------------------------------------------------------


# 验证 enter 记录 original_cwd/branch/head_commit 并持久化 session。
# mock _run_git 返回分支与 HEAD，断言 current_session 字段正确。
# original_cwd 应为调用 enter 时的工作目录（os.getcwd()），而非仓库根。
async def test_enter_records_session(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.active["feat-x"] = _make_worktree()

    def fake_run(args: list[str], cwd: object = None) -> subprocess.CompletedProcess:
        if "rev-parse" in args and "--abbrev-ref" in args:
            return _completed(stdout="main\n")
        if "rev-parse" in args and "HEAD" in args:
            return _completed(stdout="head_sha_123\n")
        return _completed()

    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        with (
            patch.object(
                manager, "_run_git", new_callable=AsyncMock, side_effect=fake_run
            ),
            patch("seacode.worktree.manager.save_worktree_session") as m_save,
            patch("seacode.worktree.manager.os.chdir") as m_chdir,
        ):
            session = await manager.enter("feat-x")
    finally:
        os.chdir(original_cwd)

    assert session.original_cwd == str(tmp_path)
    assert session.original_branch == "main"
    assert session.original_head_commit == "head_sha_123"
    assert session.worktree_name == "feat-x"
    assert manager.current_session is session
    m_save.assert_called_once()
    # enter 不应调用 os.chdir；切换由调用方（命令/工具）完成。
    m_chdir.assert_not_called()


# 验证 enter 未知 worktree 抛 WorktreeError。
# 不预置 active，直接 enter 未知 name，断言抛错。
async def test_enter_unknown_raises(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    with pytest.raises(WorktreeError, match="not found"):
        await manager.enter("unknown")


# ---------------------------------------------------------------------------
# exit
# ---------------------------------------------------------------------------


# 验证 exit action=keep 清空 session 不删除 worktree。
# 预置 active 与 current_session，调用 exit，断言 _remove_worktree 未被调用。
async def test_exit_keep_clears_session_without_removal(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    wt = _make_worktree()
    manager.active["feat-x"] = wt
    manager.current_session = _make_session()

    with (
        patch.object(manager, "_remove_worktree", new_callable=AsyncMock) as m_remove,
        patch("seacode.worktree.manager.save_worktree_session") as m_save,
    ):
        await manager.exit("feat-x", action="keep")

    assert manager.current_session is None
    m_remove.assert_not_called()
    m_save.assert_called_once_with(manager._seacode_dir, None)


# 验证 exit action=remove 有变更且未 discard 抛 WorktreeError。
# mock count_worktree_changes 返回 Changes(1, 0)，断言抛错。
async def test_exit_remove_with_changes_raises(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.active["feat-x"] = _make_worktree()

    with patch(
        "seacode.worktree.manager.count_worktree_changes",
        return_value=Changes(uncommitted=1, new_commits=0),
    ):
        with pytest.raises(WorktreeError, match="uncommitted changes"):
            await manager.exit("feat-x", action="remove", discard_changes=False)


# 验证 exit action=remove discard=True 调用 _remove_worktree。
# 预置 active，调用 exit(discard_changes=True)，断言 _remove_worktree 被调用。
async def test_exit_remove_discard_calls_remove(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.active["feat-x"] = _make_worktree()

    with (
        patch.object(manager, "_remove_worktree", new_callable=AsyncMock) as m_remove,
        patch("seacode.worktree.manager.save_worktree_session"),
    ):
        await manager.exit("feat-x", action="remove", discard_changes=True)

    m_remove.assert_called_once()


# ---------------------------------------------------------------------------
# auto_cleanup
# ---------------------------------------------------------------------------


# 验证 auto_cleanup 无变更时删除 worktree 并返回 kept=False。
# mock has_worktree_changes 返回 False，断言 _remove_worktree 被调用。
async def test_auto_cleanup_removes_when_clean(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.active["feat-x"] = _make_worktree()

    with (
        patch("seacode.worktree.manager.has_worktree_changes", return_value=False),
        patch.object(manager, "_remove_worktree", new_callable=AsyncMock) as m_remove,
    ):
        result = await manager.auto_cleanup("feat-x", "abc")

    assert result.kept is False
    m_remove.assert_called_once()


# 验证 auto_cleanup 有变更时保留 worktree 并返回 kept=True。
# mock has_worktree_changes 返回 True，断言 _remove_worktree 未被调用。
async def test_auto_cleanup_keeps_when_changes(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.active["feat-x"] = _make_worktree()

    with (
        patch("seacode.worktree.manager.has_worktree_changes", return_value=True),
        patch.object(manager, "_remove_worktree", new_callable=AsyncMock) as m_remove,
    ):
        result = await manager.auto_cleanup("feat-x", "abc")

    assert result.kept is True
    m_remove.assert_not_called()


# 验证 auto_cleanup 未知 name 返回 kept=False。
# 不预置 active，断言返回 kept=False 且不调用 _remove_worktree。
async def test_auto_cleanup_unknown_returns_not_kept(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    with (
        patch.object(manager, "_remove_worktree", new_callable=AsyncMock) as m_remove,
    ):
        result = await manager.auto_cleanup("unknown", "abc")

    assert result.kept is False
    m_remove.assert_not_called()


# ---------------------------------------------------------------------------
# restore_session
# ---------------------------------------------------------------------------


# 验证 restore_session 文件不存在时返回 None。
# 在空目录调用，断言返回 None。
async def test_restore_session_no_file_returns_none(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    assert await manager.restore_session() is None


# 验证 restore_session HEAD SHA 不可读时清空文件并返回 None。
# mock load_worktree_session 返回 session，read_worktree_head_sha 返回 None，断言 None。
async def test_restore_session_unreadable_head_clears_file(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    session = _make_session()

    with (
        patch("seacode.worktree.manager.load_worktree_session", return_value=session),
        patch("seacode.worktree.manager.read_worktree_head_sha", return_value=None),
        patch("seacode.worktree.manager.save_worktree_session") as m_save,
    ):
        result = await manager.restore_session()

    assert result is None
    m_save.assert_called_once_with(manager._seacode_dir, None)


# 验证 restore_session 可读时恢复 active 与 current_session。
# mock load 返回 session，read 返回 SHA，断言 active 含 worktree 且 current_session 设置。
async def test_restore_session_restores_active(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    session = _make_session()

    with (
        patch("seacode.worktree.manager.load_worktree_session", return_value=session),
        patch("seacode.worktree.manager.read_worktree_head_sha", return_value="sha123"),
    ):
        result = await manager.restore_session()

    assert result is session
    assert "feat-x" in manager.active
    assert manager.current_session is session
    assert manager.active["feat-x"].head_commit == "sha123"


# ---------------------------------------------------------------------------
# list_worktrees / get_current_session
# ---------------------------------------------------------------------------


# 验证 list_worktrees 返回 active 中的全部 worktree。
# 预置两个 worktree，断言返回长度为 2。
def test_list_worktrees_returns_active(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.active["a"] = _make_worktree("a")
    manager.active["b"] = _make_worktree("b")
    result = manager.list_worktrees()
    assert len(result) == 2


# 验证 get_current_session 返回 current_session。
# 设置 current_session，断言返回相同实例。
def test_get_current_session_returns_session(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    session = _make_session()
    manager.current_session = session
    assert manager.get_current_session() is session


# 验证 get_current_session 未设置时返回 None。
# 不设置 current_session，断言返回 None。
def test_get_current_session_returns_none_when_unset(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    assert manager.get_current_session() is None
