"""MCP 管理器：批量连接、生命周期、健康检查与失败隔离。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from seacode.config import MCPServerConfig
from seacode.mcp.client import MCPClient
from seacode.mcp.tool_wrapper import MCPToolWrapper
from seacode.tools import ToolRegistry
from seacode.tools.base import Tool

logger = logging.getLogger(__name__)


@dataclass
class ServerInfo:
    """单个 MCP 服务器的连接信息，包含名称与 instructions。"""

    name: str
    instructions: str = ""


@dataclass
class ConnectResult:
    """connect_all 的返回结果：已注册工具、服务器信息与错误列表。"""

    tools: list[Tool] = field(default_factory=list)
    servers: list[ServerInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class MCPManager:
    """管理多个 MCP 服务器的配置加载、批量连接、延迟获取与统一关闭。

    单 Server 连接失败不阻断其它 Server：异常转为字符串收集到
    ConnectResult.errors，调用方据此展示脱敏错误。
    """

    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}

    # 加载 MCP 服务器配置；同名后入者覆盖前者，与两层配置合并语义一致。
    def load_configs(self, configs: list[MCPServerConfig]) -> None:
        for cfg in configs:
            self._configs[cfg.name] = cfg

    # 逐个连接已加载的 MCP 服务器，返回工具列表、服务器信息与错误。
    # 单 Server 失败收集到 errors 不抛异常，继续尝试下一个。
    async def connect_all(self) -> ConnectResult:
        result = ConnectResult()
        for name, config in self._configs.items():
            try:
                client = MCPClient(config)
                await client.connect()
                self._clients[name] = client

                # 从 InitializeResult 提取 instructions，供系统提示注入。
                info = ServerInfo(name=name, instructions=client.instructions)
                result.servers.append(info)

                tools = await client.list_tools()
                for tool_def in tools:
                    wrapper = MCPToolWrapper(name, tool_def, client)
                    result.tools.append(wrapper)
                    logger.info("Registered MCP tool: %s", wrapper.name)
            except Exception as e:
                # 失败隔离：错误脱敏为字符串后收集，不阻断其它 Server。
                msg = f"MCP server '{name}': {e}"
                logger.warning(msg)
                result.errors.append(msg)

        return result

    # 连接所有服务器并把工具注册到 registry，返回 ConnectResult。
    async def register_all_tools(self, registry: ToolRegistry) -> ConnectResult:
        result = await self.connect_all()
        for tool in result.tools:
            registry.register(tool)
        return result

    # 按名获取 client：缓存命中且存活直接返回；未命中且有配置则现场创建；
    # 缓存中但不存活则关闭旧实例并重建新实例（避免旧 AsyncExitStack 状态混淆）。
    async def get_client(self, name: str) -> MCPClient | None:
        client = self._clients.get(name)
        if client is None:
            config = self._configs.get(name)
            if config is None:
                return None
            client = MCPClient(config)
            await client.connect()
            self._clients[name] = client
            return client

        if not client.is_alive:
            logger.info("Reconnecting MCP server '%s'", name)
            await client.close()
            client = MCPClient(self._configs[name])
            await client.connect()
            self._clients[name] = client

        return client

    # 优雅关闭所有 client；单个关闭失败只记 debug 日志不抛异常，最后清空字典。
    async def shutdown(self) -> None:
        for name, client in self._clients.items():
            try:
                await client.close()
                logger.info("MCP server '%s' closed", name)
            except Exception:
                logger.debug(
                    "Error closing MCP server '%s'", name, exc_info=True
                )
        self._clients.clear()
