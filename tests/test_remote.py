"""远程浏览器服务的 HTTP、WebSocket 和 Agent 事件回归。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Request

from seacode.agent import (
    LoopComplete,
    PermissionResponse,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
)
from seacode.commands.registry import Command, CommandContext, CommandType
from seacode.mcp.manager import MCPManager
from seacode.remote import RemoteServer
from seacode.web_content import INDEX_HTML


class _Browser:
    """记录服务广播的内存浏览器连接。"""

    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_send = fail_send

    async def send(self, payload: str) -> None:
        if self.fail_send:
            raise RuntimeError("disconnected")
        self.sent.append(json.loads(payload))


class _Agent:
    """提供确定事件序列的最小 Agent。"""

    total_input_tokens = 12
    total_output_tokens = 7

    async def run(self, _conversation: Any) -> AsyncIterator[Any]:
        yield StreamText("hello")
        yield ToolUseEvent("ReadFile", "tool-1", {"path": "README.md"})
        yield ToolResultEvent("tool-1", "ReadFile", "contents", False, 0.2)
        yield LoopComplete(total_turns=1)

    def clear_active_skills(self) -> None:
        return None


# 验证远程服务也能在退出时释放 MCP manager，避免只修复 TUI 生命周期。
# 使用受 MCPManager 契约约束的测试 manager 锁定关闭调用和引用清理。
class _ShutdownMCPManager(MCPManager):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        await super().shutdown()


# 验证根路径返回 SeaCode 页面，非 WebSocket 的其它路径返回 404。
# 直接调用 HTTP 回调，避免绑定真实端口造成测试并发冲突。
def test_http_routes_return_page_and_not_found() -> None:
    server = RemoteServer([])

    root = server._handle_http_request(None, Request("/", Headers()))  # type: ignore[arg-type]
    missing = server._handle_http_request(None, Request("/missing", Headers()))  # type: ignore[arg-type]
    websocket = server._handle_http_request(None, Request("/ws", Headers()))  # type: ignore[arg-type]

    assert root is not None
    assert root.status_code == 200
    assert root.body == INDEX_HTML.encode("utf-8")
    assert missing is not None
    assert missing.status_code == 404
    assert websocket is None


# 验证页面使用 SeaCode 名称并保留可滚动的命令候选菜单。
# 检查关键结构字符串，避免浏览器资产退化成无法选择的静态页面。
def test_remote_page_contains_brand_and_command_scrolling() -> None:
    assert "SeaCode Remote" in INDEX_HTML
    assert "scrollIntoView" in INDEX_HTML
    assert "permission_response" in INDEX_HTML


# 验证远程服务关闭 MCP manager 后清空引用，下一次生命周期不会复用旧连接。
# 直接调用关闭边界，避免测试依赖真实 MCP 子进程或监听端口。
@pytest.mark.asyncio
async def test_remote_shutdown_closes_mcp_manager() -> None:
    server = RemoteServer([])
    manager = _ShutdownMCPManager()
    server.mcp_manager = manager

    await server._shutdown_mcp()

    assert manager.shutdown_calls == 1
    assert server.mcp_manager is None


# 验证真实监听端口同时提供 HTTP 页面、404 与 WebSocket 握手。
# 使用操作系统分配的临时端口，避免测试依赖固定 18888 或外部浏览器。
@pytest.mark.asyncio
async def test_live_server_serves_http_and_websocket() -> None:
    server = RemoteServer([])
    async with websockets.serve(
        server._ws_handler,
        "127.0.0.1",
        0,
        process_request=server._handle_http_request,
    ) as listener:
        socket = listener.sockets[0]
        port = socket.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient() as client:
            root = await client.get(base_url + "/")
            missing = await client.get(base_url + "/missing")
        assert root.status_code == 200
        assert "SeaCode Remote" in root.text
        assert missing.status_code == 404

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as browser:
            connected = json.loads(await browser.recv())
            commands = json.loads(await browser.recv())
        assert connected["type"] == "connected"
        assert commands["type"] == "commands"


# 验证 Agent 事件按远程协议广播，循环完成后恢复可发送状态。
# 用内存浏览器和假 Agent 覆盖正文、工具、结果与结束四类事件的顺序。
@pytest.mark.asyncio
async def test_user_message_broadcasts_agent_events() -> None:
    server = RemoteServer([])
    browser = _Browser()
    server._connections.add(browser)  # type: ignore[arg-type]
    server.agent = _Agent()  # type: ignore[assignment]
    from seacode.conversation import ConversationManager

    server.conversation = ConversationManager()

    await server._handle_user_message("inspect the project")

    assert [message["type"] for message in browser.sent] == [
        "stream_text",
        "tool_use",
        "stream_end",
        "tool_result",
        "loop_complete",
    ]
    assert browser.sent[1]["data"]["toolName"] == "ReadFile"
    assert server._streaming is False


# 验证浏览器三种权限选择能回填同一个等待中的 Future。
# 每项使用新 Future，确保映射不依赖上一次选择的残留状态。
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wire_value", "expected"),
    [
        ("allow", PermissionResponse.ALLOW),
        ("deny", PermissionResponse.DENY),
        ("allowAlways", PermissionResponse.ALLOW_ALWAYS),
    ],
)
async def test_permission_response_resolves_pending_future(
    wire_value: str, expected: PermissionResponse
) -> None:
    server = RemoteServer([])
    future: asyncio.Future[PermissionResponse] = asyncio.get_running_loop().create_future()
    server._pending_permissions["permission-1"] = future

    server._resolve_permission({"id": "permission-1", "response": wire_value})

    assert await future is expected
    assert "permission-1" not in server._pending_permissions


# 验证发送失败的浏览器连接会被广播循环移除。
# 另一个可用连接仍会收到消息，保证单客户端断开不影响服务。
@pytest.mark.asyncio
async def test_broadcast_removes_failed_connection() -> None:
    server = RemoteServer([])
    healthy = _Browser()
    failed = _Browser(fail_send=True)
    server._connections.update({healthy, failed})  # type: ignore[arg-type]

    await server._broadcast({"type": "system", "data": {"message": "ready"}})

    assert healthy.sent == [{"type": "system", "data": {"message": "ready"}}]
    assert failed not in server._connections


# 验证终端专用 UI 命令在远程模式给出提示并结束命令回合。
# 直接分发 /plan，防止该路径因没有网页审批组件而卡住。
@pytest.mark.asyncio
async def test_remote_terminal_ui_command_reports_not_supported() -> None:
    server = RemoteServer([])
    browser = _Browser()
    server._connections.add(browser)  # type: ignore[arg-type]

    await server._handle_command("/plan")

    assert browser.sent[-1]["type"] == "command_done"
    assert "not fully supported" in browser.sent[-2]["data"]["message"]


# 验证远程分发器不会因 argument hint 拦截无参数命令。
# 测试与 TUI 共用同一 Command 契约，确保两条入口都能进入 handler。
@pytest.mark.asyncio
async def test_remote_command_with_argument_hint_allows_empty_args() -> None:
    server = RemoteServer([])
    browser = _Browser()
    server._connections.add(browser)  # type: ignore[arg-type]
    called: list[str] = []

    async def handler(ctx: CommandContext) -> None:
        called.append(ctx.args)

    server.command_registry.register_sync(
        Command(
            name="optional",
            description="optional arguments",
            type=CommandType.LOCAL,
            handler=handler,
            usage="/optional [value]",
            arg_prompt="value",
        )
    )

    await server._handle_command("/optional")

    assert called == [""]
    assert browser.sent[-1]["type"] == "command_done"


# 验证取消信号会结束当前循环并向网页发送结束事件。
# 假 Agent 在首段输出后设置取消标记，覆盖取消后的 UI 恢复路径。
@pytest.mark.asyncio
async def test_cancelled_loop_broadcasts_completion() -> None:
    server = RemoteServer([])
    browser = _Browser()
    server._connections.add(browser)  # type: ignore[arg-type]

    class _CancellingAgent(_Agent):
        async def run(self, _conversation: Any) -> AsyncIterator[Any]:
            yield StreamText("partial")
            assert server._cancel_event is not None
            server._cancel_event.set()
            yield LoopComplete(total_turns=1)

    from seacode.conversation import ConversationManager

    server.agent = _CancellingAgent()  # type: ignore[assignment]
    server.conversation = ConversationManager()
    await server._handle_user_message("cancel this")

    assert browser.sent[-1]["type"] == "loop_complete"
    assert server._streaming is False
