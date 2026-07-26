"""WorktreeManager：worktree 生命周期管理（创建/进入/退出/恢复/清理）。"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from seacode.worktree.changes import CleanupResult, count_worktree_changes, has_worktree_changes
from seacode.worktree.models import Worktree, WorktreeSession
from seacode.worktree.session import load_worktree_session, save_worktree_session
from seacode.worktree.setup import perform_post_creation_setup
from seacode.worktree.slug import flatten_slug, validate_slug

log = logging.getLogger(__name__)

# git 子进程统一环境变量；禁用交互提示避免卡死。
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


class WorktreeError(Exception):
    """worktree 生命周期错误。"""


def read_worktree_head_sha(wt_path: str | Path) -> str | None:
    """从 worktree 目录读取 HEAD SHA；失败返回 None。模块级别名转发到静态方法。"""
    return WorktreeManager.read_worktree_head_sha(wt_path)


class WorktreeManager:
    """管理多个 worktree 的生命周期与会话状态；asyncio.Lock 保护 active 字典与 current_session。"""

    def __init__(
        self,
        repo_root: str | Path,
        symlink_directories: list[str] | None = None,
        worktree_dir: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.symlink_directories = symlink_directories or []
        self.worktree_dir = (
            Path(worktree_dir) if worktree_dir else self.repo_root / ".seacode" / "worktrees"
        )
        self._seacode_dir = self.repo_root / ".seacode"
        self._lock = asyncio.Lock()
        self.active: dict[str, Worktree] = {}
        self.current_session: WorktreeSession | None = None

    async def _run_git(
        self, args: list[str], cwd: str | Path | None = None
    ) -> subprocess.CompletedProcess:
        """异步包装的 git 子进程调用。"""
        env = {**os.environ, **GIT_ENV}
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else str(self.repo_root),
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    @staticmethod
    def read_worktree_head_sha(wt_path: str | Path) -> str | None:
        """从 worktree 目录读取 HEAD SHA；失败返回 None。"""
        p = Path(wt_path)
        try:
            git_file = p / ".git"
            if not git_file.exists():
                return None
            content = git_file.read_text(encoding="utf-8").strip()
            # gitdir: /path/to/.git/worktrees/<name>
            if content.startswith("gitdir:"):
                gitdir_path = Path(content.split(":", 1)[1].strip())
                if not gitdir_path.is_absolute():
                    gitdir_path = (p / gitdir_path).resolve()
                # commondir 文件指向主仓库的 .git 目录
                commondir_file = gitdir_path / "commondir"
                if commondir_file.exists():
                    commondir_content = commondir_file.read_text(encoding="utf-8").strip()
                    commondir_path = Path(commondir_content)
                    if not commondir_path.is_absolute():
                        commondir_path = (gitdir_path / commondir_path).resolve()
                else:
                    commondir_path = gitdir_path
                head_file = gitdir_path / "HEAD"
                if not head_file.exists():
                    return None
                head_content = head_file.read_text(encoding="utf-8").strip()
                if head_content.startswith("ref:"):
                    # ref: refs/heads/<branch>
                    ref_path = head_content.split(":", 1)[1].strip()
                    loose_ref = commondir_path / ref_path
                    if loose_ref.exists():
                        return loose_ref.read_text(encoding="utf-8").strip()
                    # 回退到 packed-refs
                    packed_refs = commondir_path / "packed-refs"
                    if packed_refs.exists():
                        for line in packed_refs.read_text(encoding="utf-8").splitlines():
                            if line.startswith("#") or line.startswith("^"):
                                continue
                            parts = line.split(" ", 1)
                            if len(parts) == 2 and parts[1] == ref_path:
                                return parts[0]
                    return None
                return head_content
            # 非 worktree 目录，直接是 .git 仓库
            return None
        except OSError as e:
            log.warning("failed to read worktree head sha: %s", e)
            return None

    async def create(self, name: str, base_branch: str = "HEAD") -> Worktree:
        """创建或恢复一个 worktree；重名时抛 WorktreeError。"""
        async with self._lock:
            err = validate_slug(name)
            if err:
                raise WorktreeError(err)
            if name in self.active:
                raise WorktreeError(f"worktree {name} already exists")
            flat = flatten_slug(name)
            wt_path = self.worktree_dir / flat
            branch = f"worktree-{flat}"
            # 快速恢复：目录已存在且能读取 HEAD SHA 时直接复用。
            head_sha = read_worktree_head_sha(wt_path)
            if head_sha is not None:
                wt = Worktree(
                    name=name,
                    path=str(wt_path),
                    branch=branch,
                    based_on=base_branch,
                    head_commit=head_sha,
                    created=datetime.now(),
                )
                self.active[name] = wt
                return wt
            # 全新创建：显式创建父目录，避免 .seacode/worktrees 不存在时 git 失败。
            os.makedirs(self.worktree_dir, exist_ok=True)
            result = await self._run_git(
                ["worktree", "add", "-B", branch, str(wt_path), base_branch]
            )
            if result.returncode != 0:
                raise WorktreeError(f"git worktree add failed: {result.stderr}")
            perform_post_creation_setup(self.repo_root, wt_path, self.symlink_directories)
            head_sha = read_worktree_head_sha(wt_path) or ""
            wt = Worktree(
                name=name,
                path=str(wt_path),
                branch=branch,
                based_on=base_branch,
                head_commit=head_sha,
                created=datetime.now(),
            )
            self.active[name] = wt
            return wt

    async def enter(self, name: str) -> WorktreeSession:
        """进入 worktree 会话；记录原工作目录/分支/HEAD，持久化 session。"""
        async with self._lock:
            if name not in self.active:
                raise WorktreeError(f"worktree {name} not found")
            wt = self.active[name]
            # 记录当前工作目录，退出 worktree 后回到此处；不能强制回到仓库根。
            original_cwd = os.getcwd()
            # git 调用加容错：失败时回退到 HEAD/空串，不让 worktree 进入因此阻塞。
            try:
                result = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
                original_branch = (
                    result.stdout.strip() if result.returncode == 0 else "HEAD"
                )
            except (subprocess.SubprocessError, OSError):
                original_branch = "HEAD"
            try:
                result = await self._run_git(["rev-parse", "HEAD"])
                original_head_commit = (
                    result.stdout.strip() if result.returncode == 0 else ""
                )
            except (subprocess.SubprocessError, OSError):
                original_head_commit = ""
            session = WorktreeSession(
                original_cwd=original_cwd,
                worktree_path=wt.path,
                worktree_name=name,
                original_branch=original_branch,
                original_head_commit=original_head_commit,
                session_id=str(id(self)),
                hook_based=False,
            )
            self.current_session = session
            save_worktree_session(self._seacode_dir, session)
            return session

    async def exit(self, name: str, action: str = "keep", discard_changes: bool = False) -> None:
        """退出 worktree 会话；action=remove 时若有变更且未 discard 抛错。"""
        async with self._lock:
            if name not in self.active:
                raise WorktreeError(f"worktree {name} not found")
            wt = self.active[name]
            if action == "remove" and not discard_changes:
                changes = count_worktree_changes(wt.path, wt.head_commit)
                if changes.uncommitted > 0 or changes.new_commits > 0:
                    raise WorktreeError(
                        f"worktree has {changes.uncommitted} uncommitted changes and "
                        f"{changes.new_commits} new commits; "
                        f"set discard_changes=True to force remove"
                    )
            self.current_session = None
            save_worktree_session(self._seacode_dir, None)
            if action == "remove":
                await self._remove_worktree(name, wt)

    async def _remove_worktree(self, name: str, wt: Worktree) -> None:
        """删除 worktree 物理目录与本地分支；失败时只记日志不抛错。"""
        result = await self._run_git(["worktree", "remove", "--force", wt.path])
        if result.returncode != 0:
            log.warning("git worktree remove failed: %s", result.stderr)
        await asyncio.sleep(0.1)
        await self._run_git(["branch", "-D", wt.branch])
        self.active.pop(name, None)

    async def auto_cleanup(self, name: str, head_commit: str) -> CleanupResult:
        """无变更时自动删除 worktree；有变更返回 kept=True。"""
        async with self._lock:
            if name not in self.active:
                return CleanupResult(kept=False)
            wt = self.active[name]
            if has_worktree_changes(wt.path, head_commit):
                return CleanupResult(kept=True)
            await self._remove_worktree(name, wt)
            return CleanupResult(kept=False)

    def list_worktrees(self) -> list[Worktree]:
        """返回当前 active 的 worktree 列表。"""
        return list(self.active.values())

    def get_current_session(self) -> WorktreeSession | None:
        """返回当前会话；未进入 worktree 时返回 None。"""
        return self.current_session

    async def restore_session(self) -> WorktreeSession | None:
        """从磁盘恢复 worktree 会话；文件不存在或损坏时返回 None。"""
        session = load_worktree_session(self._seacode_dir)
        if session is None:
            return None
        head_sha = read_worktree_head_sha(session.worktree_path)
        if head_sha is None:
            save_worktree_session(self._seacode_dir, None)
            return None
        flat = flatten_slug(session.worktree_name)
        wt = Worktree(
            name=session.worktree_name,
            path=session.worktree_path,
            branch=f"worktree-{flat}",
            based_on="unknown",
            head_commit=head_sha,
            created=datetime.now(),
        )
        self.active[session.worktree_name] = wt
        self.current_session = session
        return session
