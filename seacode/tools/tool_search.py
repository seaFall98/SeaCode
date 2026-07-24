"""延迟工具搜索：模型按需发现并加载 MCP 工具的 Schema。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from seacode.tools.base import Tool, ToolCategory, ToolResult

# 仅用于类型注解，避免运行时与 tools/__init__.py 形成循环导入。
if TYPE_CHECKING:
    from seacode.tools import ToolRegistry


class ToolSearchParams(BaseModel):
    """ToolSearch 入参：query 支持 select: 前缀精确查找与关键词评分搜索。"""

    query: str
    max_results: int = 5


class ToolSearchTool(Tool):
    """延迟工具搜索入口；自身 should_defer=False 永远可见。

    - query 以 "select:" 开头时按逗号分隔精确加载指定工具；
    - 否则走评分搜索（名称整串 10、描述整串 5、单词匹配名称 3、单词匹配描述 1）；
    - 命中后 mark_discovered，下一轮 get_all_schemas 包含完整 Schema；
    - 未命中时返回可用延迟工具名列表辅助模型。
    """

    name = "ToolSearch"
    description = (
        "Search for and load additional tools that are not immediately available. "
        "Use query 'select:<name>[,<name>...]' to load specific tools by name, "
        "or provide keywords to search by relevance."
    )
    params_model = ToolSearchParams
    category = ToolCategory.READ
    should_defer = False

    def __init__(
        self,
        registry: ToolRegistry,
        protocol: str = "anthropic",
    ) -> None:
        self._registry = registry
        self._protocol = protocol

    # 生成 ToolSearch 自身的 Schema；移除 Pydantic 默认 title 字段。
    def get_schema(self) -> dict[str, Any]:
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    # 执行搜索：select: 走精确查找，否则评分搜索；命中后 mark_discovered。
    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, ToolSearchParams)
        query = params.query
        max_results = params.max_results

        if query.startswith("select:"):
            # select: 后按逗号分隔工具名，去空格后精确查找。
            names = [n.strip() for n in query[7:].split(",")]
            schemas = self._registry.find_deferred_by_names(names, self._protocol)
        else:
            schemas = self._registry.search_deferred(
                query, max_results, self._protocol
            )

        if not schemas:
            # 未命中时返回可用延迟工具名，辅助模型下一步选择。
            deferred_names = self._registry.get_deferred_tool_names()
            return ToolResult(
                content=(
                    f'No matching deferred tools for "{query}". '
                    f'Available: {", ".join(deferred_names)}'
                )
            )

        # 命中后标记为已发现，下一轮 Schema 列表包含完整定义。
        for s in schemas:
            if "name" in s:
                self._registry.mark_discovered(s["name"])

        return ToolResult(
            content=(
                f"Found {len(schemas)} tool(s). Their full schemas are now loaded:\n\n"
                f"{json.dumps(schemas, indent=2, ensure_ascii=False)}"
            )
        )
