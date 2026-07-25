"""worktree session 持久化的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from seacode.worktree.models import WorktreeSession
from seacode.worktree.session import (
    SESSION_FILENAME,
    load_worktree_session,
    save_worktree_session,
)


def _make_session(**overrides: object) -> WorktreeSession:
    """构造一个完整 WorktreeSession，参数可覆盖。"""
    base = {
        "original_cwd": "/repo",
        "worktree_path": "/repo/.seacode/worktrees/feat-x",
        "worktree_name": "feat-x",
        "original_branch": "main",
        "original_head_commit": "abc123",
        "session_id": "sess-1",
        "hook_based": False,
    }
    base.update(overrides)
    return WorktreeSession(**base)  # type: ignore[arg-type]


# 验证 save_worktree_session(None) 删除已存在的会话文件。
# 先写入会话文件，再传 None 删除，断言文件不存在。
def test_save_none_deletes_existing_file(tmp_path: Path) -> None:
    save_worktree_session(tmp_path, _make_session())
    assert (tmp_path / SESSION_FILENAME).exists()
    save_worktree_session(tmp_path, None)
    assert not (tmp_path / SESSION_FILENAME).exists()


# 验证 save_worktree_session(None) 在文件不存在时不抛异常。
# 在空目录传 None，断言不抛错且文件仍不存在。
def test_save_none_no_file_is_silent(tmp_path: Path) -> None:
    save_worktree_session(tmp_path, None)
    assert not (tmp_path / SESSION_FILENAME).exists()


# 验证 save_worktree_session 写入合法 JSON 且字段完整。
# 写入会话后读取文件解析 JSON，断言字段值与传入一致。
def test_save_writes_valid_json(tmp_path: Path) -> None:
    session = _make_session(hook_based=True)
    save_worktree_session(tmp_path, session)
    data = json.loads((tmp_path / SESSION_FILENAME).read_text(encoding="utf-8"))
    assert data["original_cwd"] == "/repo"
    assert data["worktree_path"] == "/repo/.seacode/worktrees/feat-x"
    assert data["worktree_name"] == "feat-x"
    assert data["original_branch"] == "main"
    assert data["original_head_commit"] == "abc123"
    assert data["session_id"] == "sess-1"
    assert data["hook_based"] is True


# 验证 load_worktree_session 文件不存在时返回 None。
# 在空目录调用，断言返回 None。
def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_worktree_session(tmp_path) is None


# 验证 load_worktree_session 合法 JSON 返回 WorktreeSession。
# 先写入合法会话，再加载，断言字段值与写入一致。
def test_load_returns_session_for_valid_json(tmp_path: Path) -> None:
    save_worktree_session(tmp_path, _make_session(hook_based=True))
    loaded = load_worktree_session(tmp_path)
    assert loaded is not None
    assert loaded.original_cwd == "/repo"
    assert loaded.worktree_path == "/repo/.seacode/worktrees/feat-x"
    assert loaded.worktree_name == "feat-x"
    assert loaded.original_branch == "main"
    assert loaded.original_head_commit == "abc123"
    assert loaded.session_id == "sess-1"
    assert loaded.hook_based is True


# 验证 load_worktree_session 非法 JSON 返回 None。
# 写入非法 JSON 后加载，断言返回 None。
def test_load_returns_none_for_invalid_json(tmp_path: Path) -> None:
    (tmp_path / SESSION_FILENAME).write_text("{not valid json", encoding="utf-8")
    assert load_worktree_session(tmp_path) is None


# 验证 load_worktree_session 缺 worktree_path 字段返回 None。
# 写入只含 original_cwd 的 JSON，断言返回 None。
def test_load_returns_none_when_missing_worktree_path(tmp_path: Path) -> None:
    (tmp_path / SESSION_FILENAME).write_text(
        json.dumps({"original_cwd": "/repo"}), encoding="utf-8"
    )
    assert load_worktree_session(tmp_path) is None


# 验证 load_worktree_session 空 dict 返回 None。
# 写入空 dict JSON，断言返回 None。
def test_load_returns_none_for_empty_dict(tmp_path: Path) -> None:
    (tmp_path / SESSION_FILENAME).write_text("{}", encoding="utf-8")
    assert load_worktree_session(tmp_path) is None


# 验证 load_worktree_session session_id 与 hook_based 缺省时取默认值。
# 写入只含必填字段的 JSON，断言 session_id 为空串、hook_based 为 False。
def test_load_uses_defaults_for_optional_fields(tmp_path: Path) -> None:
    data = {
        "original_cwd": "/repo",
        "worktree_path": "/wt",
        "worktree_name": "feat",
        "original_branch": "main",
        "original_head_commit": "abc",
    }
    (tmp_path / SESSION_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    loaded = load_worktree_session(tmp_path)
    assert loaded is not None
    assert loaded.session_id == ""
    assert loaded.hook_based is False


# 验证 save_worktree_session 创建父目录。
# 传入嵌套路径，断言父目录被创建且文件写入成功。
def test_save_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / ".seacode"
    save_worktree_session(nested, _make_session())
    assert (nested / SESSION_FILENAME).exists()
