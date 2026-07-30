"""Agent 工作目录上下文测试：验证相对路径和命令执行都落在当前 Agent 目录。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from seacode.agent import Agent
from seacode.client import ToolCallComplete
from seacode.tools import ToolRegistry
from seacode.tools.base import Tool, ToolCategory, ToolResult
from seacode.tools.bash import Bash
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


# 验证文件工具、搜索工具和 Bash 都按调用上下文解析相对路径。
# 在独立 parent/child 目录中执行，确保旧的进程 cwd 行为无法误通过。
@pytest.mark.asyncio
async def test_core_tools_use_explicit_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    monkeypatch.chdir(parent)

    cache = FileStateCache()
    target = child / "nested" / "note.txt"

    write_result = await WriteFile(file_state_cache=cache).execute(
        WriteFileParams(file_path="nested/note.txt", content="one"),
        work_dir=child,
    )
    assert write_result.is_error is False
    assert target.read_text(encoding="utf-8") == "one"
    assert not (parent / "nested" / "note.txt").exists()

    read_result = await ReadFile(file_state_cache=cache).execute(
        ReadFileParams(file_path="nested/note.txt"), work_dir=child
    )
    assert "one" in read_result.content

    edit_result = await EditFile(file_state_cache=cache).execute(
        EditFileParams(
            file_path="nested/note.txt", old_string="one", new_string="two"
        ),
        work_dir=child,
    )
    assert edit_result.is_error is False
    assert target.read_text(encoding="utf-8") == "two"

    source = child / "src" / "main.txt"
    source.parent.mkdir()
    source.write_text("needle\n", encoding="utf-8")
    glob_result = await Glob().execute(
        GlobParams(pattern="**/*.txt", path="."), work_dir=child
    )
    grep_result = await Grep().execute(
        GrepParams(pattern="needle", path="."), work_dir=child
    )
    assert "src" in glob_result.content and "main.txt" in glob_result.content
    assert "src" in grep_result.content and "needle" in grep_result.content

    command = f'"{sys.executable}" -c "import os; print(os.getcwd())"'
    bash_result = await Bash().execute(
        BashParams(command=command), work_dir=child
    )
    assert bash_result.is_error is False
    assert str(child.resolve()).lower() in bash_result.content.lower()


class _CaptureParams(BaseModel):
    value: str = ""


class _CaptureWorkDirTool(Tool):
    """记录 Agent 实际注入的工作目录。"""

    name = "CaptureWorkDir"
    description = "Capture the current work directory."
    params_model = _CaptureParams
    category = ToolCategory.READ

    async def execute(  # type: ignore[override]
        self,
        params: _CaptureParams,
        *,
        work_dir: str | Path | None = None,
    ) -> ToolResult:
        del params
        return ToolResult(content=str(work_dir))


class _Client:
    model = "test-model"


# 验证 Agent 的真实工具执行入口会把自身 work_dir 传给支持该参数的工具。
# 通过 _execute_single_tool_direct 覆盖串行与并发路径共用的上下文注入点。
@pytest.mark.asyncio
async def test_agent_injects_work_dir_into_tool_execution(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_CaptureWorkDirTool())
    agent = Agent(
        client=_Client(),  # type: ignore[arg-type]
        registry=registry,
        protocol="anthropic",
        work_dir=str(tmp_path),
    )

    result = await agent._execute_single_tool_direct(
        ToolCallComplete(
            tool_id="capture-1",
            tool_name="CaptureWorkDir",
            arguments={"value": "test"},
        )
    )

    assert result.result.is_error is False
    assert Path(result.result.content).resolve() == tmp_path.resolve()
