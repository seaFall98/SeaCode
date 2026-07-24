"""ReadFile 工具：读取文件并返回带行号的内容。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from seacode.tools.base import Tool, ToolCategory, ToolResult

if TYPE_CHECKING:
    from seacode.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    """ReadFile 参数模型。"""

    file_path: str = Field(description="Absolute or relative path to the file to read")
    offset: int = Field(default=0, description="Line offset to start reading from (0-based)")
    limit: int = Field(default=2000, description="Maximum number of lines to read")


class ReadFile(Tool):
    """读取文件内容并附加行号，同时记录 mtime 供编辑门控使用。"""

    name = "ReadFile"
    description = "Read a file and return its contents with line numbers."
    params_model = Params
    category = ToolCategory.READ
    is_concurrency_safe = True

    # file_state_cache 由注册中心注入；为 None 时跳过 mtime 记录。
    def __init__(self, file_state_cache: FileStateCache | None = None) -> None:
        self._state_cache = file_state_cache

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        path = Path(params.file_path)
        if not path.exists():
            return ToolResult(content=f"Error: file not found: {params.file_path}", is_error=True)
        if not path.is_file():
            return ToolResult(content=f"Error: not a file: {params.file_path}", is_error=True)

        resolved = str(path.resolve())

        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(content=f"Error reading file: {e}", is_error=True)

        # 记录 mtime 供 WriteFile/EditFile 做 read-before-edit 门控。
        if self._state_cache:
            try:
                mtime_ns = path.stat().st_mtime_ns
                self._state_cache.record(resolved, mtime_ns)
            except OSError:
                pass

        lines = text.splitlines()
        selected = lines[params.offset : params.offset + params.limit]
        numbered = [f"{i + params.offset + 1}\t{line}" for i, line in enumerate(selected)]
        return ToolResult(content="\n".join(numbered))
