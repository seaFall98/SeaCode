"""Grep 工具：按正则搜索文件内容。"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from seacode.tools.base import SKIP_DIRS, Tool, ToolCategory, ToolResult


class Params(BaseModel):
    """Grep 参数模型。"""

    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Base directory to search from")
    include: str = Field(default="", description="Glob filter for filenames (e.g. '*.py')")


class Grep(Tool):
    """按正则搜索文件内容，返回 file:line:content 格式的匹配结果。"""

    name = "Grep"
    description = "Search file contents using a regex pattern, returning file:line:content matches."
    params_model = Params
    category = ToolCategory.READ
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        base = Path(params.path)
        if not base.exists():
            return ToolResult(content=f"Error: path not found: {params.path}", is_error=True)

        try:
            regex = re.compile(params.pattern)
        except re.error as e:
            return ToolResult(content=f"Error: invalid regex: {e}", is_error=True)

        glob_pattern = params.include if params.include else "**/*"
        if not glob_pattern.startswith("**/"):
            glob_pattern = "**/" + glob_pattern

        results: list[str] = []
        for file_path in sorted(base.glob(glob_pattern)):
            if not file_path.is_file():
                continue
            if any(part in SKIP_DIRS for part in file_path.parts):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            for line_num, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = file_path.relative_to(base)
                    results.append(f"{rel}:{line_num}:{line}")

        if not results:
            return ToolResult(content="No matches found.")
        return ToolResult(content="\n".join(results))
