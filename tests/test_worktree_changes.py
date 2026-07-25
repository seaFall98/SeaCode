"""worktree 变更检测的单元测试，覆盖 fail-closed 路径。"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from seacode.worktree.changes import (
    Changes,
    count_worktree_changes,
    has_unpushed_commits,
    has_worktree_changes,
)


# 构造一个 fake CompletedProcess；stdout/stderr/returncode 可配置。
def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# 验证 count_worktree_changes 无 uncommitted 变更时返回 0。
# mock git status stdout 为空，断言 uncommitted=0。
async def test_count_changes_no_uncommitted() -> None:
    def fake_run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
        if "status" in args:
            return _completed(stdout="")
        if "rev-list" in args:
            return _completed(stdout="0")
        return _completed()

    with patch("seacode.worktree.changes._run_git", side_effect=fake_run):
        changes = count_worktree_changes("/wt", "abc")
    assert changes.uncommitted == 0


# 验证 count_worktree_changes 有 uncommitted 变更时返回正确数量。
# mock git status stdout 含 3 行，断言 uncommitted=3。
async def test_count_changes_counts_uncommitted_lines() -> None:
    def fake_run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
        if "status" in args:
            return _completed(stdout=" M file1\n M file2\n?? file3\n")
        if "rev-list" in args:
            return _completed(stdout="0")
        return _completed()

    with patch("seacode.worktree.changes._run_git", side_effect=fake_run):
        changes = count_worktree_changes("/wt", "abc")
    assert changes.uncommitted == 3


# 验证 count_worktree_changes git status 异常时 uncommitted=1 (fail-closed)。
# mock _run_git 对 status 抛 SubprocessError，断言 uncommitted=1。
async def test_count_changes_status_failure_returns_one() -> None:
    def fake_run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
        if "status" in args:
            raise subprocess.SubprocessError("boom")
        if "rev-list" in args:
            return _completed(stdout="0")
        return _completed()

    with patch("seacode.worktree.changes._run_git", side_effect=fake_run):
        changes = count_worktree_changes("/wt", "abc")
    assert changes.uncommitted == 1


# 验证 count_worktree_changes git rev-list 输出数字时 new_commits 正确。
# mock rev-list stdout="5"，断言 new_commits=5。
async def test_count_changes_rev_list_returns_count() -> None:
    def fake_run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
        if "status" in args:
            return _completed(stdout="")
        if "rev-list" in args:
            return _completed(stdout="5")
        return _completed()

    with patch("seacode.worktree.changes._run_git", side_effect=fake_run):
        changes = count_worktree_changes("/wt", "abc")
    assert changes.new_commits == 5


# 验证 count_worktree_changes git rev-list 输出非数字时 new_commits=1 (fail-closed)。
# mock rev-list stdout="not a number"，断言 new_commits=1。
async def test_count_changes_rev_list_non_numeric_returns_one() -> None:
    def fake_run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
        if "status" in args:
            return _completed(stdout="")
        if "rev-list" in args:
            return _completed(stdout="not a number")
        return _completed()

    with patch("seacode.worktree.changes._run_git", side_effect=fake_run):
        changes = count_worktree_changes("/wt", "abc")
    assert changes.new_commits == 1


# 验证 count_worktree_changes git rev-list 异常时 new_commits=1 (fail-closed)。
# mock _run_git 对 rev-list 抛 SubprocessError，断言 new_commits=1。
async def test_count_changes_rev_list_failure_returns_one() -> None:
    def fake_run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
        if "status" in args:
            return _completed(stdout="")
        if "rev-list" in args:
            raise subprocess.SubprocessError("boom")
        return _completed()

    with patch("seacode.worktree.changes._run_git", side_effect=fake_run):
        changes = count_worktree_changes("/wt", "abc")
    assert changes.new_commits == 1


# 验证 has_worktree_changes 在 uncommitted>0 时返回 True。
# mock count 返回 Changes(1, 0)，断言 has_worktree_changes 返回 True。
async def test_has_changes_returns_true_when_uncommitted() -> None:
    with patch("seacode.worktree.changes.count_worktree_changes", return_value=Changes(1, 0)):
        assert has_worktree_changes("/wt", "abc") is True


# 验证 has_worktree_changes 在 new_commits>0 时返回 True。
# mock count 返回 Changes(0, 1)，断言 has_worktree_changes 返回 True。
async def test_has_changes_returns_true_when_new_commits() -> None:
    with patch("seacode.worktree.changes.count_worktree_changes", return_value=Changes(0, 1)):
        assert has_worktree_changes("/wt", "abc") is True


# 验证 has_worktree_changes 在无变更时返回 False。
# mock count 返回 Changes(0, 0)，断言 has_worktree_changes 返回 False。
async def test_has_changes_returns_false_when_clean() -> None:
    with patch("seacode.worktree.changes.count_worktree_changes", return_value=Changes(0, 0)):
        assert has_worktree_changes("/wt", "abc") is False


# 验证 has_unpushed_commits 在有未推送 commit 时返回 True。
# mock rev-list stdout 非空，断言返回 True。
async def test_has_unpushed_returns_true_when_output_non_empty() -> None:
    with patch("seacode.worktree.changes._run_git", return_value=_completed(stdout="abc123\n")):
        assert has_unpushed_commits("/wt") is True


# 验证 has_unpushed_commits 在无未推送 commit 时返回 False。
# mock rev-list stdout 为空且 returncode=0，断言返回 False。
async def test_has_unpushed_returns_false_when_clean() -> None:
    with patch("seacode.worktree.changes._run_git", return_value=_completed(stdout="")):
        assert has_unpushed_commits("/wt") is False


# 验证 has_unpushed_commits 在 returncode 非 0 时返回 True。
# mock rev-list returncode=1，断言返回 True。
async def test_has_unpushed_returns_true_on_nonzero_returncode() -> None:
    with patch("seacode.worktree.changes._run_git", return_value=_completed(returncode=1)):
        assert has_unpushed_commits("/wt") is True


# 验证 has_unpushed_commits 在异常时返回 True (fail-closed)。
# mock _run_git 抛 SubprocessError，断言返回 True。
async def test_has_unpushed_returns_true_on_exception() -> None:
    with patch("seacode.worktree.changes._run_git", side_effect=subprocess.SubprocessError("boom")):
        assert has_unpushed_commits("/wt") is True
