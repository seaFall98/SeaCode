"""ExitWorktreeTool 单元测试：覆盖未 session、非法 action、变更检测与 keep/remove 全分支。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from seacode.tools.exit_worktree import ExitWorktreeParams, ExitWorktreeTool
from seacode.worktree.changes import Changes
from seacode.worktree.manager import WorktreeError, WorktreeManager
from seacode.worktree.models import WorktreeSession


def _make_session(name: str = "feat-x") -> WorktreeSession:
    return WorktreeSession(
        original_cwd="/repo",
        worktree_path=f"/wt/{name}",
        worktree_name=name,
        original_branch="main",
        original_head_commit="abc123",
        session_id="sess-1",
        hook_based=False,
    )


# 验证未在 session 中时返回 is_error=True。
# mock get_current_session 返回 None，
# 断言 result.is_error=True 且 content 含 "未在 worktree session 中"。
async def test_not_in_session_returns_error() -> None:
    manager = WorktreeManager("/repo")
    tool = ExitWorktreeTool(manager)
    params = ExitWorktreeParams(action="keep")

    result = await tool.execute(params)

    assert result.is_error is True
    assert "未在 worktree session 中" in result.content


# 验证 action 非法（绕过 Literal）时返回 is_error=True。
# 用 model_construct 绕过 Literal 校验传入非法 action，断言 is_error=True。
async def test_invalid_action_returns_error() -> None:
    manager = WorktreeManager("/repo")
    manager.current_session = _make_session()
    tool = ExitWorktreeTool(manager)
    # model_construct 不做校验，可以传入任意字符串。
    params = ExitWorktreeParams.model_construct(action="invalid", discard_changes=False)

    result = await tool.execute(params)

    assert result.is_error is True
    assert "Invalid action" in result.content


# 验证 action=remove 且 not discard 时检测到变更返回数量提示。
# mock count_worktree_changes 返回 Changes(1, 2)，断言 is_error=True 且 content 含数量。
async def test_remove_with_changes_returns_change_count() -> None:
    manager = WorktreeManager("/repo")
    manager.current_session = _make_session()
    tool = ExitWorktreeTool(manager)
    params = ExitWorktreeParams(action="remove", discard_changes=False)

    with patch(
        "seacode.tools.exit_worktree.count_worktree_changes",
        return_value=Changes(uncommitted=1, new_commits=2),
    ):
        result = await tool.execute(params)

    assert result.is_error is True
    assert "1 uncommitted changes" in result.content
    assert "2 new commits" in result.content
    assert "discard_changes: true" in result.content


# 验证 action=keep 返回保留信息且调用 manager.exit(action="keep")。
# mock manager.exit，断言 result.is_error=False 且 exit 收到 action="keep"。
async def test_keep_action_returns_success() -> None:
    manager = WorktreeManager("/repo")
    manager.current_session = _make_session()
    manager.exit = AsyncMock(return_value=None)  # type: ignore[method-assign]
    tool = ExitWorktreeTool(manager)
    params = ExitWorktreeParams(action="keep")

    result = await tool.execute(params)

    assert result.is_error is False
    assert "已退出 worktree (action=keep)" in result.content
    manager.exit.assert_awaited_once_with(
        "feat-x", action="keep", discard_changes=False
    )


# 验证 action=remove discard=True 返回删除信息并调用 manager.exit(action="remove")。
# mock manager.exit，断言 result.is_error=False 且 exit 收到
# action="remove" 与 discard_changes=True。
async def test_remove_with_discard_returns_success() -> None:
    manager = WorktreeManager("/repo")
    manager.current_session = _make_session()
    manager.exit = AsyncMock(return_value=None)  # type: ignore[method-assign]
    tool = ExitWorktreeTool(manager)
    params = ExitWorktreeParams(action="remove", discard_changes=True)

    result = await tool.execute(params)

    assert result.is_error is False
    assert "已退出 worktree (action=remove)" in result.content
    manager.exit.assert_awaited_once_with(
        "feat-x", action="remove", discard_changes=True
    )


# 验证 action=remove discard=True 但无变更时不查 count_worktree_changes（性能优化）。
# 此处验证 discard=True 跳过变更检测：mock count_worktree_changes 应不被调用。
async def test_remove_with_discard_skips_change_check() -> None:
    manager = WorktreeManager("/repo")
    manager.current_session = _make_session()
    manager.exit = AsyncMock(return_value=None)  # type: ignore[method-assign]
    tool = ExitWorktreeTool(manager)
    params = ExitWorktreeParams(action="remove", discard_changes=True)

    with patch(
        "seacode.tools.exit_worktree.count_worktree_changes"
    ) as m_count:
        result = await tool.execute(params)

    assert result.is_error is False
    m_count.assert_not_called()


# 验证 manager.exit 抛 WorktreeError 时返回 is_error=True。
# mock exit 抛 WorktreeError，断言 result.is_error=True 且 content 含错误信息。
async def test_exit_raises_worktree_error_returns_error() -> None:
    manager = WorktreeManager("/repo")
    manager.current_session = _make_session()
    manager.exit = AsyncMock(side_effect=WorktreeError("exit failed"))  # type: ignore[method-assign]
    tool = ExitWorktreeTool(manager)
    params = ExitWorktreeParams(action="keep")

    result = await tool.execute(params)

    assert result.is_error is True
    assert "exit failed" in result.content
