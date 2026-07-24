"""MCP 单连接客户端：封装 stdio 与 Streamable HTTP 两种传输与协议握手。"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from seacode.config import MCPServerConfig, build_child_env, resolve_env_vars

logger = logging.getLogger(__name__)


class MCPClient:
    """单个 MCP 服务器的连接管理：建立、握手、列举/调用工具、关闭。

    内部用 AsyncExitStack 统一管理子进程、HTTP client 与 MCP 会话的资源释放；
    上层只依赖 connect / list_tools / call_tool / close / is_alive 接口，
    不直接接触 MCP SDK 类型。
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._alive = False
        # 保存 InitializeResult，用于提取 instructions 等服务器元信息。
        self._init_result: types.InitializeResult | None = None

    # 返回连接是否存活；wrapper 据此决定是否触发重连。
    @property
    def is_alive(self) -> bool:
        return self._alive

    # 从 InitializeResult 提取 instructions；缺失时返回空串。
    @property
    def instructions(self) -> str:
        if self._init_result is not None and self._init_result.instructions:
            return self._init_result.instructions
        return ""

    # 建立连接并完成 MCP 握手；已存活时直接返回，异常时清理资源并上抛。
    async def connect(self) -> None:
        if self._alive:
            return

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        try:
            if self.config.is_stdio:
                read, write = await self._connect_stdio()
            else:
                read, write = await self._connect_http()

            session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            # initialize 完成 MCP 协议握手，返回服务器元信息。
            self._init_result = await session.initialize()
            self._session = session
            self._alive = True
            logger.info("MCP server '%s' connected", self.name)
        except Exception:
            # 握手失败时释放已压栈资源，避免句柄泄露。
            await self._cleanup_stack()
            raise

    # stdio 传输：启动子进程并通过 stdin/stdout 通信，stderr 重定向 devnull 防缓冲阻塞。
    async def _connect_stdio(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.command is not None

        params = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=build_child_env(self.config.env),
        )
        # stderr 重定向到 devnull，防止子进程日志填满管道缓冲区导致阻塞。
        devnull = open(os.devnull, "w")
        self._stack.callback(devnull.close)
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=devnull)
        )
        return read, write

    # Streamable HTTP 传输：用 httpx.AsyncClient 承载自定义 Headers 与重定向。
    async def _connect_http(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.url is not None

        # Headers 中的 ${VAR} 在连接时一次性展开；不做 OAuth 令牌刷新。
        resolved_headers = {
            k: resolve_env_vars(v) for k, v in self.config.headers.items()
        }
        http_client = httpx.AsyncClient(
            headers=resolved_headers,
            follow_redirects=True,
        )
        await self._stack.enter_async_context(http_client)

        result = await self._stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=http_client)
        )
        # streamable_http_client 返回 (read, write, _) 三元组，取前两个。
        read, write = result[0], result[1]
        return read, write

    # 列举服务器提供的工具定义；未连接时断言失败。
    async def list_tools(self) -> list[types.Tool]:
        assert self._session is not None
        result = await self._session.list_tools()
        return list(result.tools)

    # 调用指定工具；参数由调用方按 inputSchema 构造。
    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        assert self._session is not None
        return await self._session.call_tool(name, arguments)

    # 关闭连接：先置存活标记为 False，再释放资源栈。
    async def close(self) -> None:
        self._alive = False
        self._session = None
        await self._cleanup_stack()

    # 释放 AsyncExitStack；容忍 cancel scope 清理期常见的 RuntimeError。
    async def _cleanup_stack(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug(
                        "Cancel scope cleanup (expected during shutdown): %s", e
                    )
                else:
                    raise
            except Exception:
                logger.debug(
                    "Error closing stack for '%s'", self.name, exc_info=True
                )
            self._stack = None
