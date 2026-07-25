# SyntheticOutput 工具：以结构化 JSON 输出最终结果，供非交互/协调者模式回合收尾。
"""SyntheticOutput 工具实现。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from seacode.tools.base import Tool, ToolCategory, ToolResult


class SyntheticOutputParams(BaseModel):
    # output 支持对象/数组/字符串；对象/数组序列化为 JSON，字符串原样返回。
    output: dict[str, Any] | list[Any] | str


class SyntheticOutputTool(Tool):
    # 结构化输出工具；设计为非交互/协调者模式下的最终工具调用：
    # 执行后模型给出无工具调用的最终回复使 Agent 循环自然终止；
    # 结构化结果同时存入 self.last_output，供调用方（如 TaskManager）在
    # run_to_completion 结束后直接读取，无需解析对话历史。
    name = "SyntheticOutput"
    description = (
        "以结构化 JSON 格式输出最终结果；"
        "用于非交互或协调者模式会话中返回结构化最终回复"
    )
    params_model = SyntheticOutputParams
    category = ToolCategory.READ
    is_concurrency_safe = True
    is_system_tool = True

    def __init__(self, json_schema: dict[str, Any] | None = None) -> None:
        self._json_schema = json_schema
        # 缓存最近一次结构化输出，供调用方在循环结束后读取。
        self.last_output: str = ""

    async def execute(self, params: BaseModel) -> ToolResult:
        p: SyntheticOutputParams = params  # type: ignore[assignment]

        if self._json_schema is not None:
            error = self._validate_schema(p.output)
            if error:
                return ToolResult(
                    content=f"输出不符合所需 schema: {error}", is_error=True
                )

        if isinstance(p.output, str):
            content = p.output
        else:
            content = json.dumps(p.output, ensure_ascii=False, indent=2)

        # 缓存结构化结果；调用方可在 Agent 循环结束后读取此字段作为最终结果。
        self.last_output = content
        return ToolResult(content=content, is_error=False)

    # 校验输出是否符合 schema；仅检查 type 与 required，失败返回错误描述。
    def _validate_schema(self, data: Any) -> str | None:
        schema = self._json_schema
        if schema is None:
            return None

        if "type" in schema:
            expected_type = schema["type"]
            if expected_type == "object" and not isinstance(data, dict):
                return f"期望 object，得到 {type(data).__name__}"
            if expected_type == "array" and not isinstance(data, list):
                return f"期望 array，得到 {type(data).__name__}"
            if expected_type == "string" and not isinstance(data, str):
                return f"期望 string，得到 {type(data).__name__}"

        if "required" in schema and isinstance(data, dict):
            missing = [k for k in schema["required"] if k not in data]
            if missing:
                return f"缺少必填字段: {', '.join(missing)}"

        return None
