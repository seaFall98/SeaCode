from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import types as mcp_types

from seacode.config import MCPServerConfig
from seacode.mcp.client import MCPClient


# 构造 stdio 类型的 MCPServerConfig。
def _stdio_config(name: str = "fs") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command="npx",
        args=("-y", "server"),
        env={"NODE_PATH": "/usr/lib/node"},
    )


# 构造 HTTP 类型的 MCPServerConfig。
def _http_config(name: str = "remote") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        url="https://mcp.example.com/sse",
        headers={"Authorization": "Bearer token"},
    )


# 模拟 ClientSession，记录 initialize / list_tools / call_tool 调用。
class _FakeSession:
    def __init__(
        self,
        tools: list[mcp_types.Tool] | None = None,
        call_result: mcp_types.CallToolResult | None = None,
        instructions: str = "",
    ) -> None:
        self._tools = tools or []
        self._call_result = call_result
        self._instructions = instructions

    async def initialize(self) -> mcp_types.InitializeResult:
        return mcp_types.InitializeResult(
            protocolVersion="1.0",
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="fake", version="1.0"),
            instructions=self._instructions,
        )

    async def list_tools(self) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=self._tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        assert self._call_result is not None
        return self._call_result


# 构造一个 @asynccontextmanager 装饰的传输工厂；用 side_effect 装载后每次调用返回新实例。
def _make_transport_factory(read: Any = None, write: Any = None) -> Any:
    @asynccontextmanager
    async def _cm(*args: Any, **kwargs: Any) -> Any:
        yield (read, write)

    return _cm


# 构造一个 @asynccontextmanager 装饰的会话工厂；用 side_effect 装载后每次调用返回新实例。
def _make_session_factory(session: _FakeSession) -> Any:
    @asynccontextmanager
    async def _cm(*args: Any, **kwargs: Any) -> Any:
        yield session

    return _cm


# 构造一个会抛出异常的 @asynccontextmanager 传输工厂。
def _make_failing_transport_factory(error: Exception) -> Any:
    @asynccontextmanager
    async def _cm(*args: Any, **kwargs: Any) -> Any:
        raise error
        yield None  # pragma: no cover - 不可达

    return _cm


# 记录 stdio transport 的进入与退出任务，验证退出必须回到创建它的 owner task。
# 使用真实 AsyncExitStack 调用路径，避免只测试 close 不抛异常而漏掉子进程清理问题。
class _TaskTrackingTransport:
    def __init__(self) -> None:
        self.entered_task: asyncio.Task[Any] | None = None
        self.exited_task: asyncio.Task[Any] | None = None

    async def __aenter__(self) -> tuple[Any, Any]:
        self.entered_task = asyncio.current_task()
        return (None, None)

    async def __aexit__(self, *args: Any) -> None:
        del args
        self.exited_task = asyncio.current_task()


# ---------------------------------------------------------------------------
# is_stdio / is_http 配置判断
# ---------------------------------------------------------------------------


# 验证 stdio 配置的 is_stdio 属性为 True。
def test_stdio_config_is_stdio_true() -> None:
    assert _stdio_config().is_stdio is True


# 验证 HTTP 配置的 is_stdio 属性为 False。
def test_http_config_is_stdio_false() -> None:
    assert _http_config().is_stdio is False


# ---------------------------------------------------------------------------
# connect / is_alive / instructions
# ---------------------------------------------------------------------------


# 验证 stdio connect 成功后 is_alive=True 并保存 instructions。
@pytest.mark.asyncio
async def test_stdio_connect_sets_alive_and_instructions() -> None:
    config = _stdio_config()
    client = MCPClient(config)
    session = _FakeSession(instructions="Filesystem MCP server")

    with (
        patch("seacode.mcp.client.stdio_client") as mock_stdio,
        patch("seacode.mcp.client.ClientSession") as mock_session_cls,
    ):
        # side_effect 让 mock 被调用时执行工厂，返回 context manager 实例。
        mock_stdio.side_effect = _make_transport_factory()
        mock_session_cls.side_effect = _make_session_factory(session)

        await client.connect()

    assert client.is_alive is True
    assert client.instructions == "Filesystem MCP server"
    await client.close()


# 验证 HTTP connect 成功后 is_alive=True。
@pytest.mark.asyncio
async def test_http_connect_sets_alive() -> None:
    config = _http_config()
    client = MCPClient(config)
    session = _FakeSession()

    with (
        patch("seacode.mcp.client.streamable_http_client") as mock_http,
        patch("seacode.mcp.client.httpx.AsyncClient") as mock_httpx_cls,
        patch("seacode.mcp.client.ClientSession") as mock_session_cls,
    ):
        # streamable_http_client 返回三元组 (read, write, _)。
        @asynccontextmanager
        async def _http_cm(*args: Any, **kwargs: Any) -> Any:
            yield (MagicMock(), MagicMock(), None)

        mock_http.side_effect = _http_cm
        mock_httpx_instance = MagicMock()
        mock_httpx_instance.__aenter__ = AsyncMock(return_value=mock_httpx_instance)
        mock_httpx_instance.__aexit__ = AsyncMock(return_value=None)
        mock_httpx_cls.return_value = mock_httpx_instance
        mock_session_cls.side_effect = _make_session_factory(session)

        await client.connect()

    assert client.is_alive is True
    await client.close()


# 验证已存活的 client 重复 connect 不重复握手。
@pytest.mark.asyncio
async def test_connect_skips_when_already_alive() -> None:
    config = _stdio_config()
    client = MCPClient(config)
    client._alive = True  # noqa: SLF001

    # 不需要 mock，因为 connect 应直接返回。
    await client.connect()
    assert client.is_alive is True


# 验证 connect 失败时清理资源并保持 is_alive=False。
@pytest.mark.asyncio
async def test_connect_failure_cleans_up_and_stays_dead() -> None:
    config = _stdio_config()
    client = MCPClient(config)

    with patch("seacode.mcp.client.stdio_client") as mock_stdio:
        mock_stdio.side_effect = _make_failing_transport_factory(
            ConnectionError("spawn failed")
        )

        with pytest.raises(ConnectionError):
            await client.connect()

    assert client.is_alive is False


# 验证 instructions 属性在未连接时返回空串。
def test_instructions_empty_before_connect() -> None:
    client = MCPClient(_stdio_config())
    assert client.instructions == ""


# ---------------------------------------------------------------------------
# list_tools / call_tool
# ---------------------------------------------------------------------------


# 验证 list_tools 返回 session.list_tools 的工具列表。
@pytest.mark.asyncio
async def test_list_tools_returns_session_tools() -> None:
    config = _stdio_config()
    client = MCPClient(config)
    tools = [mcp_types.Tool(name="read", inputSchema={"type": "object"})]
    session = _FakeSession(tools=tools)

    with (
        patch("seacode.mcp.client.stdio_client") as mock_stdio,
        patch("seacode.mcp.client.ClientSession") as mock_session_cls,
    ):
        mock_stdio.side_effect = _make_transport_factory()
        mock_session_cls.side_effect = _make_session_factory(session)
        await client.connect()

        result = await client.list_tools()
        await client.close()

    assert len(result) == 1
    assert result[0].name == "read"


# 验证 call_tool 转发到 session.call_tool。
@pytest.mark.asyncio
async def test_call_tool_forwards_to_session() -> None:
    config = _stdio_config()
    client = MCPClient(config)
    call_result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="done")],
        isError=False,
    )
    session = _FakeSession(call_result=call_result)

    with (
        patch("seacode.mcp.client.stdio_client") as mock_stdio,
        patch("seacode.mcp.client.ClientSession") as mock_session_cls,
    ):
        mock_stdio.side_effect = _make_transport_factory()
        mock_session_cls.side_effect = _make_session_factory(session)
        await client.connect()

        result = await client.call_tool("search", {"query": "test"})
        await client.close()

    assert result.isError is False
    assert isinstance(result.content[0], mcp_types.TextContent)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


# 验证 close 后 is_alive=False 且 session 被清空。
@pytest.mark.asyncio
async def test_close_sets_alive_false_and_clears_session() -> None:
    config = _stdio_config()
    client = MCPClient(config)
    session = _FakeSession()

    with (
        patch("seacode.mcp.client.stdio_client") as mock_stdio,
        patch("seacode.mcp.client.ClientSession") as mock_session_cls,
    ):
        mock_stdio.side_effect = _make_transport_factory()
        mock_session_cls.side_effect = _make_session_factory(session)
        await client.connect()

        assert client.is_alive is True
        await client.close()

    assert client.is_alive is False
    assert client._session is None  # noqa: SLF001


# 验证 close 在未连接时也能安全调用。
@pytest.mark.asyncio
async def test_close_safe_when_not_connected() -> None:
    client = MCPClient(_stdio_config())
    await client.close()
    assert client.is_alive is False


# 验证跨任务关闭时仍由创建 transport 的 owner task 执行退出。
# 该不变式防止 AnyIO cancel scope 在事件循环关闭阶段才被动清理。
@pytest.mark.asyncio
async def test_close_exits_transport_in_owner_task() -> None:
    client = MCPClient(_stdio_config())
    transport = _TaskTrackingTransport()
    session = _FakeSession()

    with (
        patch("seacode.mcp.client.stdio_client", return_value=transport),
        patch(
            "seacode.mcp.client.ClientSession",
            side_effect=_make_session_factory(session),
        ),
    ):
        await client.connect()
        owner_task = transport.entered_task
        await asyncio.create_task(client.close())

    assert owner_task is not None
    assert transport.exited_task is owner_task


# ---------------------------------------------------------------------------
# _cleanup_stack 容错
# ---------------------------------------------------------------------------


# 验证 _cleanup_stack 吞掉 cancel scope 相关的 RuntimeError，不抛异常。
# 构造一个 __aexit__ 抛 cancel scope 错误的假 stack，断言 _cleanup_stack 静默通过。
@pytest.mark.asyncio
async def test_cleanup_stack_tolerates_cancel_scope_runtime_error() -> None:
    client = MCPClient(_stdio_config())

    async def _raise_cancel_scope(*args: Any) -> None:
        raise RuntimeError("cancel scope stack mismatch")

    class _FakeStack:
        __aexit__ = _raise_cancel_scope

    client._stack = _FakeStack()  # type: ignore[assignment]  # noqa: SLF001

    # 不应抛异常；cancel scope 错误被视为关闭期的预期噪音。
    await client._cleanup_stack()  # noqa: SLF001
    assert client._stack is None  # noqa: SLF001


# 验证 _cleanup_stack 对其它异常只记 debug 日志不抛出，最终清空 stack。
@pytest.mark.asyncio
async def test_cleanup_stack_tolerates_other_exceptions() -> None:
    client = MCPClient(_stdio_config())

    async def _raise_other(*args: Any) -> None:
        raise ValueError("unexpected cleanup error")

    class _FakeStack:
        __aexit__ = _raise_other

    client._stack = _FakeStack()  # type: ignore[assignment]  # noqa: SLF001

    await client._cleanup_stack()  # noqa: SLF001
    assert client._stack is None  # noqa: SLF001
