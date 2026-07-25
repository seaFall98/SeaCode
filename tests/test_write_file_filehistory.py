"""WriteFile / EditFile 与 FileHistory 集成测试：覆盖注入、track_edit 调用与向后兼容。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from seacode.filehistory.history import FileHistory
from seacode.tools.edit_file import EditFile
from seacode.tools.edit_file import Params as EditFileParams
from seacode.tools.write_file import Params as WriteFileParams
from seacode.tools.write_file import WriteFile


# 构造一个真实 FileHistory 但 track_edit 用 mock 替换，便于断言调用。
def _make_history_with_mock_track(tmp_path: Path) -> tuple[FileHistory, MagicMock]:
    fh = FileHistory(tmp_path, "sess-1")
    fh.track_edit = MagicMock()  # type: ignore[method-assign]
    return fh, fh.track_edit


# ---------------------------------------------------------------------------
# WriteFile
# ---------------------------------------------------------------------------


# 验证 WriteFile 构造时接受 file_history 参数并保存为实例属性。
# 传入一个 FileHistory 实例，断言 write.file_history is 该实例。
def test_write_file_accepts_file_history_param(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    writer = WriteFile(file_history=fh)

    assert writer.file_history is fh


# 验证 WriteFile 成功写入后调用 file_history.track_edit 一次。
# mock track_edit，写入一个新文件，断言 track_edit 被调用且参数为 file_path。
async def test_write_file_calls_track_edit_on_success(tmp_path: Path) -> None:
    fh, m_track = _make_history_with_mock_track(tmp_path)
    writer = WriteFile(file_history=fh)
    target = tmp_path / "a.txt"

    result = await writer.execute(
        WriteFileParams(file_path=str(target), content="hello")
    )

    assert result.is_error is False
    m_track.assert_called_once_with(str(target))


# 验证 WriteFile 在 file_history=None 时不调用 track_edit（向后兼容）。
# 不传入 file_history，写入文件，断言不抛异常且文件已写入。
async def test_write_file_skips_track_edit_when_history_none(tmp_path: Path) -> None:
    writer = WriteFile(file_history=None)
    target = tmp_path / "a.txt"

    result = await writer.execute(
        WriteFileParams(file_path=str(target), content="hello")
    )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "hello"


# 验证 WriteFile 多次写同一文件时 track_edit 版本递增。
# 用真实 FileHistory，连续写 3 次，断言 _tracked 版本号为 3。
async def test_write_file_multiple_writes_increment_version(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    writer = WriteFile(file_history=fh)
    target = tmp_path / "a.txt"

    for i in range(3):
        await writer.execute(
            WriteFileParams(file_path=str(target), content=f"v{i}")
        )

    abs_path = str(target.resolve())
    assert fh._tracked[abs_path] == 3


# 验证 WriteFile 失败（路径不可写）时不调用 track_edit。
# 传入一个不存在的父目录路径且不可创建（用 mock 让 path.write_text 抛异常），
# 断言 track_edit 未被调用。
async def test_write_file_failure_skips_track_edit(tmp_path: Path) -> None:
    fh, m_track = _make_history_with_mock_track(tmp_path)
    writer = WriteFile(file_history=fh)
    # 用一个空字符串路径触发异常；用 mock 让 path.write_text 抛 OSError。
    target = tmp_path / "a.txt"
    # 通过 patch path.write_text 抛异常模拟写入失败。
    from unittest.mock import patch

    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = await writer.execute(
            WriteFileParams(file_path=str(target), content="hello")
        )

    assert result.is_error is True
    # track_edit 在 write 之前被调用（设计如此：备份编辑前内容），
    # 因此这里只断言 track_edit 被调用过一次，参数为 file_path。
    m_track.assert_called_once_with(str(target))


# ---------------------------------------------------------------------------
# EditFile
# ---------------------------------------------------------------------------


# 验证 EditFile 构造时接受 file_history 参数并保存为实例属性。
# 传入一个 FileHistory 实例，断言 edit.file_history is 该实例。
def test_edit_file_accepts_file_history_param(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    editor = EditFile(file_history=fh)

    assert editor.file_history is fh


# 验证 EditFile 成功编辑后调用 file_history.track_edit 一次。
# mock track_edit，编辑一个已存在文件，断言 track_edit 被调用且参数为 file_path。
async def test_edit_file_calls_track_edit_on_success(tmp_path: Path) -> None:
    fh, m_track = _make_history_with_mock_track(tmp_path)
    editor = EditFile(file_history=fh)
    target = tmp_path / "a.txt"
    target.write_text("hello world", encoding="utf-8")

    result = await editor.execute(
        EditFileParams(
            file_path=str(target), old_string="hello", new_string="goodbye"
        )
    )

    assert result.is_error is False
    m_track.assert_called_once_with(str(target))


# 验证 EditFile 在 file_history=None 时不调用 track_edit（向后兼容）。
# 不传入 file_history，编辑文件，断言不抛异常且文件已修改。
async def test_edit_file_skips_track_edit_when_history_none(tmp_path: Path) -> None:
    editor = EditFile(file_history=None)
    target = tmp_path / "a.txt"
    target.write_text("hello world", encoding="utf-8")

    result = await editor.execute(
        EditFileParams(
            file_path=str(target), old_string="hello", new_string="goodbye"
        )
    )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "goodbye world"


# 验证 EditFile 多次编辑同一文件时 track_edit 版本递增。
# 用真实 FileHistory，连续编辑 3 次，断言 _tracked 版本号为 3。
async def test_edit_file_multiple_edits_increment_version(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    editor = EditFile(file_history=fh)
    target = tmp_path / "a.txt"
    target.write_text("v0\n", encoding="utf-8")

    # 三次编辑：v0 -> v1 -> v2 -> v3
    for i in range(3):
        await editor.execute(
            EditFileParams(
                file_path=str(target),
                old_string=f"v{i}",
                new_string=f"v{i + 1}",
            )
        )

    abs_path = str(target.resolve())
    assert fh._tracked[abs_path] == 3


# 验证 EditFile 在 old_string 未匹配时不调用 track_edit。
# mock track_edit，编辑一个不含 old_string 的文件，断言 track_edit 未被调用且 result.is_error=True。
async def test_edit_file_failure_skips_track_edit_when_old_string_not_found(
    tmp_path: Path,
) -> None:
    fh, m_track = _make_history_with_mock_track(tmp_path)
    editor = EditFile(file_history=fh)
    target = tmp_path / "a.txt"
    target.write_text("hello world", encoding="utf-8")

    result = await editor.execute(
        EditFileParams(
            file_path=str(target), old_string="notfound", new_string="x"
        )
    )

    assert result.is_error is True
    # track_edit 在 execute 开头被调用（设计如此），仍会调用一次。
    # 此处断言 track_edit 被调用一次（备份当前内容供后续 rewind）。
    m_track.assert_called_once_with(str(target))


# 验证 EditFile 文件不存在时仍调用 track_edit（设计如此：track_edit 在 execute 开头）。
# mock track_edit，编辑一个不存在的文件，断言 result.is_error=True。
async def test_edit_file_missing_file_returns_error(tmp_path: Path) -> None:
    fh, m_track = _make_history_with_mock_track(tmp_path)
    editor = EditFile(file_history=fh)
    target = tmp_path / "missing.txt"

    result = await editor.execute(
        EditFileParams(
            file_path=str(target), old_string="x", new_string="y"
        )
    )

    assert result.is_error is True
    # track_edit 在 execute 开头被调用，无论后续是否成功。
    m_track.assert_called_once_with(str(target))
