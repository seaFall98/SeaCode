"""Worktree 与 WorktreeSession 数据类。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Worktree:
    """单个 worktree 的运行时元数据。"""

    name: str
    path: str
    branch: str
    based_on: str
    head_commit: str
    created: datetime


@dataclass
class WorktreeSession:
    """进入 worktree 后的会话状态，用于持久化与恢复。"""

    original_cwd: str
    worktree_path: str
    worktree_name: str
    original_branch: str
    original_head_commit: str
    session_id: str
    hook_based: bool = False
