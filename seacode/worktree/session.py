"""worktree 会话持久化：保存与加载 WorktreeSession JSON。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from seacode.worktree.models import WorktreeSession

log = logging.getLogger(__name__)

# 会话文件名，位于 .seacode/ 目录下。
SESSION_FILENAME = "worktree_session.json"


def save_worktree_session(seacode_dir: str | Path, session: WorktreeSession | None) -> None:
    """session 为 None 时删除文件；非 None 时写入 JSON。"""
    path = Path(seacode_dir) / SESSION_FILENAME
    if session is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "original_cwd": session.original_cwd,
        "worktree_path": session.worktree_path,
        "worktree_name": session.worktree_name,
        "original_branch": session.original_branch,
        "original_head_commit": session.original_head_commit,
        "session_id": session.session_id,
        "hook_based": session.hook_based,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_worktree_session(seacode_dir: str | Path) -> WorktreeSession | None:
    """容错读取；文件不存在/JSON 无效/缺字段返回 None。"""
    path = Path(seacode_dir) / SESSION_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("failed to load worktree session: %s", e)
        return None
    if not isinstance(data, dict) or not data or "worktree_path" not in data:
        return None
    try:
        return WorktreeSession(
            original_cwd=data["original_cwd"],
            worktree_path=data["worktree_path"],
            worktree_name=data["worktree_name"],
            original_branch=data["original_branch"],
            original_head_commit=data["original_head_commit"],
            session_id=data.get("session_id", ""),
            hook_based=data.get("hook_based", False),
        )
    except KeyError as e:
        log.warning("missing field in worktree session: %s", e)
        return None
