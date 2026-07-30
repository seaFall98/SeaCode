from __future__ import annotations

from typing import Any

import pytest
from mcp import types as mcp_types
from pydantic import BaseModel

from seacode.config import MCPServerConfig
from seacode.mcp.client import MCPClient
from seacode.mcp.tool_wrapper import (
    MCPToolWrapper,
    _build_params_model,
    _extract_text,
)
from seacode.tools.base import ToolCategory


# 构造一个 mcp_types.Tool 实例，封装 inputSchema 与可选 description。
def _make_tool_def(
    name: str = "search",
    description: str | None = "Search files",
    input_schema: dict[str, Any] | None = None,
) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=description,
        inputSchema=input_schema or {"type": "object", "properties": {}},
    )


# 受 MCPClient 契约约束的测试 client，只替代连接和调用的外部传输行为。
class _FakeClient(MCPClient):
    def __init__(
        self,
        call_result: mcp_types.CallToolResult | None = None,
        call_error: Exception | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        super().__init__(MCPServerConfig(name="fake", command="fake-mcp"))
        self._alive = True
        self._call_result = call_result
        self._call_error = call_error
        self._connect_error = connect_error
        self.connect_calls = 0

    @property
    def is_alive(self) -> bool:
        return self._alive

    @is_alive.setter
    def is_alive(self, value: bool) -> None:
        self._alive = value

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_error is not None:
            raise self._connect_error
        self._alive = True

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        if self._call_error is not None:
            raise self._call_error
        assert self._call_result is not None
        return self._call_result


# ---------------------------------------------------------------------------
# _build_params_model
# ---------------------------------------------------------------------------


# 验证空 inputSchema 生成无字段的参数模型。
def test_build_params_model_empty_schema() -> None:
    model = _build_params_model("empty", {"type": "object", "properties": {}})
    instance = model()
    assert isinstance(instance, BaseModel)


# 验证必填字段用 Ellipsis 标记，缺失时校验失败。
def test_build_params_model_required_field() -> None:
    model = _build_params_model(
        "req",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    with pytest.raises(Exception):
        model()
    instance = model(path="/tmp")
    assert instance.path == "/tmp"  # type: ignore[attr-defined]


# 验证可选字段允许 None，构造时可不传。
def test_build_params_model_optional_field() -> None:
    model = _build_params_model(
        "opt",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    )
    instance = model()
    assert instance.limit is None  # type: ignore[attr-defined]
    instance2 = model(limit=10)
    assert instance2.limit == 10  # type: ignore[attr-defined]


# 验证 JSON Schema 类型映射到 Python 类型；未知类型回退为 str。
@pytest.mark.parametrize(
    ("json_type", "py_value"),
    [
        ("string", "hello"),
        ("integer", 42),
        ("number", 3.14),
        ("boolean", True),
    ],
)
def test_build_params_model_type_mapping(json_type: str, py_value: Any) -> None:
    model = _build_params_model(
        "typed",
        {
            "type": "object",
            "properties": {"field": {"type": json_type}},
            "required": ["field"],
        },
    )
    instance = model(field=py_value)
    assert getattr(instance, "field") == py_value


# 验证未知 JSON 类型回退为 str。
def test_build_params_model_unknown_type_falls_back_to_str() -> None:
    model = _build_params_model(
        "unk",
        {
            "type": "object",
            "properties": {"field": {"type": "custom_type"}},
            "required": ["field"],
        },
    )
    instance = model(field="fallback")
    assert isinstance(instance.field, str)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


# 验证 TextContent 提取 text 字段。
def test_extract_text_from_text_content() -> None:
    blocks = [mcp_types.TextContent(type="text", text="hello world")]
    assert _extract_text(blocks) == "hello world"


# 验证多个 TextContent 以换行拼接。
def test_extract_text_joins_multiple_blocks() -> None:
    blocks = [
        mcp_types.TextContent(type="text", text="line1"),
        mcp_types.TextContent(type="text", text="line2"),
    ]
    assert _extract_text(blocks) == "line1\nline2"


# 验证 ImageContent 提取为占位描述。
def test_extract_text_from_image_content() -> None:
    blocks = [
        mcp_types.ImageContent(type="image", data="base64data", mimeType="image/png")
    ]
    result = _extract_text(blocks)
    assert "[image: image/png]" in result


# 验证空 content 列表返回 (no output)。
def test_extract_text_empty_list_returns_no_output() -> None:
    assert _extract_text([]) == "(no output)"


# ---------------------------------------------------------------------------
# MCPToolWrapper 初始化与 Schema
# ---------------------------------------------------------------------------


# 验证 wrapper 使用 AgentTeam 统一的 mcp__{server}__{tool} 名称，
# 分类为 SYSTEM 且 should_defer=True。
def test_wrapper_name_prefixing_and_category() -> None:
    tool_def = _make_tool_def(name="search", description="Search files")
    wrapper = MCPToolWrapper("fs", tool_def, _FakeClient())

    assert wrapper.name == "mcp__fs__search"
    assert wrapper.description == "Search files"
    assert wrapper.category == ToolCategory.SYSTEM
    assert wrapper.should_defer is True
    assert wrapper.is_concurrency_safe is False
    assert wrapper.mcp_tool_name == "search"


# 验证 description 缺失时回退为工具名。
def test_wrapper_description_falls_back_to_tool_name() -> None:
    tool_def = mcp_types.Tool(
        name="nodesc",
        description=None,
        inputSchema={"type": "object", "properties": {}},
    )
    wrapper = MCPToolWrapper("srv", tool_def, _FakeClient())

    assert wrapper.description == "nodesc"


# 验证 get_schema 透传原始 inputSchema 内容，不重新构造字段结构。
def test_wrapper_get_schema_passes_through_input_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    tool_def = _make_tool_def(name="search", input_schema=schema)
    wrapper = MCPToolWrapper("fs", tool_def, _FakeClient())

    result = wrapper.get_schema()
    assert result["name"] == "mcp__fs__search"
    # Pydantic 模型可能复制 dict，比较内容而非引用身份。
    assert result["input_schema"] == schema


# ---------------------------------------------------------------------------
# MCPToolWrapper.execute
# ---------------------------------------------------------------------------


# 验证 execute 成功调用 call_tool 并提取文本。
@pytest.mark.asyncio
async def test_execute_success_extracts_text() -> None:
    call_result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="result text")],
        isError=False,
    )
    client = _FakeClient(call_result=call_result)
    wrapper = MCPToolWrapper("fs", _make_tool_def(), client)

    params = wrapper.params_model()
    result = await wrapper.execute(params)

    assert result.content == "result text"
    assert result.is_error is False


# 验证 execute 在 call_tool 返回 isError=True 时标记错误。
@pytest.mark.asyncio
async def test_execute_propagates_is_error_flag() -> None:
    call_result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="boom")],
        isError=True,
    )
    client = _FakeClient(call_result=call_result)
    wrapper = MCPToolWrapper("fs", _make_tool_def(), client)

    result = await wrapper.execute(wrapper.params_model())

    assert result.is_error is True
    assert "boom" in result.content


# 验证 client 已断开时 execute 触发重连后继续调用。
@pytest.mark.asyncio
async def test_execute_reconnects_when_client_dead() -> None:
    call_result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")],
        isError=False,
    )
    client = _FakeClient(call_result=call_result)
    client.is_alive = False
    wrapper = MCPToolWrapper("fs", _make_tool_def(), client)

    result = await wrapper.execute(wrapper.params_model())

    assert client.connect_calls == 1
    assert result.content == "ok"


# 验证重连失败时返回错误结果而不抛异常。
@pytest.mark.asyncio
async def test_execute_reconnect_failure_returns_error_result() -> None:
    client = _FakeClient(connect_error=ConnectionError("refused"))
    client.is_alive = False
    wrapper = MCPToolWrapper("fs", _make_tool_def(), client)

    result = await wrapper.execute(wrapper.params_model())

    assert result.is_error is True
    assert "reconnect failed" in result.content


# 验证 call_tool 抛异常时置 _alive=False 并返回错误结果，不中断回合。
@pytest.mark.asyncio
async def test_execute_call_failure_sets_alive_false_and_returns_error() -> None:
    client = _FakeClient(call_error=RuntimeError("server crashed"))
    wrapper = MCPToolWrapper("fs", _make_tool_def(), client)

    assert client.is_alive is True
    result = await wrapper.execute(wrapper.params_model())

    assert result.is_error is True
    assert "MCP tool call failed" in result.content
    assert client.is_alive is False
