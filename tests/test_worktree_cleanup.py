"""worktree 后台清理五层过滤与 ephemeral 模式的单元测试。"""

from __future__ import annotations

import asyncio
import stat
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seacode.worktree.cleanup import (
    _is_ephemeral,
    cleanup_stale_worktrees,
    start_stale_cleanup_task,
)
from seacode.worktree.manager import WorktreeManager
from seacode.worktree.models import Worktree, WorktreeSession


def _make_worktree(name: str = "agent-a1a2b3c4") -> Worktree:
    return Worktree(
        name=name,
        path=f"/wt/{name}",
        branch=f"worktree-{name}",
        based_on="HEAD",
        head_commit="abc123",
        created=datetime.now(),
    )


# 构造 stat() mock 返回值；st_mode 设为目录类型避免 is_dir() 报错。
def _mock_stat_return(mtime: float) -> MagicMock:
    m = MagicMock()
    m.st_mode = stat.S_IFDIR
    m.st_mtime = mtime
    return m


# ---------------------------------------------------------------------------
# _is_ephemeral
# ---------------------------------------------------------------------------


# 验证 _is_ephemeral 匹配 agent-a + 7 hex 模式。
# 传入 "agent-a1a2b3c4" 断言返回 True。
@pytest.mark.parametrize(
    "name",
    [
        "agent-a1a2b3c4",
        "wf_12345678-123-1",
        "wf-1",
        "bridge-foo-bar",
        "job-test.name-12345678",
    ],
)
def test_is_ephemeral_matches_patterns(name: str) -> None:
    assert _is_ephemeral(name) is True


# 验证 _is_ephemeral 不匹配用户命名。
# 传入 "feat-x" 断言返回 False。
def test_is_ephemeral_rejects_user_named() -> None:
    assert _is_ephemeral("feat-x") is False


# ---------------------------------------------------------------------------
# cleanup_stale_worktrees
# ---------------------------------------------------------------------------


# 验证 cleanup_stale_worktrees 在 worktree_dir 不存在时返回 0。
# 不创建 worktree_dir，断言返回 0。
async def test_cleanup_returns_zero_when_dir_missing(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    assert await cleanup_stale_worktrees(manager, 24) == 0


# 验证 cleanup_stale_worktrees 跳过非 ephemeral 目录。
# 创建非 ephemeral 目录，断言返回 0 且不删除。
async def test_cleanup_skips_non_ephemeral(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.worktree_dir.mkdir(parents=True)
    (manager.worktree_dir / "feat-x").mkdir()

    assert await cleanup_stale_worktrees(manager, 24) == 0
    assert (manager.worktree_dir / "feat-x").exists()


# 验证 cleanup_stale_worktrees 跳过当前 session。
# 创建 ephemeral 目录与 current_session，断言返回 0。
async def test_cleanup_skips_current_session(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.worktree_dir.mkdir(parents=True)
    (manager.worktree_dir / "agent-a1a2b3c4").mkdir()
    manager.current_session = WorktreeSession(
        original_cwd="/repo",
        worktree_path=str(manager.worktree_dir / "agent-a1a2b3c4"),
        worktree_name="agent-a1a2b3c4",
        original_branch="main",
        original_head_commit="abc",
        session_id="sess",
    )

    assert await cleanup_stale_worktrees(manager, 24) == 0


# 验证 cleanup_stale_worktrees 跳过年龄不足的目录。
# mock st_mtime 返回近期时间，断言返回 0。
async def test_cleanup_skips_recent_worktree(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.worktree_dir.mkdir(parents=True)
    wt_path = manager.worktree_dir / "agent-a1a2b3c4"
    wt_path.mkdir()

    recent_mtime = datetime.now().timestamp()
    with patch("pathlib.Path.stat") as m_stat:
        m_stat.return_value = _mock_stat_return(recent_mtime)
        assert await cleanup_stale_worktrees(manager, 24) == 0


# 验证 cleanup_stale_worktrees 跳过 HEAD SHA 不可读的目录。
# mock read_worktree_head_sha 返回 None，断言返回 0。
async def test_cleanup_skips_unreadable_head(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.worktree_dir.mkdir(parents=True)
    wt_path = manager.worktree_dir / "agent-a1a2b3c4"
    wt_path.mkdir()

    old_mtime = (datetime.now() - timedelta(hours=48)).timestamp()
    with (
        patch("pathlib.Path.stat") as m_stat,
        patch.object(manager, "read_worktree_head_sha", return_value=None),
    ):
        m_stat.return_value = _mock_stat_return(old_mtime)
        assert await cleanup_stale_worktrees(manager, 24) == 0


# 验证 cleanup_stale_worktrees 跳过有变更的目录。
# mock has_worktree_changes 返回 True，断言返回 0。
async def test_cleanup_skips_with_changes(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.worktree_dir.mkdir(parents=True)
    wt_path = manager.worktree_dir / "agent-a1a2b3c4"
    wt_path.mkdir()

    old_mtime = (datetime.now() - timedelta(hours=48)).timestamp()
    with (
        patch("pathlib.Path.stat") as m_stat,
        patch.object(manager, "read_worktree_head_sha", return_value="abc"),
        patch("seacode.worktree.cleanup.has_worktree_changes", return_value=True),
    ):
        m_stat.return_value = _mock_stat_return(old_mtime)
        assert await cleanup_stale_worktrees(manager, 24) == 0


# 验证 cleanup_stale_worktrees 跳过有未推送 commit 的目录。
# mock has_unpushed_commits 返回 True，断言返回 0。
async def test_cleanup_skips_with_unpushed(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.worktree_dir.mkdir(parents=True)
    wt_path = manager.worktree_dir / "agent-a1a2b3c4"
    wt_path.mkdir()

    old_mtime = (datetime.now() - timedelta(hours=48)).timestamp()
    with (
        patch("pathlib.Path.stat") as m_stat,
        patch.object(manager, "read_worktree_head_sha", return_value="abc"),
        patch("seacode.worktree.cleanup.has_worktree_changes", return_value=False),
        patch("seacode.worktree.cleanup.has_unpushed_commits", return_value=True),
    ):
        m_stat.return_value = _mock_stat_return(old_mtime)
        assert await cleanup_stale_worktrees(manager, 24) == 0


# 验证 cleanup_stale_worktrees 通过全部过滤后删除 active 中的 worktree。
# 预置 active worktree，mock 全部过滤通过，断言 _remove_worktree 被调用且返回 1。
async def test_cleanup_removes_active_worktree(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.worktree_dir.mkdir(parents=True)
    wt_path = manager.worktree_dir / "agent-a1a2b3c4"
    wt_path.mkdir()
    manager.active["agent-a1a2b3c4"] = _make_worktree("agent-a1a2b3c4")

    old_mtime = (datetime.now() - timedelta(hours=48)).timestamp()
    with (
        patch("pathlib.Path.stat") as m_stat,
        patch.object(manager, "read_worktree_head_sha", return_value="abc"),
        patch("seacode.worktree.cleanup.has_worktree_changes", return_value=False),
        patch("seacode.worktree.cleanup.has_unpushed_commits", return_value=False),
        patch.object(manager, "_remove_worktree", new_callable=AsyncMock) as m_remove,
    ):
        m_stat.return_value = _mock_stat_return(old_mtime)
        result = await cleanup_stale_worktrees(manager, 24)

    assert result == 1
    m_remove.assert_called_once()


# 验证 cleanup_stale_worktrees 通过全部过滤后删除非 active 的 worktree。
# 不预置 active，mock _run_git 返回成功，断言返回 1。
async def test_cleanup_removes_non_active_worktree(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    manager.worktree_dir.mkdir(parents=True)
    wt_path = manager.worktree_dir / "agent-a1a2b3c4"
    wt_path.mkdir()

    old_mtime = (datetime.now() - timedelta(hours=48)).timestamp()
    with (
        patch("pathlib.Path.stat") as m_stat,
        patch.object(manager, "read_worktree_head_sha", return_value="abc"),
        patch("seacode.worktree.cleanup.has_worktree_changes", return_value=False),
        patch("seacode.worktree.cleanup.has_unpushed_commits", return_value=False),
        patch.object(manager, "_run_git", new_callable=AsyncMock) as m_git,
    ):
        m_stat.return_value = _mock_stat_return(old_mtime)
        result = await cleanup_stale_worktrees(manager, 24)

    assert result == 1
    # 至少调用 worktree remove + branch -D 两次
    assert m_git.call_count >= 2


# ---------------------------------------------------------------------------
# start_stale_cleanup_task
# ---------------------------------------------------------------------------


# 验证 start_stale_cleanup_task 循环执行清理。
# mock asyncio.sleep 抛 CancelledError 在第二次调用时退出，断言 cleanup 被调用一次。
async def test_start_stale_cleanup_task_loops_until_cancelled() -> None:
    manager = WorktreeManager("/repo")

    call_count = 0

    async def fake_sleep(interval: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError()

    with (
        patch("seacode.worktree.cleanup.asyncio.sleep", side_effect=fake_sleep),
        patch(
            "seacode.worktree.cleanup.cleanup_stale_worktrees",
            new_callable=AsyncMock,
            return_value=0,
        ) as m_cleanup,
    ):
        await start_stale_cleanup_task(manager, 1, 24)

    m_cleanup.assert_called_once()


# 验证 start_stale_cleanup_task 异常时不退出循环。
# mock cleanup_stale_worktrees 抛异常，断言循环继续直到 CancelledError。
async def test_start_stale_cleanup_task_continues_on_exception() -> None:
    manager = WorktreeManager("/repo")

    call_count = 0

    async def fake_sleep(interval: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise asyncio.CancelledError()

    async def fake_cleanup(mgr: WorktreeManager, hours: int) -> int:
        if call_count == 1:
            raise RuntimeError("boom")
        return 0

    with (
        patch("seacode.worktree.cleanup.asyncio.sleep", side_effect=fake_sleep),
        patch(
            "seacode.worktree.cleanup.cleanup_stale_worktrees",
            side_effect=fake_cleanup,
        ),
    ):
        await start_stale_cleanup_task(manager, 1, 24)
