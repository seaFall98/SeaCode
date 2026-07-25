"""worktree 数据类字段与默认值的单元测试。"""

from __future__ import annotations

from datetime import datetime

from seacode.worktree.models import Worktree, WorktreeSession


# 验证 Worktree 六字段构造后保留传入值。
# 传入全部字段构造 Worktree，断言每个字段持有传入值。
def test_worktree_constructor_preserves_all_fields() -> None:
    now = datetime.now()
    wt = Worktree(
        name="feat-x",
        path="/tmp/feat-x",
        branch="worktree-feat-x",
        based_on="HEAD",
        head_commit="abc123",
        created=now,
    )
    assert wt.name == "feat-x"
    assert wt.path == "/tmp/feat-x"
    assert wt.branch == "worktree-feat-x"
    assert wt.based_on == "HEAD"
    assert wt.head_commit == "abc123"
    assert wt.created is now


# 验证 WorktreeSession 七字段构造后保留传入值。
# 传入全部字段构造 WorktreeSession，断言每个字段持有传入值。
def test_worktree_session_constructor_preserves_all_fields() -> None:
    session = WorktreeSession(
        original_cwd="/repo",
        worktree_path="/repo/.seacode/worktrees/feat-x",
        worktree_name="feat-x",
        original_branch="main",
        original_head_commit="abc123",
        session_id="sess-1",
        hook_based=True,
    )
    assert session.original_cwd == "/repo"
    assert session.worktree_path == "/repo/.seacode/worktrees/feat-x"
    assert session.worktree_name == "feat-x"
    assert session.original_branch == "main"
    assert session.original_head_commit == "abc123"
    assert session.session_id == "sess-1"
    assert session.hook_based is True


# 验证 WorktreeSession.hook_based 默认为 False。
# 构造 WorktreeSession 不传 hook_based，断言取默认值 False。
def test_worktree_session_hook_based_defaults_false() -> None:
    session = WorktreeSession(
        original_cwd="/repo",
        worktree_path="/repo/.seacode/worktrees/feat-x",
        worktree_name="feat-x",
        original_branch="main",
        original_head_commit="abc123",
        session_id="sess-1",
    )
    assert session.hook_based is False
