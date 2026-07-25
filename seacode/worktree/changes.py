"""worktree 变更检测：fail-closed 统计未提交变更与新 commit。"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Changes:
    """worktree 变更统计：未提交变更数与基于 head_commit 的新 commit 数。"""

    uncommitted: int = 0
    new_commits: int = 0


@dataclass
class CleanupResult:
    """auto_cleanup 与 cleanup_stale_worktrees 的返回值；kept=True 表示因变更保留。"""

    kept: bool


def _run_git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess:
    """同步执行 git 子进程；禁用交互提示，超时 60s。"""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def count_worktree_changes(wt_path: str | Path, head_commit: str) -> Changes:
    """统计 worktree 未提交变更数与基于 head_commit 的新 commit 数；fail-closed。"""
    uncommitted = 0
    new_commits = 0
    try:
        result = _run_git(["status", "--porcelain"], cwd=wt_path)
        uncommitted = len([line for line in result.stdout.splitlines() if line.strip()])
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("git status failed: %s", e)
        uncommitted = 1
    try:
        result = _run_git(["rev-list", "--count", f"{head_commit}..HEAD"], cwd=wt_path)
        new_commits = int(result.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        log.warning("git rev-list failed: %s", e)
        new_commits = 1
    return Changes(uncommitted=uncommitted, new_commits=new_commits)


def has_worktree_changes(wt_path: str | Path, head_commit: str) -> bool:
    """worktree 是否有未提交变更或新 commit。"""
    changes = count_worktree_changes(wt_path, head_commit)
    return changes.uncommitted > 0 or changes.new_commits > 0


def has_unpushed_commits(wt_path: str | Path) -> bool:
    """worktree 是否有未推送 commit；fail-closed：异常时返回 True。"""
    try:
        result = _run_git(
            ["rev-list", "--max-count=1", "HEAD", "--not", "--remotes"], cwd=wt_path
        )
        return not (result.returncode == 0 and not result.stdout.strip())
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("git rev-list unpushed failed: %s", e)
        return True
