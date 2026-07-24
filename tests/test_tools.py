from __future__ import annotations

import sys
from pathlib import Path

import pytest

from seacode.tools.base import SKIP_DIRS
from seacode.tools.bash import Bash, _exit_code_hint, _interpret_exit_code
from seacode.tools.bash import Params as BashParams
from seacode.tools.edit_file import EditFile
from seacode.tools.edit_file import Params as EditFileParams
from seacode.tools.file_state_cache import FileStateCache
from seacode.tools.glob import Glob
from seacode.tools.glob import Params as GlobParams
from seacode.tools.grep import Grep
from seacode.tools.grep import Params as GrepParams
from seacode.tools.read_file import Params as ReadFileParams
from seacode.tools.read_file import ReadFile
from seacode.tools.write_file import Params as WriteFileParams
from seacode.tools.write_file import WriteFile


# 装配共享 FileStateCache 的三个文件工具，模拟注册中心的注入方式。
def _file_tools() -> tuple[ReadFile, WriteFile, EditFile, FileStateCache]:
    cache = FileStateCache()
    return (
        ReadFile(file_state_cache=cache),
        WriteFile(file_state_cache=cache),
        EditFile(file_state_cache=cache),
        cache,
    )


# 写入指定内容到路径，便于测试快速准备文件。
def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# ReadFile
# ---------------------------------------------------------------------------


# 验证 ReadFile 返回带行号的内容，格式为"行号<TAB>内容"。
# 写入三行文本，读取后断言每行前缀为从 1 开始的行号与制表符分隔。
@pytest.mark.asyncio
async def test_read_file_returns_content_with_line_numbers(tmp_path: Path) -> None:
    path = _write(tmp_path / "sample.txt", "alpha\nbeta\ngamma\n")
    reader, _, _, _ = _file_tools()

    result = await reader.execute(ReadFileParams(file_path=str(path)))

    assert result.is_error is False
    assert result.content == "1\talpha\n2\tbeta\n3\tgamma"


# 验证 ReadFile 的 offset 与 limit 参数能截取中间行段。
# 写入五行文本，从第 2 行起读取两行，断言只返回第 2、3 行且行号连续。
@pytest.mark.asyncio
async def test_read_file_respects_offset_and_limit(tmp_path: Path) -> None:
    path = _write(tmp_path / "lines.txt", "one\ntwo\nthree\nfour\nfive\n")
    reader, _, _, _ = _file_tools()

    result = await reader.execute(
        ReadFileParams(file_path=str(path), offset=1, limit=2)
    )

    assert result.content == "2\ttwo\n3\tthree"


# 验证 ReadFile 对不存在文件返回带路径的错误结果。
# 指向不存在的路径，断言 is_error 为 True 且内容包含 file not found。
@pytest.mark.asyncio
async def test_read_file_reports_missing_file(tmp_path: Path) -> None:
    reader, _, _, _ = _file_tools()
    missing = tmp_path / "absent.txt"

    result = await reader.execute(ReadFileParams(file_path=str(missing)))

    assert result.is_error is True
    assert "file not found" in result.content


# 验证 ReadFile 拒绝把目录路径当作文件读取。
# 传入目录路径，断言 is_error 为 True 且内容包含 not a file。
@pytest.mark.asyncio
async def test_read_file_rejects_directory_path(tmp_path: Path) -> None:
    reader, _, _, _ = _file_tools()

    result = await reader.execute(ReadFileParams(file_path=str(tmp_path)))

    assert result.is_error is True
    assert "not a file" in result.content


# ---------------------------------------------------------------------------
# WriteFile
# ---------------------------------------------------------------------------


# 验证 WriteFile 对已存在但未读过的文件触发 read-before-edit 门控。
# 预先写入文件但不调用 ReadFile，直接 WriteFile 应返回门控错误。
@pytest.mark.asyncio
async def test_write_file_blocks_unread_existing_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "existing.txt", "old\n")
    _, writer, _, _ = _file_tools()

    result = await writer.execute(WriteFileParams(file_path=str(path), content="new\n"))

    assert result.is_error is True
    assert "has not been read" in result.content
    assert path.read_text(encoding="utf-8") == "old\n"


# 验证 WriteFile 能正常写入新文件并返回成功信息。
# 写入不存在的新文件，断言内容落盘且结果包含成功提示。
@pytest.mark.asyncio
async def test_write_file_creates_new_file(tmp_path: Path) -> None:
    path = tmp_path / "new.txt"
    _, writer, _, _ = _file_tools()

    result = await writer.execute(WriteFileParams(file_path=str(path), content="hello\n"))

    assert result.is_error is False
    assert "Successfully wrote" in result.content
    assert path.read_text(encoding="utf-8") == "hello\n"


# 验证 WriteFile 在父目录缺失时自动创建目录层级。
# 写入嵌套路径，断言父目录被创建且文件内容正确。
@pytest.mark.asyncio
async def test_write_file_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "dir" / "file.txt"
    _, writer, _, _ = _file_tools()

    result = await writer.execute(WriteFileParams(file_path=str(path), content="data"))

    assert result.is_error is False
    assert path.read_text(encoding="utf-8") == "data"


# 验证 WriteFile 在 ReadFile 之后允许覆盖已存在文件。
# 先写入并读取文件，再用 WriteFile 覆盖，断言新内容落盘。
@pytest.mark.asyncio
async def test_write_file_allows_overwrite_after_read(tmp_path: Path) -> None:
    path = _write(tmp_path / "rw.txt", "initial\n")
    reader, writer, _, _ = _file_tools()
    await reader.execute(ReadFileParams(file_path=str(path)))

    result = await writer.execute(
        WriteFileParams(file_path=str(path), content="replaced\n")
    )

    assert result.is_error is False
    assert path.read_text(encoding="utf-8") == "replaced\n"


# ---------------------------------------------------------------------------
# EditFile
# ---------------------------------------------------------------------------


# 验证 EditFile 在 old_string 未出现时返回零次匹配错误。
# 先读取文件，再用不存在的 old_string 编辑，断言 is_error 且内容含 not found。
@pytest.mark.asyncio
async def test_edit_file_reports_zero_matches(tmp_path: Path) -> None:
    path = _write(tmp_path / "edit.txt", "alpha\nbeta\n")
    reader, _, editor, _ = _file_tools()
    await reader.execute(ReadFileParams(file_path=str(path)))

    result = await editor.execute(
        EditFileParams(file_path=str(path), old_string="gamma", new_string="delta")
    )

    assert result.is_error is True
    assert "not found" in result.content
    assert path.read_text(encoding="utf-8") == "alpha\nbeta\n"


# 验证 EditFile 在 old_string 出现多次时返回非唯一错误。
# 写入重复行并读取，编辑非唯一字符串，断言 is_error 且内容含多次匹配提示。
@pytest.mark.asyncio
async def test_edit_file_reports_multiple_matches(tmp_path: Path) -> None:
    path = _write(tmp_path / "dup.txt", "dup\n dup\n")
    reader, _, editor, _ = _file_tools()
    await reader.execute(ReadFileParams(file_path=str(path)))

    result = await editor.execute(
        EditFileParams(file_path=str(path), old_string="dup", new_string="x")
    )

    assert result.is_error is True
    assert "2 times" in result.content


# 验证 EditFile 唯一匹配时成功替换并返回 diff 输出。
# 写入唯一字符串并读取，编辑后断言文件更新、结果含 addition 与 diff 文本。
@pytest.mark.asyncio
async def test_edit_file_replaces_unique_match_and_returns_diff(tmp_path: Path) -> None:
    path = _write(tmp_path / "uniq.txt", "line one\nline two\nline three\n")
    reader, _, editor, _ = _file_tools()
    await reader.execute(ReadFileParams(file_path=str(path)))

    result = await editor.execute(
        EditFileParams(
            file_path=str(path), old_string="line two", new_string="line TWO"
        )
    )

    assert result.is_error is False
    assert "addition" in result.content
    assert "+" in result.content
    assert "-" in result.content
    assert path.read_text(encoding="utf-8") == "line one\nline TWO\nline three\n"


# 验证 EditFile 对未读过的文件触发 read-before-edit 门控。
# 预先写入文件但不读取，直接编辑应返回门控错误且文件不变。
@pytest.mark.asyncio
async def test_edit_file_blocks_unread_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "guard.txt", "original\n")
    _, _, editor, _ = _file_tools()

    result = await editor.execute(
        EditFileParams(
            file_path=str(path), old_string="original", new_string="changed"
        )
    )

    assert result.is_error is True
    assert "has not been read" in result.content
    assert path.read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------


# 验证 Bash 命令超时返回 is_error=True 与超时提示。
# 用 Python sleep 配合短超时，断言结果为错误且内容含 timed out。
@pytest.mark.asyncio
async def test_bash_reports_timeout() -> None:
    bash = Bash()
    sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'

    result = await bash.execute(BashParams(command=sleep_cmd, timeout=1))

    assert result.is_error is True
    assert "timed out" in result.content


# 验证 Bash 对零输出命令返回 "(no output)" 占位提示。
# 执行无输出的 Python 语句，断言内容为占位提示且非错误。
@pytest.mark.asyncio
async def test_bash_reports_no_output_placeholder() -> None:
    bash = Bash()
    cmd = f'"{sys.executable}" -c "pass"'

    result = await bash.execute(BashParams(command=cmd))

    assert result.is_error is False
    assert result.content == "(no output)"


# 验证 Bash 正常执行命令并合并输出，is_error 始终为 False。
# 执行打印命令，断言内容含输出文本且非错误。
@pytest.mark.asyncio
async def test_bash_returns_merged_output_on_success() -> None:
    bash = Bash()
    cmd = f'"{sys.executable}" -c "print(\'sea-output\')"'

    result = await bash.execute(BashParams(command=cmd))

    assert result.is_error is False
    assert "sea-output" in result.content


# 验证 Bash 对非零退出码追加提示但 is_error 仍为 False。
# 执行以退出码 1 结束的命令，断言非错误且内容含 Exit code 提示。
@pytest.mark.asyncio
async def test_bash_appends_exit_code_hint_without_error() -> None:
    bash = Bash()
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'

    result = await bash.execute(BashParams(command=cmd))

    assert result.is_error is False
    assert "Exit code 1" in result.content


# 验证 grep 返回 1 在退出码语义中不被视为真正错误。
# 直接调用判定函数，断言 exit 1 为非错误、exit 2 为错误。
def test_bash_interpret_grep_exit_code_semantics() -> None:
    assert _interpret_exit_code("grep pattern file", 1) is False
    assert _interpret_exit_code("grep pattern file", 2) is True


# 验证 grep 无匹配时退出码提示附带语义说明。
# 直接调用提示函数，断言 grep 提示含 no matches found，普通命令只有退出码。
def test_bash_exit_code_hint_includes_grep_semantics() -> None:
    assert _exit_code_hint("grep pattern file", 1) == "Exit code 1 (no matches found)"
    assert _exit_code_hint("unknown-cmd", 1) == "Exit code 1"


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


# 验证 Glob 按 glob 模式返回匹配文件并过滤 SKIP_DIRS 目录。
# 在主目录放 .py 文件、在 .git 内放 .py 文件，断言只返回主目录文件。
@pytest.mark.asyncio
async def test_glob_matches_pattern_and_skips_dirs(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "x\n")
    _write(tmp_path / "b.py", "y\n")
    _write(tmp_path / ".git" / "hidden.py", "z\n")
    glob = Glob()

    result = await glob.execute(GlobParams(pattern="**/*.py", path=str(tmp_path)))

    assert result.is_error is False
    matches = set(result.content.split("\n"))
    assert matches == {"a.py", "b.py"}
    assert ".git" in SKIP_DIRS


# 验证 Glob 在无匹配时返回友好提示而非错误。
# 用不存在的扩展名模式搜索，断言内容含 No files matched 且非错误。
@pytest.mark.asyncio
async def test_glob_reports_no_match(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "x\n")
    glob = Glob()

    result = await glob.execute(
        GlobParams(pattern="**/*.nonexistent", path=str(tmp_path))
    )

    assert result.is_error is False
    assert "No files matched" in result.content


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------


# 验证 Grep 按正则搜索文件内容并返回 file:line:content 格式。
# 写入含目标字符串的文件，断言返回结果含文件名、行号与匹配行。
@pytest.mark.asyncio
async def test_grep_returns_file_line_content_matches(tmp_path: Path) -> None:
    _write(tmp_path / "src.py", "import os\nprint('hello')\n")
    grep = Grep()

    result = await grep.execute(GrepParams(pattern="hello", path=str(tmp_path)))

    assert result.is_error is False
    assert "src.py:2:print('hello')" in result.content


# 验证 Grep 的 include 参数只匹配指定文件名模式。
# 写入 .py 与 .txt 文件各一个，include 限定 .py 后断言 .txt 不在结果中。
@pytest.mark.asyncio
async def test_grep_respects_include_filter(tmp_path: Path) -> None:
    _write(tmp_path / "match.py", "target line\n")
    _write(tmp_path / "skip.txt", "target line\n")
    grep = Grep()

    result = await grep.execute(
        GrepParams(pattern="target", path=str(tmp_path), include="*.py")
    )

    assert result.is_error is False
    assert "match.py" in result.content
    assert "skip.txt" not in result.content


# 验证 Grep 在无匹配时返回友好提示而非错误。
# 搜索不存在的字符串，断言内容含 No matches found 且非错误。
@pytest.mark.asyncio
async def test_grep_reports_no_match(tmp_path: Path) -> None:
    _write(tmp_path / "data.py", "alpha\nbeta\n")
    grep = Grep()

    result = await grep.execute(
        GrepParams(pattern="nonexistent", path=str(tmp_path))
    )

    assert result.is_error is False
    assert "No matches found" in result.content
