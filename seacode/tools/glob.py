"""Glob 工具：按 glob 模式查找文件。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from seacode.tools.base import SKIP_DIRS, Tool, ToolCategory, ToolResult


class Params(BaseModel):
    """Glob 参数模型。"""

    pattern: str = Field(description="Glob pattern to match (e.g. '**/*.py')")
    path: str = Field(default=".", description="Base directory to search from")


class Glob(Tool):
    """按 glob 模式查找文件，返回相对路径并按修改时间倒序排列。"""

    name = "Glob"
    description = "Find files matching a glob pattern, returning relative paths."
    params_model = Params
    category = ToolCategory.READ
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        base = Path(params.path)
        if not base.exists():
            return ToolResult(content=f"Error: path not found: {params.path}", is_error=True)

        try:
            found = [
                p
                for p in base.glob(params.pattern)
                if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
            ]
            # 按修改时间倒序，最近修改的排前面。
            found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            matches = [str(p.relative_to(base)) for p in found]
        except Exception as e:
            return ToolResult(content=f"Error: {e}", is_error=True)

        if not matches:
            return ToolResult(content="No files matched the pattern.")
        return ToolResult(content="\n".join(matches))
