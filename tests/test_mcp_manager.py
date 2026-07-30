from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from mcp import types as mcp_types

from seacode.config import MCPServerConfig
from seacode.mcp.client import MCPClient
from seacode.mcp.manager import MCPManager, ServerInfo
from seacode.tools import ToolRegistry


# 构造 stdio 类型的 MCPServerConfig，便于隔离测试配置加载。
def _stdio_config(name: str = "fs") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem"),
    )


# 构造 HTTP 类型的 MCPServerConfig。
def _http_config(name: str = "remote") -> MCPServerConfig:
    return MCPServerConfig(name=name, url="https://mcp.example.com/sse")


# 构造一个 mcp_types.Tool 实例用于测试。
def _tool_def(name: str = "search") -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description="Search files",
        inputSchema={"type": "object", "properties": {}},
    )


# 受 MCPClient 契约约束的测试 client，只替代底层连接传输。
class _FakeClient(MCPClient):
    def __init__(
        self,
        tools: list[mcp_types.Tool] | None = None,
        instructions: str = "",
        connect_error: BaseException | None = None,
        close_error: Exception | None = None,
    ) -> None:
        super().__init__(_stdio_config("fake"))
        self._tools = tools or []
        self._instructions = instructions
        self._connect_error = connect_error
        self._close_error = close_error
        self._alive = True
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return self._alive

    @is_alive.setter
    def is_alive(self, value: bool) -> None:
        self._alive = value

    @property
    def instructions(self) -> str:
        return self._instructions

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.is_alive = True

    async def list_tools(self) -> list[mcp_types.Tool]:
        return self._tools

    async def close(self) -> None:
        self.closed = True
        self.is_alive = False
        if self._close_error is not None:
            raise self._close_error


# ---------------------------------------------------------------------------
# load_configs
# ---------------------------------------------------------------------------


# 验证 load_configs 按 name 覆盖同名配置，与两层配置合并语义一致。
def test_load_configs_overrides_same_name() -> None:
    manager = MCPManager()
    first = _stdio_config("fs")
    second = _http_config("fs")

    manager.load_configs([first, second])

    assert len(manager._configs) == 1
    assert manager._configs["fs"].url == "https://mcp.example.com/sse"


# 验证 load_configs 追加不同 name 的配置。
def test_load_configs_appends_different_names() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs"), _http_config("remote")])

    assert set(manager._configs.keys()) == {"fs", "remote"}


# ---------------------------------------------------------------------------
# connect_all
# ---------------------------------------------------------------------------


# 验证 connect_all 成功连接所有服务器并返回工具与服务器信息。
@pytest.mark.asyncio
async def test_connect_all_success_returns_tools_and_servers() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs"), _http_config("remote")])

    fake_fs = _FakeClient(
        tools=[_tool_def("read"), _tool_def("write")],
        instructions="Filesystem server",
    )
    fake_remote = _FakeClient(tools=[_tool_def("query")])

    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.side_effect = [fake_fs, fake_remote]
        result = await manager.connect_all()

    assert len(result.servers) == 2
    assert len(result.tools) == 3
    assert result.errors == []
    server_names = {s.name for s in result.servers}
    assert server_names == {"fs", "remote"}
    fs_info = next(s for s in result.servers if s.name == "fs")
    assert fs_info.instructions == "Filesystem server"


# 验证 connect_all 单服务器失败不阻断其它服务器，错误收集到 errors。
@pytest.mark.asyncio
async def test_connect_all_isolates_failure() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("broken"), _stdio_config("ok")])

    fake_broken = _FakeClient(connect_error=ConnectionError("refused"))
    fake_ok = _FakeClient(tools=[_tool_def("search")])

    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.side_effect = [fake_broken, fake_ok]
        result = await manager.connect_all()

    assert len(result.errors) == 1
    assert "broken" in result.errors[0]
    assert len(result.servers) == 1
    assert result.servers[0].name == "ok"
    assert len(result.tools) == 1


# 验证单个 MCP 内部 CancelledError 被视为该 Server 的连接失败并继续后续 Server。
# 用未处于取消状态的当前任务模拟生命周期内部取消，断言错误收集且正常 Server 仍注册。
@pytest.mark.asyncio
async def test_connect_all_isolates_internal_cancelled_error() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("cancelled"), _stdio_config("ok")])

    fake_cancelled = _FakeClient(connect_error=asyncio.CancelledError())
    fake_ok = _FakeClient(tools=[_tool_def("search")])

    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.side_effect = [fake_cancelled, fake_ok]
        result = await manager.connect_all()

    assert len(result.errors) == 1
    assert "cancelled" in result.errors[0]
    assert "CancelledError" in result.errors[0]
    assert fake_cancelled.closed is True
    assert [server.name for server in result.servers] == ["ok"]
    assert len(result.tools) == 1


# 验证用户取消整个 connect_all 时 CancelledError 仍向上传播，不被失败隔离吞掉。
# 阻塞第一个 Server 的 connect，取消外层任务后断言抛出 CancelledError 且连接被清理。
@pytest.mark.asyncio
async def test_connect_all_propagates_external_cancellation() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("blocked")])
    started = asyncio.Event()
    blocker = asyncio.Event()

    class _BlockingClient(_FakeClient):
        async def connect(self) -> None:
            started.set()
            await blocker.wait()

    fake_blocked = _BlockingClient()
    with patch("seacode.mcp.manager.MCPClient", return_value=fake_blocked):
        task = asyncio.create_task(manager.connect_all())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert fake_blocked.closed is True


# 验证 connect_all 无配置时返回空结果。
@pytest.mark.asyncio
async def test_connect_all_no_configs_returns_empty() -> None:
    manager = MCPManager()
    result = await manager.connect_all()

    assert result.servers == []
    assert result.tools == []
    assert result.errors == []


# ---------------------------------------------------------------------------
# register_all_tools
# ---------------------------------------------------------------------------


# 验证 register_all_tools 把工具注册到 registry。
@pytest.mark.asyncio
async def test_register_all_tools_registers_to_registry() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs")])

    fake_fs = _FakeClient(tools=[_tool_def("read"), _tool_def("write")])
    registry = ToolRegistry()

    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.return_value = fake_fs
        result = await manager.register_all_tools(registry)

    assert len(result.tools) == 2
    assert registry.get("mcp__fs__read") is not None
    assert registry.get("mcp__fs__write") is not None
    servers = manager.list_servers()
    assert [(s.name, s.tool_count, s.status) for s in servers] == [
        ("fs", 2, "connected")
    ]


# 验证连接失败的配置仍能通过公开状态接口呈现错误状态。
# 使用真实 manager 完成注册流程，确保 /mcp 不依赖测试替身才能工作。
@pytest.mark.asyncio
async def test_list_servers_reports_connection_failure() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("broken")])
    fake_broken = _FakeClient(connect_error=ConnectionError("refused"))
    registry = ToolRegistry()

    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.return_value = fake_broken
        await manager.register_all_tools(registry)

    servers = manager.list_servers()
    assert [(s.name, s.tool_count, s.status) for s in servers] == [
        ("broken", 0, "error")
    ]


# 验证同一会话内重复注册会复用首次结果，不会再次创建客户端。
# 连续调用两次后断言客户端构造和工具注册都只发生一次。
@pytest.mark.asyncio
async def test_register_all_tools_reuses_initialized_result() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs")])
    registry = ToolRegistry()
    fake_fs = _FakeClient(tools=[_tool_def("read")])

    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.return_value = fake_fs
        first = await manager.register_all_tools(registry)
        second = await manager.register_all_tools(registry)

    assert first is second
    assert manager.is_initialized is True
    assert mock_client_cls.call_count == 1
    assert registry.get("mcp__fs__read") is not None


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------


# 验证 get_client 缓存命中且存活时直接返回已有 client。
@pytest.mark.asyncio
async def test_get_client_returns_cached_alive_client() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs")])

    fake_fs = _FakeClient()
    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.return_value = fake_fs
        await manager.connect_all()
        client = await manager.get_client("fs")

    assert client is fake_fs


# 验证 get_client 未命中但有配置时现场创建并连接。
@pytest.mark.asyncio
async def test_get_client_creates_on_demand_when_config_exists() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs")])

    fake_fs = _FakeClient()
    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.return_value = fake_fs
        client = await manager.get_client("fs")

    assert client is fake_fs
    assert fake_fs.is_alive is True


# 验证 get_client 无配置时返回 None。
@pytest.mark.asyncio
async def test_get_client_returns_none_when_no_config() -> None:
    manager = MCPManager()
    client = await manager.get_client("nonexistent")
    assert client is None


# 验证 get_client 缓存中但不存活时关闭旧实例并重建。
@pytest.mark.asyncio
async def test_get_client_reconnects_when_cached_client_dead() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs")])

    old_client = _FakeClient()
    new_client = _FakeClient()
    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.side_effect = [old_client, new_client]
        await manager.connect_all()
        # 标记旧 client 已断开。
        old_client.is_alive = False
        client = await manager.get_client("fs")

    assert client is new_client
    assert old_client.closed is True


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


# 验证 shutdown 关闭所有 client 并清空字典。
@pytest.mark.asyncio
async def test_shutdown_closes_all_clients() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs"), _http_config("remote")])

    fake_fs = _FakeClient()
    fake_remote = _FakeClient()
    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.side_effect = [fake_fs, fake_remote]
        await manager.connect_all()
        await manager.shutdown()

    assert fake_fs.closed is True
    assert fake_remote.closed is True
    assert manager._clients == {}


# 验证 shutdown 单个关闭失败不阻断其它，最终仍清空字典。
@pytest.mark.asyncio
async def test_shutdown_tolerates_close_failure() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("fs")])

    fake_fs = _FakeClient(close_error=RuntimeError("close failed"))
    with patch("seacode.mcp.manager.MCPClient") as mock_client_cls:
        mock_client_cls.return_value = fake_fs
        await manager.connect_all()
        await manager.shutdown()

    assert manager._clients == {}


# ---------------------------------------------------------------------------
# ServerInfo
# ---------------------------------------------------------------------------


# 验证 ServerInfo 默认 instructions、工具数和连接状态稳定。
def test_server_info_defaults() -> None:
    info = ServerInfo(name="fs")
    assert info.name == "fs"
    assert info.instructions == ""
    assert info.tool_count == 0
    assert info.status == "connected"


# 验证 MCPManager 在首次连接前仍能返回已配置服务器状态。
# 使用真实 manager 读取配置，避免只依赖带有 list_servers 的测试替身。
def test_list_servers_reports_configured_before_connection() -> None:
    manager = MCPManager()
    manager.load_configs([_stdio_config("codegraph")])

    servers = manager.list_servers()

    assert len(servers) == 1
    assert servers[0].name == "codegraph"
    assert servers[0].tool_count == 0
    assert servers[0].status == "configured"
