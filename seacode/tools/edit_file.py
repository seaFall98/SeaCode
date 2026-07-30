"""EditFile 工具：按唯一匹配替换文件中的字符串。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from seacode.tools.base import Tool, ToolCategory, ToolResult, resolve_tool_path
from seacode.tools.diff import build_diff

if TYPE_CHECKING:
    from seacode.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    """EditFile 参数模型。"""

    file_path: str = Field(description="Path to the file to edit")
    old_string: str = Field(
        description="The exact string to find and replace (must be unique in file)"
    )
    new_string: str = Field(description="The replacement string")


class EditFile(Tool):
    """替换文件中唯一匹配的字符串，必须先 ReadFile 读取目标文件。"""

    name = "EditFile"
    description = (
        "Replace an exact string in a file. The old_string must appear exactly once in the file.\n"
        "You MUST read the file with ReadFile before editing. This tool will fail otherwise."
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
        if not path.exists():
            return ToolResult(content=f"Error: file not found: {params.file_path}", is_error=True)

        # 编辑前必须先读过且未被外部修改。
        if self._state_cache:
            resolved = str(path.resolve())
            ok, err_msg = self._state_cache.check(resolved)
            if not ok:
                return ToolResult(content=err_msg, is_error=True)

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(content=f"Error reading file: {e}", is_error=True)

        # old_string 必须唯一匹配：0 次报未命中，>1 次报多次。
        count = content.count(params.old_string)
        if count == 0:
            return ToolResult(content="Error: old_string not found in file", is_error=True)
        if count > 1:
            return ToolResult(
                content=f"Error: old_string found {count} times, must be unique",
                is_error=True,
            )

        new_content = content.replace(params.old_string, params.new_string, 1)
        try:
            path.write_text(new_content, encoding="utf-8")
            if self._state_cache:
                self._state_cache.update(str(path.resolve()))
        except Exception as e:
            return ToolResult(content=f"Error writing file: {e}", is_error=True)

        diff = build_diff(content, new_content)
        addition_word = "addition" if diff.additions == 1 else "additions"
        removal_word = "removal" if diff.removals == 1 else "removals"
        summary = (
            f"Updated {params.file_path} with {diff.additions} {addition_word} "
            f"and {diff.removals} {removal_word}"
        )
        return ToolResult(content=f"{summary}\n{diff.text}")
