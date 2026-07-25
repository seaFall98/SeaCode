"""worktree 创建后初始化：复制本地配置、设置 git hooks、创建符号链接、复制忽略文件。"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# 复制到新 worktree 的本地配置文件列表。
LOCAL_CONFIG_FILES = ["settings.local.json", ".env"]


def perform_post_creation_setup(
    repo_root: str | Path,
    wt_path: str | Path,
    symlink_directories: list[str] | None,
) -> None:
    """四步 best-effort 初始化；每步独立 try/except，单步失败不影响整体。"""
    repo = Path(repo_root)
    wt = Path(wt_path)
    _copy_local_configs(repo, wt)
    _setup_git_hooks(repo, wt)
    _create_symlinks(repo, wt, symlink_directories or [])
    _copy_ignored_files(repo, wt)


def _copy_local_configs(repo_root: Path, wt_path: Path) -> None:
    """复制 LOCAL_CONFIG_FILES 中存在的本地配置文件到 worktree。"""
    for f in LOCAL_CONFIG_FILES:
        src = repo_root / f
        if src.exists():
            try:
                shutil.copy2(src, wt_path / f)
            except OSError as e:
                log.warning("failed to copy %s: %s", f, e)


def _setup_git_hooks(repo_root: Path, wt_path: Path) -> None:
    """检测 .husky 或 .git/hooks，配置 worktree 的 core.hooksPath。"""
    husky = repo_root / ".husky"
    git_hooks = repo_root / ".git" / "hooks"
    hooks_path = husky if husky.is_dir() else (git_hooks if git_hooks.is_dir() else None)
    if hooks_path is None:
        return
    try:
        subprocess.run(
            ["git", "config", "core.hooksPath", str(hooks_path)],
            cwd=str(wt_path),
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("failed to setup git hooks: %s", e)


def _create_symlinks(repo_root: Path, wt_path: Path, symlink_directories: list[str]) -> None:
    """为 symlink_directories 中存在的目录创建符号链接到 worktree。"""
    for d in symlink_directories:
        src = repo_root / d
        dst = wt_path / d
        if src.exists() and not (dst.exists() or dst.is_symlink()):
            try:
                os.symlink(src, dst)
            except OSError as e:
                log.warning("failed to symlink %s: %s", d, e)


def _copy_ignored_files(repo_root: Path, wt_path: Path) -> None:
    """根据 .worktreeinclude 模式复制被 git 忽略的文件到 worktree。"""
    include_file = repo_root / ".worktreeinclude"
    if not include_file.exists():
        return
    patterns = [
        line.strip()
        for line in include_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("git ls-files failed: %s", e)
        return
    if result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        for pattern in patterns:
            if fnmatch.fnmatch(line, pattern):
                src = repo_root / line
                dst = wt_path / line
                if src.is_file():
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                    except OSError as e:
                        log.warning("failed to copy ignored %s: %s", line, e)
                break
