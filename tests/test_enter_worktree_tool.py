"""EnterWorktreeTool 单元测试：覆盖已 session、自动命名、slug 校验、成功与失败路径。"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

from seacode.tools.enter_worktree import EnterWorktreeParams, EnterWorktreeTool
from seacode.worktree.manager import WorktreeError, WorktreeManager
from seacode.worktree.models import Worktree, WorktreeSession


def _make_worktree(name: str = "feat-x") -> Worktree:
    return Worktree(
        name=name,
        path=f"/wt/{name}",
        branch=f"worktree-{name}",
        based_on="HEAD",
        head_commit="abc123",
        created=datetime.datetime.now(),
    )


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


# 验证已在 session 中时返回 is_error=True 且不调用 manager.create。
# mock get_current_session 返回非 None，断言结果 is_error=True 且内容含 "已退出"。
async def test_already_in_session_returns_error() -> None:
    manager = WorktreeManager("/repo")
    manager.current_session = _make_session()
    tool = EnterWorktreeTool(manager)
    params = EnterWorktreeParams(name="feat-x")

    result = await tool.execute(params)

    assert result.is_error is True
    assert "已在 worktree session 中" in result.content


# 验证 name 留空时自动生成 wt-<hex> 前缀名并传给 manager.create。
# mock create/enter，断言 create 收到的 name 以 "wt-" 开头。
async def test_default_name_auto_generated_with_wt_prefix() -> None:
    manager = WorktreeManager("/repo")
    captured_name: list[str] = []

    async def fake_create(name: str, base_branch: str = "HEAD") -> Worktree:
        captured_name.append(name)
        return _make_worktree(name=name)

    manager.create = AsyncMock(side_effect=fake_create)  # type: ignore[method-assign]
    manager.enter = AsyncMock(return_value=_make_session())  # type: ignore[method-assign]
    tool = EnterWorktreeTool(manager)
    params = EnterWorktreeParams(name="")

    result = await tool.execute(params)

    assert result.is_error is False
    assert len(captured_name) == 1
    assert captured_name[0].startswith("wt-")


# 验证 slug 非法（含非法字符）时返回 is_error=True。
# 传入含空格的 name，断言 is_error=True 且内容含 "Invalid worktree name"。
async def test_invalid_slug_returns_error() -> None:
    manager = WorktreeManager("/repo")
    tool = EnterWorktreeTool(manager)
    params = EnterWorktreeParams(name="foo bar")

    result = await tool.execute(params)

    assert result.is_error is True
    assert "Invalid worktree name" in result.content


# 验证 manager.create 成功并 enter 后返回路径与分支信息。
# mock create/enter 返回 Worktree 与 WorktreeSession，断言 result.content 含 name/path/branch。
async def test_create_and_enter_success_returns_worktree_info() -> None:
    manager = WorktreeManager("/repo")
    wt = _make_worktree(name="feat-x")
    manager.create = AsyncMock(return_value=wt)  # type: ignore[method-assign]
    manager.enter = AsyncMock(return_value=_make_session())  # type: ignore[method-assign]
    changed_work_dirs: list[str] = []
    tool = EnterWorktreeTool(manager, on_work_dir_changed=changed_work_dirs.append)
    params = EnterWorktreeParams(name="feat-x")

    result = await tool.execute(params)

    assert result.is_error is False
    assert "feat-x" in result.content
    assert "/wt/feat-x" in result.content
    assert "worktree-feat-x" in result.content
    assert changed_work_dirs == ["/wt/feat-x"]


# 验证 manager.create 抛 WorktreeError 时返回 is_error=True。
# mock create 抛 WorktreeError，断言 result.is_error=True 且 content 含错误信息。
async def test_create_raises_worktree_error_returns_error() -> None:
    manager = WorktreeManager("/repo")
    manager.create = AsyncMock(side_effect=WorktreeError("git worktree add failed"))  # type: ignore[method-assign]
    tool = EnterWorktreeTool(manager)
    params = EnterWorktreeParams(name="feat-x")

    result = await tool.execute(params)

    assert result.is_error is True
    assert "git worktree add failed" in result.content


# 验证 manager.enter 抛 WorktreeError 时返回 is_error=True。
# mock create 成功，enter 抛 WorktreeError，断言 result.is_error=True。
async def test_enter_raises_worktree_error_returns_error() -> None:
    manager = WorktreeManager("/repo")
    manager.create = AsyncMock(return_value=_make_worktree())  # type: ignore[method-assign]
    manager.enter = AsyncMock(side_effect=WorktreeError("enter failed"))  # type: ignore[method-assign]
    changed_work_dirs: list[str] = []
    tool = EnterWorktreeTool(manager, on_work_dir_changed=changed_work_dirs.append)
    params = EnterWorktreeParams(name="feat-x")

    result = await tool.execute(params)

    assert result.is_error is True
    assert "enter failed" in result.content
    assert changed_work_dirs == []
