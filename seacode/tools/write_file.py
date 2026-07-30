"""WriteFile 工具：写入文件，受 read-before-edit 门控约束。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from seacode.tools.base import Tool, ToolCategory, ToolResult, resolve_tool_path

if TYPE_CHECKING:
    from seacode.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    """WriteFile 参数模型。"""

    file_path: str = Field(description="Path to the file to write")
    content: str = Field(description="Content to write to the file")


class WriteFile(Tool):
    """写入文件内容，覆盖已存在文件；已存在文件必须先被 ReadFile 读过。"""

    name = "WriteFile"
    description = (
        "Write content to a file, creating parent directories if needed. "
        "Overwrites existing files.\n"
        "You MUST read existing files with ReadFile before overwriting them. "
        "This tool will fail otherwise."
    )
    params_model = Params
    category = ToolCategory.WRITE

    # file_history 为第 13 步 worktree changes 保留入参守卫，默认 None 不调用。
    def __init__(
        self,
        file_history: Any = None,
        file_state_cache: FileStateCache | None = None,
    ) -> None:
        self.file_history = file_history
        self._state_cache = file_state_cache

    async def execute(  # type: ignore[override]
        self, params: Params, *, work_dir: str | Path | None = None
    ) -> ToolResult:
        path = resolve_tool_path(params.file_path, work_dir)
        # 第 13 步 worktree changes 会填充 file_history；本步不消费。
        if self.file_history is not None:
            track_path = path if work_dir is not None else params.file_path
            self.file_history.track_edit(track_path)

        # 已存在文件必须先被读过且未被外部修改。
        if self._state_cache and path.exists():
            resolved = str(path.resolve())
            ok, err_msg = self._state_cache.check(resolved)
            if not ok:
                return ToolResult(content=err_msg, is_error=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params.content, encoding="utf-8")
            if self._state_cache:
                self._state_cache.update(str(path.resolve()))
        except Exception as e:
            return ToolResult(content=f"Error writing file: {e}", is_error=True)
        return ToolResult(content=f"Successfully wrote to {params.file_path}")
