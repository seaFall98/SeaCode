"""MCP 工具包装器：把外部工具适配为 SeaCode Tool 接口。"""

from __future__ import annotations

from typing import Any

from mcp import types as mcp_types
from pydantic import BaseModel, create_model

from seacode.mcp.client import MCPClient
from seacode.tools.base import Tool, ToolCategory, ToolResult


# 根据 inputSchema 动态构造 Pydantic 参数模型；必填字段用 Ellipsis，可选字段允许 None。
def _build_params_model(
    tool_name: str, input_schema: dict[str, Any]
) -> type[BaseModel]:
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    field_definitions: dict[str, Any] = {}
    for name, prop in properties.items():
        py_type = _json_type_to_python(prop.get("type", "string"))
        if name in required:
            field_definitions[name] = (py_type, ...)
        else:
            field_definitions[name] = (py_type | None, None)

    return create_model(f"{tool_name}Params", **field_definitions)


# JSON Schema 类型映射到 Python 类型；未知类型回退为 str 保证可透传。
def _json_type_to_python(json_type: str) -> type:
    mapping: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    return mapping.get(json_type, str)


# 把 MCP CallToolResult 的多态 content 列表提取为文本；空结果返回 (no output)。
def _extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        elif isinstance(block, mcp_types.ImageContent):
            parts.append(f"[image: {block.mimeType}]")
        elif isinstance(block, mcp_types.EmbeddedResource):
            resource = block.resource
            if hasattr(resource, "text"):
                parts.append(resource.text)
            else:
                parts.append(f"[binary resource: {resource.uri}]")
    return "\n".join(parts) if parts else "(no output)"


class MCPToolWrapper(Tool):
    """把单个 MCP 工具适配为 SeaCode Tool 接口。

    - name 采用 mcp__{server}__{tool} 命名空间隔离，与 AgentTeam 工具筛选契约一致；
    - category 设为 command 让第 05 步权限链对外部工具同样适用；
    - should_defer=True 让初始 Schema 跳过，模型经 ToolSearch 发现后再纳入；
    - execute 调用前检查 is_alive，断开则重连，调用异常置 _alive=False 触发下次重连。
    """

    def __init__(
        self,
        server_name: str,
        tool_def: mcp_types.Tool,
        client: MCPClient,
    ) -> None:
        self._server_name = server_name
        self._tool_def = tool_def
        self._client = client
        self.name = f"mcp__{server_name}__{tool_def.name}"
        self.description = tool_def.description or tool_def.name
        self.category = ToolCategory.SYSTEM
        self.is_concurrency_safe = False
        self.should_defer = True
        self.params_model = _build_params_model(
            tool_def.name, tool_def.inputSchema
        )

    # 返回原始 MCP 工具名；execute 调用 call_tool 时用此名而非前缀名。
    @property
    def mcp_tool_name(self) -> str:
        return self._tool_def.name

    # 透传 MCP inputSchema 作为工具 Schema；不在 SeaCode 侧重新构造。
    def get_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._tool_def.inputSchema,
        }

    # 执行外部工具：调用前保活重连，调用异常置 _alive=False 返回错误结果不中断回合。
    async def execute(self, params: BaseModel) -> ToolResult:
        if not self._client.is_alive:
            try:
                await self._client.connect()
            except Exception as e:
                return ToolResult(
                    content=f"MCP server '{self._server_name}' reconnect failed: {e}",
                    is_error=True,
                )

        try:
            # call_tool 用原始工具名，前缀名仅用于 SeaCode 内部路由。
            result = await self._client.call_tool(
                self._tool_def.name, params.model_dump(exclude_none=True)
            )
        except Exception as e:
            # 调用异常时显式置 _alive=False，下次调用触发重连。
            self._client._alive = False  # noqa: SLF001
            return ToolResult(
                content=f"MCP tool call failed: {e}",
                is_error=True,
            )

        text = _extract_text(result.content)
        return ToolResult(content=text, is_error=bool(result.isError))
