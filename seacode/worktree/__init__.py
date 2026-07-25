# Git Worktree 隔离工作区：生命周期管理、创建后初始化、会话持久化、
# 变更保护、后台清理、SubAgent 集成通知
"""worktree 子包：统一导出公开类与函数。"""

from __future__ import annotations

from seacode.worktree.changes import (
    Changes,
    CleanupResult,
    count_worktree_changes,
    has_unpushed_commits,
    has_worktree_changes,
)
from seacode.worktree.cleanup import cleanup_stale_worktrees, start_stale_cleanup_task
from seacode.worktree.integration import build_worktree_notice, generate_worktree_name
from seacode.worktree.manager import WorktreeError, WorktreeManager, read_worktree_head_sha
from seacode.worktree.models import Worktree, WorktreeSession
from seacode.worktree.session import load_worktree_session, save_worktree_session
from seacode.worktree.setup import perform_post_creation_setup
from seacode.worktree.slug import flatten_slug, validate_slug

__all__ = [
    "Changes",
    "CleanupResult",
    "Worktree",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeSession",
    "build_worktree_notice",
    "cleanup_stale_worktrees",
    "count_worktree_changes",
    "flatten_slug",
    "generate_worktree_name",
    "has_unpushed_commits",
    "has_worktree_changes",
    "load_worktree_session",
    "perform_post_creation_setup",
    "read_worktree_head_sha",
    "save_worktree_session",
    "start_stale_cleanup_task",
    "validate_slug",
]
