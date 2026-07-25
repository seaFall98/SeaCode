"""worktree 创建后初始化四步的单元测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from seacode.worktree.setup import (
    _copy_ignored_files,
    _copy_local_configs,
    _create_symlinks,
    _setup_git_hooks,
    perform_post_creation_setup,
)


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# 验证 _copy_local_configs 复制已存在的 .env 文件。
# 在 repo_root 创建 .env，调用后断言 wt_path/.env 存在。
def test_copy_local_configs_copies_env(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / ".env").write_text("KEY=value", encoding="utf-8")

    _copy_local_configs(repo, wt)

    assert (wt / ".env").exists()
    assert (wt / ".env").read_text(encoding="utf-8") == "KEY=value"


# 验证 _copy_local_configs 不存在的文件被跳过。
# repo_root 不创建 settings.local.json，调用后断言 wt_path 中不存在。
def test_copy_local_configs_skips_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()

    _copy_local_configs(repo, wt)

    assert not (wt / "settings.local.json").exists()


# 验证 _copy_local_configs 复制失败时只警告不抛异常。
# mock shutil.copy2 抛 OSError，断言不抛错。
def test_copy_local_configs_handles_oserror(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / ".env").write_text("KEY=value", encoding="utf-8")

    with patch("seacode.worktree.setup.shutil.copy2", side_effect=OSError("denied")):
        _copy_local_configs(repo, wt)  # 不抛异常

    assert not (wt / ".env").exists()


# 验证 _setup_git_hooks 优先使用 .husky 目录。
# 创建 .husky 目录，调用后断言 git config 被调用且路径含 .husky。
def test_setup_git_hooks_prefers_husky(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / ".husky").mkdir()

    with patch("seacode.worktree.setup.subprocess.run", return_value=_completed()) as mock_run:
        _setup_git_hooks(repo, wt)

    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert ".husky" in args[-1]


# 验证 _setup_git_hooks 回退到 .git/hooks。
# 不创建 .husky，创建 .git/hooks，断言 git config 被调用。
def test_setup_git_hooks_falls_back_to_git_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / ".git" / "hooks").mkdir(parents=True)

    with patch("seacode.worktree.setup.subprocess.run", return_value=_completed()) as mock_run:
        _setup_git_hooks(repo, wt)

    mock_run.assert_called_once()


# 验证 _setup_git_hooks 都不存在时不调用 git config。
# 不创建 .husky 也不创建 .git/hooks，断言 git config 未被调用。
def test_setup_git_hooks_skips_when_no_hooks_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()

    with patch("seacode.worktree.setup.subprocess.run", return_value=_completed()) as mock_run:
        _setup_git_hooks(repo, wt)

    mock_run.assert_not_called()


# 验证 _setup_git_hooks SubprocessError 不抛异常。
# mock subprocess.run 抛 SubprocessError，断言不抛错。
def test_setup_git_hooks_handles_subprocess_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / ".husky").mkdir()

    with patch(
        "seacode.worktree.setup.subprocess.run",
        side_effect=subprocess.SubprocessError("boom"),
    ):
        _setup_git_hooks(repo, wt)  # 不抛异常


# 验证 _create_symlinks 源存在目标不存在时创建符号链接。
# 创建源目录，调用后断言目标路径是符号链接。
# Windows 无管理员权限时跳过（os.symlink 需 SeCreateSymbolicLinkPrivilege）。
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 创建符号链接需要管理员权限",
)
def test_create_symlinks_creates_link(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / "node_modules").mkdir()

    _create_symlinks(repo, wt, ["node_modules"])

    assert (wt / "node_modules").is_symlink()


# 验证 _create_symlinks 源不存在时跳过。
# 不创建源目录，调用后断言目标不是符号链接。
def test_create_symlinks_skips_missing_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()

    _create_symlinks(repo, wt, ["node_modules"])

    assert not (wt / "node_modules").is_symlink()


# 验证 _create_symlinks 目标已存在时跳过。
# 创建源与目标，调用后断言目标不是符号链接（仍是原目录）。
def test_create_symlinks_skips_existing_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / "node_modules").mkdir()
    (wt / "node_modules").mkdir()

    _create_symlinks(repo, wt, ["node_modules"])

    assert not (wt / "node_modules").is_symlink()


# 验证 _create_symlinks OSError 时只警告不抛异常。
# mock os.symlink 抛 OSError，断言不抛错。
def test_create_symlinks_handles_oserror(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / "node_modules").mkdir()

    with patch("seacode.worktree.setup.os.symlink", side_effect=OSError("denied")):
        _create_symlinks(repo, wt, ["node_modules"])  # 不抛异常


# 验证 _copy_ignored_files 无 .worktreeinclude 时跳过。
# 不创建 .worktreeinclude，调用后断言不调用 git ls-files。
def test_copy_ignored_files_skips_when_no_include_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()

    with patch("seacode.worktree.setup.subprocess.run", return_value=_completed()) as mock_run:
        _copy_ignored_files(repo, wt)

    mock_run.assert_not_called()


# 验证 _copy_ignored_files 模式匹配时复制文件。
# 创建 .worktreeinclude 与匹配文件，调用后断言文件被复制到 wt_path。
def test_copy_ignored_files_copies_matched(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / ".worktreeinclude").write_text("*.env\n", encoding="utf-8")
    (repo / "secrets.env").write_text("KEY=value", encoding="utf-8")

    with patch(
        "seacode.worktree.setup.subprocess.run",
        return_value=_completed(stdout="secrets.env\n"),
    ):
        _copy_ignored_files(repo, wt)

    assert (wt / "secrets.env").exists()
    assert (wt / "secrets.env").read_text(encoding="utf-8") == "KEY=value"


# 验证 _copy_ignored_files git ls-files 失败时跳过。
# mock subprocess.run 抛 SubprocessError，断言不抛错且不复制文件。
def test_copy_ignored_files_handles_git_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / ".worktreeinclude").write_text("*.env\n", encoding="utf-8")

    with patch(
        "seacode.worktree.setup.subprocess.run",
        side_effect=subprocess.SubprocessError("boom"),
    ):
        _copy_ignored_files(repo, wt)  # 不抛异常

    assert not (wt / "secrets.env").exists()


# 验证 perform_post_creation_setup 调用全部四步。
# mock 四个内部函数，断言每个都被调用一次。
def test_perform_post_creation_setup_calls_all_steps(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()

    with (
        patch("seacode.worktree.setup._copy_local_configs") as m1,
        patch("seacode.worktree.setup._setup_git_hooks") as m2,
        patch("seacode.worktree.setup._create_symlinks") as m3,
        patch("seacode.worktree.setup._copy_ignored_files") as m4,
    ):
        perform_post_creation_setup(repo, wt, ["node_modules"])

    m1.assert_called_once_with(repo, wt)
    m2.assert_called_once_with(repo, wt)
    m3.assert_called_once_with(repo, wt, ["node_modules"])
    m4.assert_called_once_with(repo, wt)


# 验证 perform_post_creation_setup symlink_directories 为 None 时传空列表。
# mock _create_symlinks 检查传入空列表。
def test_perform_post_creation_setup_none_symlinks_passes_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()

    with (
        patch("seacode.worktree.setup._copy_local_configs"),
        patch("seacode.worktree.setup._setup_git_hooks"),
        patch("seacode.worktree.setup._create_symlinks") as m3,
        patch("seacode.worktree.setup._copy_ignored_files"),
    ):
        perform_post_creation_setup(repo, wt, None)

    m3.assert_called_once_with(repo, wt, [])
