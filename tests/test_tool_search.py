from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from seacode.tools import ToolRegistry
from seacode.tools.base import Tool, ToolCategory, ToolResult
from seacode.tools.tool_search import ToolSearchParams, ToolSearchTool


# 可配置名称、描述与 should_defer 的测试工具，用于隔离延迟工具搜索行为。
class _Params(BaseModel):
    value: str = ""


class _DeferredTool(Tool):
    params_model = _Params
    category = ToolCategory.READ

    def __init__(
        self,
        name: str,
        description: str,
        defer: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.should_defer = defer

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(content=f"executed {self.name}")


# 构造含三个延迟工具与一个非延迟工具的注册中心，供搜索测试使用。
def _registry_with_deferred() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        _DeferredTool("mcp_fs_read", "Read a file from the filesystem server")
    )
    registry.register(
        _DeferredTool("mcp_fs_write", "Write a file to the filesystem server")
    )
    registry.register(
        _DeferredTool("mcp_git_commit", "Create a git commit via the git server")
    )
    # 非延迟工具应永远可见且不进入 ToolSearch 结果。
    registry.register(_DeferredTool("ReadFile", "Built-in read", defer=False))
    return registry


# 验证 ToolSearchTool 自身不延迟注册，永远出现在 Schema 列表中。
def test_tool_search_itself_is_not_deferred() -> None:
    tool = ToolSearchTool(registry=ToolRegistry(), protocol="anthropic")
    assert tool.should_defer is False
    assert tool.name == "ToolSearch"
    assert tool.category == ToolCategory.READ


# 验证 ToolSearchTool 的 Schema 不含 Pydantic 默认 title 字段。
def test_tool_search_schema_drops_title() -> None:
    tool = ToolSearchTool(registry=ToolRegistry(), protocol="anthropic")
    schema = tool.get_schema()
    assert schema["name"] == "ToolSearch"
    assert "title" not in schema["input_schema"]


# 验证 select: 前缀按名精确加载延迟工具 Schema，并标记为已发现。
# 命中后下一轮 get_all_schemas 应包含完整 Schema。
@pytest.mark.asyncio
async def test_select_prefix_loads_named_deferred_tools() -> None:
    registry = _registry_with_deferred()
    tool = ToolSearchTool(registry=registry, protocol="anthropic")

    result = await tool.execute(
        ToolSearchParams(query="select:mcp_fs_read,mcp_git_commit")
    )

    assert "Found 2 tool(s)" in result.content
    assert not result.is_error
    # 标记为已发现后应出现在 get_all_schemas 中。
    schemas = {s["name"] for s in registry.get_all_schemas("anthropic")}
    assert "mcp_fs_read" in schemas
    assert "mcp_git_commit" in schemas
    # 未 select 的延迟工具仍未发现。
    assert "mcp_fs_write" not in schemas


# 验证 select: 前缀忽略未注册名与非延迟工具名，只返回合法命中。
@pytest.mark.asyncio
async def test_select_prefix_ignores_unknown_and_non_deferred_names() -> None:
    registry = _registry_with_deferred()
    tool = ToolSearchTool(registry=registry, protocol="anthropic")

    # 包含未知名、非延迟工具名与一个合法延迟工具名。
    result = await tool.execute(
        ToolSearchParams(query="select:NonExistent,ReadFile,mcp_fs_read")
    )

    assert "Found 1 tool(s)" in result.content
    assert registry.is_discovered("mcp_fs_read")


# 验证关键词搜索按评分排序返回，名称整串匹配得分最高。
@pytest.mark.asyncio
async def test_keyword_search_ranks_by_score() -> None:
    registry = _registry_with_deferred()
    tool = ToolSearchTool(registry=registry, protocol="anthropic")

    # "mcp_fs" 同时出现在两个工具名中，应都命中。
    result = await tool.execute(ToolSearchParams(query="mcp_fs", max_results=5))

    payload = json.loads(result.content.split("\n\n", 1)[1])
    names = [s["name"] for s in payload]
    assert "mcp_fs_read" in names
    assert "mcp_fs_write" in names
    # git 相关工具不应出现在 fs 搜索结果中。
    assert "mcp_git_commit" not in names


# 验证关键词搜索遵守 max_results 上限。
@pytest.mark.asyncio
async def test_keyword_search_respects_max_results() -> None:
    registry = _registry_with_deferred()
    tool = ToolSearchTool(registry=registry, protocol="anthropic")

    result = await tool.execute(ToolSearchParams(query="mcp", max_results=1))

    payload = json.loads(result.content.split("\n\n", 1)[1])
    assert len(payload) == 1


# 验证无命中时返回可用延迟工具名列表，辅助模型下一步选择。
@pytest.mark.asyncio
async def test_no_match_returns_available_deferred_names() -> None:
    registry = _registry_with_deferred()
    tool = ToolSearchTool(registry=registry, protocol="anthropic")

    result = await tool.execute(ToolSearchParams(query="nonexistent_query"))

    assert "No matching deferred tools" in result.content
    assert "mcp_fs_read" in result.content
    assert "mcp_fs_write" in result.content
    assert "mcp_git_commit" in result.content
    # 非延迟工具不应出现在可用列表中。
    assert "ReadFile" not in result.content


# 验证 mark_discovered 后该工具不再出现在 get_deferred_tool_names 中。
@pytest.mark.asyncio
async def test_mark_discovered_removes_from_deferred_list() -> None:
    registry = _registry_with_deferred()
    tool = ToolSearchTool(registry=registry, protocol="anthropic")

    assert "mcp_fs_read" in registry.get_deferred_tool_names()
    await tool.execute(ToolSearchParams(query="select:mcp_fs_read"))
    assert "mcp_fs_read" not in registry.get_deferred_tool_names()


# 验证 OpenAI 协议下 ToolSearch 返回的 Schema 用 parameters 键而非 input_schema。
@pytest.mark.asyncio
async def test_select_returns_openai_schema_format() -> None:
    registry = _registry_with_deferred()
    tool = ToolSearchTool(registry=registry, protocol="openai")

    result = await tool.execute(ToolSearchParams(query="select:mcp_fs_read"))

    payload = json.loads(result.content.split("\n\n", 1)[1])
    assert payload[0]["type"] == "function"
    assert "parameters" in payload[0]


# 验证 ToolSearchParams 默认 max_results 为 5。
def test_tool_search_params_defaults() -> None:
    params = ToolSearchParams(query="test")
    assert params.max_results == 5
