"""worktree 后台清理：识别 ephemeral 名称、按 cutoff_hours 清理陈旧 worktree。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from seacode.worktree.changes import has_unpushed_commits, has_worktree_changes
from seacode.worktree.manager import WorktreeManager
from seacode.worktree.slug import flatten_slug

log = logging.getLogger(__name__)

# 临时 worktree 名称模式；匹配这些模式的 worktree 才会被清理。
EPHEMERAL_PATTERNS = [
    re.compile(p)
    for p in [
        r"^agent-a[0-9a-f]{7}$",
        r"^wf_[0-9a-f]{8}-[0-9a-f]{3}-\d+$",
        r"^wf-\d+$",
        r"^bridge-[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*$",
        r"^job-[a-zA-Z0-9._-]{1,55}-[0-9a-f]{8}$",
    ]
]


def _is_ephemeral(name: str) -> bool:
    """name 是否匹配任一 ephemeral 模式。"""
    return any(p.match(name) for p in EPHEMERAL_PATTERNS)


async def cleanup_stale_worktrees(manager: WorktreeManager, cutoff_hours: int) -> int:
    """遍历 worktree_dir，按五层过滤清理陈旧 ephemeral worktree；返回清理数量。"""
    cutoff = datetime.now() - timedelta(hours=cutoff_hours)
    removed = 0
    if not manager.worktree_dir.exists():
        return 0
    for entry in manager.worktree_dir.iterdir():
        name = entry.name
        if not entry.is_dir():
            continue
        if not _is_ephemeral(name):
            continue
        if manager.current_session and manager.current_session.worktree_name == name:
            continue
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime > cutoff:
                continue
        except OSError:
            continue
        head_sha = manager.read_worktree_head_sha(str(entry))
        if head_sha is None:
            continue
        if has_worktree_changes(str(entry), head_sha):
            continue
        if has_unpushed_commits(str(entry)):
            continue
        # 通过全部过滤：执行删除。
        flat_name = flatten_slug(name)
        if name in manager.active:
            await manager._remove_worktree(name, manager.active[name])
            removed += 1
        else:
            try:
                await manager._run_git(["worktree", "remove", "--force", str(entry)])
                await asyncio.sleep(0.1)
                await manager._run_git(["branch", "-D", f"worktree-{flat_name}"])
                removed += 1
            except Exception as e:
                log.warning("failed to cleanup %s: %s", name, e)
    return removed


async def start_stale_cleanup_task(
    manager: WorktreeManager, interval: int, cutoff_hours: int
) -> None:
    """周期性后台清理循环；异常不退出，CancelledError 时退出。"""
    while True:
        try:
            await asyncio.sleep(interval)
            count = await cleanup_stale_worktrees(manager, cutoff_hours)
            if count > 0:
                log.info("cleaned up %d stale worktrees", count)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("stale cleanup task error: %s", e)
