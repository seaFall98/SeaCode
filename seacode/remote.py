"""远程浏览器服务：用 WebSocket 将 SeaCode 运行事件连接到单页界面。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.http11 import Request, Response

from seacode.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from seacode.client import create_client
from seacode.commands import CommandContext, CommandRegistry, CommandType, parse_command
from seacode.commands.handlers import register_all_commands
from seacode.config import MCPServerConfig, ProviderConfig
from seacode.conversation import ConversationManager
from seacode.hooks import HookEngine
from seacode.mcp import MCPManager
from seacode.memory import MemoryManager, Session, SessionManager, load_instructions
from seacode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from seacode.skills.loader import SkillLoader
from seacode.tools import ToolRegistry, create_default_registry
from seacode.tools.load_skill import LoadSkill
from seacode.tools.tool_search import ToolSearchTool
from seacode.web_content import INDEX_HTML

log = logging.getLogger(__name__)


class RemoteServer:
    """提供根页面和 WebSocket，并桥接一套 SeaCode Agent 运行时。"""

    def __init__(
        self,
        providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
        mcp_servers: tuple[MCPServerConfig, ...] | list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        addr: str = "0.0.0.0",
        port: int = 18888,
    ) -> None:
        self.providers = list(providers)
        self._mcp_server_configs = list(mcp_servers or [])
        self.hook_engine = hook_engine
        self.addr = addr
        self.port = port

        self._connections: set[ServerConnection] = set()
        self.agent: Agent | None = None
        self.conversation: ConversationManager | None = None
        self.registry: ToolRegistry | None = None
        self.session_id = ""
        self._streaming = False
        self._cancel_event: asyncio.Event | None = None
        self._pending_permissions: dict[str, asyncio.Future[PermissionResponse]] = {}

        self.command_registry = CommandRegistry()
        register_all_commands(self.command_registry)
        self.mcp_manager: MCPManager | None = None
        self._mcp_instructions = ""
        self.skill_loader: SkillLoader | None = None
        self.memory_manager: MemoryManager | None = None
        self.session_manager: SessionManager | None = None
        self.session: Session | None = None
        self._permission_checker: PermissionChecker | None = None
        self._pre_plan_mode = PermissionMode.DEFAULT

    async def run(self) -> None:
        """初始化运行时并开始提供 HTTP 与 WebSocket 服务。"""
        self._init_agent()
        try:
            await self._init_mcp()
            print(f"\n  SeaCode Remote: http://localhost:{self.port}\n")
            async with websockets.serve(
                self._ws_handler,
                self.addr,
                self.port,
                process_request=self._handle_http_request,
                max_size=4 * 1024 * 1024,
            ):
                await asyncio.Future()
        finally:
            await self._shutdown_mcp()

    def _handle_http_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        del connection
        if request.path == "/":
            return Response(
                200,
                "OK",
                websockets.Headers({"Content-Type": "text/html; charset=utf-8"}),
                INDEX_HTML.encode("utf-8"),
            )
        if request.path == "/ws":
            return None
        return Response(404, "Not Found", websockets.Headers(), b"404 Not Found")

    async def _ws_handler(self, websocket: ServerConnection) -> None:
        self._connections.add(websocket)
        try:
            await self._broadcast(
                {
                    "type": "connected",
                    "data": {"session": self.session_id, "cwd": os.getcwd()},
                }
            )
            await self._broadcast(
                {"type": "commands", "data": self._build_command_list()}
            )
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                message_type = message.get("type", "")
                data = message.get("data", {})
                if message_type == "user_message":
                    content = str(data.get("content", "")).strip()
                    if content:
                        asyncio.create_task(self._handle_user_message(content))
                elif message_type == "permission_response":
                    self._resolve_permission(data)
                elif message_type == "cancel" and self._cancel_event is not None:
                    self._cancel_event.set()
                elif message_type == "ping":
                    await self._broadcast({"type": "pong", "data": None})
        except websockets.ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)

    def _init_agent(self) -> None:
        if not self.providers:
            raise RuntimeError("no provider configured")
        provider = self.providers[0]
        work_dir = os.getcwd()
        home = Path.home()
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(work_dir),
            rule_engine=RuleEngine(
                user_rules_path=home / ".seacode" / "permissions.yaml",
                project_rules_path=Path(work_dir) / ".seacode" / "permissions.yaml",
                local_rules_path=Path(work_dir) / ".seacode" / "permissions.local.yaml",
            ),
            mode=PermissionMode.DEFAULT,
        )
        self._permission_checker = checker
        self.memory_manager = MemoryManager(work_dir)
        self.session_manager = SessionManager(work_dir)
        self.session_manager.cleanup()
        self.session = self.session_manager.create()
        self.session_id = self.session.session_id

        self.registry = create_default_registry()
        self.registry.register(ToolSearchTool(self.registry, protocol=provider.protocol))
        self.skill_loader = SkillLoader(
            project_dir=Path(work_dir) / ".seacode" / "skills",
            user_dir=home / ".seacode" / "skills",
        )
        self.skill_loader.load_all()
        load_skill_tool = LoadSkill()
        self.registry.register(load_skill_tool)
        self.agent = Agent(
            client=create_client(provider),
            registry=self.registry,
            protocol=provider.protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=load_instructions(work_dir),
            memory_manager=self.memory_manager,
            hook_engine=self.hook_engine,
        )
        self.agent.session_id = self.session_id
        load_skill_tool.set_loader(self.skill_loader)
        load_skill_tool.set_agent(self.agent)
        catalog = self.skill_loader.get_catalog()
        if catalog:
            lines = ["You can use the following Skills:", ""]
            lines.extend(f"- {name}: {description}" for name, description in catalog)
            lines.extend(
                [
                    "",
                    "If the user's request matches a Skill, call LoadSkill to activate it.",
                ]
            )
            self.agent.set_skill_catalog("\n".join(lines))
        self.conversation = ConversationManager()
        log.info("Remote agent initialized session=%s model=%s", self.session_id, provider.model)

    async def _init_mcp(self) -> None:
        if not self._mcp_server_configs or self.registry is None or self.agent is None:
            return
        manager = MCPManager()
        manager.load_configs(self._mcp_server_configs)
        result = await manager.register_all_tools(self.registry)
        self.mcp_manager = manager
        self.agent.mcp_manager = manager
        self.agent._mcp_connected = bool(getattr(manager, "is_initialized", False))
        for error in result.errors:
            log.warning("MCP error: %s", error)
        if not result.servers:
            return
        sections: list[str] = []
        for server in result.servers:
            section = f"## {server.name}\n"
            if server.instructions:
                section += server.instructions
            else:
                names = [
                    tool.name
                    for tool in self.registry.list_tools()
                    if tool.name.startswith(f"mcp__{server.name}__")
                ]
                if names:
                    section += "Available tools: " + ", ".join(names)
            sections.append(section)
        self._mcp_instructions = (
            "# MCP Server Instructions\n\n"
            "The following MCP servers have provided instructions for how to use "
            "their tools and resources:\n\n"
            + "\n\n".join(sections)
        )

    async def _shutdown_mcp(self) -> None:
        manager = self.mcp_manager
        if manager is None:
            return
        self.mcp_manager = None
        await manager.shutdown()

    async def _handle_user_message(self, content: str) -> None:
        if self._streaming:
            return
        if content.startswith("/"):
            await self._handle_command(content)
            return
        if self.agent is None or self.conversation is None:
            await self._broadcast(
                {"type": "error", "data": {"message": "Remote agent is unavailable"}}
            )
            return
        self._streaming = True
        self.conversation.add_user_message(content)
        if self._mcp_instructions:
            self.conversation.add_system_reminder(self._mcp_instructions)
            self._mcp_instructions = ""
        self._cancel_event = asyncio.Event()
        started = time.monotonic()
        stream_buffer = ""
        finished = False
        total_turns = 0
        try:
            async for event in self.agent.run(self.conversation):
                if self._cancel_event.is_set():
                    break
                if isinstance(event, StreamText):
                    stream_buffer += event.text
                    await self._broadcast({"type": "stream_text", "data": {"text": event.text}})
                elif isinstance(event, ThinkingText):
                    await self._broadcast({"type": "thinking_text", "data": {"text": event.text}})
                elif isinstance(event, ToolUseEvent):
                    await self._broadcast(
                        {
                            "type": "tool_use",
                            "data": {
                                "toolId": event.tool_id,
                                "toolName": event.tool_name,
                                "args": event.arguments,
                            },
                        }
                    )
                elif isinstance(event, ToolResultEvent):
                    stream_buffer = await self._finish_stream(stream_buffer)
                    await self._broadcast(
                        {
                            "type": "tool_result",
                            "data": {
                                "toolId": event.tool_id,
                                "toolName": event.tool_name,
                                "output": event.output,
                                "isError": event.is_error,
                                "elapsed": event.elapsed,
                            },
                        }
                    )
                elif isinstance(event, PermissionRequest):
                    permission_id = f"perm_{time.time_ns()}"
                    self._pending_permissions[permission_id] = event.future
                    await self._broadcast(
                        {
                            "type": "permission_request",
                            "data": {
                                "id": permission_id,
                                "toolName": event.tool_name,
                                "description": event.description,
                            },
                        }
                    )
                elif isinstance(event, TurnComplete):
                    stream_buffer = await self._finish_stream(stream_buffer)
                    await self._broadcast({"type": "turn_complete", "data": {"turn": event.turn}})
                elif isinstance(event, LoopComplete):
                    stream_buffer = await self._finish_stream(stream_buffer)
                    finished = True
                    total_turns = event.total_turns
                    await self._broadcast(
                        {
                            "type": "loop_complete",
                            "data": {
                                "totalTurns": event.total_turns,
                                "elapsed": time.monotonic() - started,
                            },
                        }
                    )
                elif isinstance(event, UsageEvent):
                    await self._broadcast(
                        {
                            "type": "usage",
                            "data": {
                                "inputTokens": event.input_tokens,
                                "outputTokens": event.output_tokens,
                            },
                        }
                    )
                elif isinstance(event, ErrorEvent):
                    await self._broadcast({"type": "error", "data": {"message": event.message}})
                elif isinstance(event, CompactNotification):
                    await self._broadcast({"type": "compact", "data": {"message": event.message}})
                elif isinstance(event, RetryEvent):
                    await self._broadcast(
                        {
                            "type": "retry",
                            "data": {"reason": event.reason, "waitMs": int(event.wait * 1000)},
                        }
                    )
                elif isinstance(event, HookEvent):
                    status = "ok" if event.success else "error"
                    await self._broadcast(
                        {
                            "type": "system",
                            "data": {"message": f"Hook [{event.hook_id}] {status}: {event.output}"},
                        }
                    )
        except asyncio.CancelledError:
            await self._broadcast(
                {"type": "error", "data": {"message": "Operation cancelled"}}
            )
        except Exception as error:
            log.exception("Remote agent run failed")
            await self._broadcast({"type": "error", "data": {"message": str(error)}})
        finally:
            if self._cancel_event is not None and self._cancel_event.is_set() and not finished:
                await self._finish_stream(stream_buffer)
                await self._broadcast(
                    {
                        "type": "loop_complete",
                        "data": {
                            "totalTurns": total_turns,
                            "elapsed": time.monotonic() - started,
                        },
                    }
                )
            self._streaming = False
            self._cancel_event = None

    async def _finish_stream(self, text: str) -> str:
        if text:
            await self._broadcast({"type": "stream_end", "data": {"text": text}})
        return ""

    async def _handle_command(self, input_text: str) -> None:
        name, args, is_command = parse_command(input_text)
        if not is_command or not name:
            return
        command = self.command_registry.find(name)
        if command is None:
            await self._broadcast(
                {
                    "type": "error",
                    "data": {
                        "message": (
                            f"Unknown command: /{name} - type /help to see available commands"
                        )
                    },
                }
            )
            await self._command_done()
            return
        if not args and command.arg_prompt:
            await self._broadcast({"type": "system", "data": {"message": command.arg_prompt}})
            await self._command_done()
            return
        if command.type == CommandType.LOCAL:
            try:
                await command.handler(self._build_command_context(args))
            except Exception as error:
                await self._broadcast(
                    {"type": "error", "data": {"message": f"Command error: {error}"}}
                )
            await self._command_done()
            return
        if command.type == CommandType.LOCAL_UI:
            if name == "clear":
                self.conversation = ConversationManager()
                if self.agent is not None:
                    self.agent.clear_active_skills()
                await self._broadcast({"type": "clear", "data": None})
            elif name == "compact":
                await self._handle_compact()
                return
            else:
                await self._broadcast(
                    {
                        "type": "system",
                        "data": {"message": f"/{name} is not fully supported in remote mode."},
                    }
                )
            await self._command_done()
            return
        try:
            await command.handler(self._build_command_context(args))
        except Exception as error:
            await self._broadcast(
                {"type": "error", "data": {"message": f"Command error: {error}"}}
            )
            await self._command_done()

    async def _handle_compact(self) -> None:
        if self.agent is None or self.conversation is None:
            await self._broadcast(
                {"type": "error", "data": {"message": "Compact requires an active agent."}}
            )
            await self._command_done()
            return
        await self._broadcast({"type": "system", "data": {"message": "Compacting conversation..."}})
        result = await self.agent.manual_compact(self.conversation)
        if isinstance(result, CompactNotification):
            await self._broadcast({"type": "system", "data": {"message": result.message}})
        elif isinstance(result, ErrorEvent):
            await self._broadcast({"type": "error", "data": {"message": result.message}})
        await self._command_done()

    def _build_command_context(self, args: str) -> CommandContext:
        return CommandContext(
            args=args,
            agent=self.agent,
            conversation=self.conversation,
            session=self.session,
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            ui=self,
            permission_checker=self._permission_checker,
            mcp_manager=self.mcp_manager,
            config={
                "registry": self.command_registry,
                "set_session": self._set_session,
                "set_conversation": self._set_conversation,
                "clear_chat": self._clear_chat,
                "render_restored": self._render_restored,
            },
        )

    async def _render_restored(self, messages: list[Any]) -> None:
        for message in messages:
            if getattr(message, "role", "") == "user":
                await self._broadcast({"type": "replay_user", "data": {"content": message.content}})
            elif getattr(message, "role", "") == "assistant":
                await self._broadcast(
                    {"type": "replay_assistant", "data": {"content": message.content}}
                )

    def add_system_message(self, text: str) -> None:
        asyncio.ensure_future(self._broadcast({"type": "system", "data": {"message": text}}))

    def send_user_message(self, text: str) -> None:
        asyncio.create_task(self._handle_user_message(text))

    def set_plan_mode(self, enabled: bool) -> None:
        self.set_permission_mode(PermissionMode.PLAN if enabled else self._pre_plan_mode)

    def set_permission_mode(self, mode: PermissionMode) -> None:
        if mode == PermissionMode.PLAN and self._permission_checker is not None:
            self._pre_plan_mode = self._permission_checker.mode
        if self._permission_checker is not None:
            self._permission_checker.mode = mode
        if self.agent is not None:
            self.agent.set_permission_mode(mode)

    def get_token_count(self) -> tuple[int, int]:
        used = getattr(self.conversation, "estimated_tokens", 0)
        limit = self.providers[0].get_context_window() if self.providers else 0
        return used, limit

    def refresh_status(self) -> None:
        return None

    def _set_session(self, session: Session) -> None:
        self.session = session
        self.session_id = session.session_id
        if self.agent is not None:
            self.agent.session_id = self.session_id

    def _set_conversation(self, conversation: ConversationManager) -> None:
        self.conversation = conversation

    def _clear_chat(self) -> None:
        asyncio.ensure_future(self._broadcast({"type": "clear", "data": None}))

    def _resolve_permission(self, data: dict[str, Any]) -> None:
        permission_id = str(data.get("id", ""))
        future = self._pending_permissions.pop(permission_id, None)
        if future is None or future.done():
            return
        mapping = {
            "allow": PermissionResponse.ALLOW,
            "deny": PermissionResponse.DENY,
            "allowAlways": PermissionResponse.ALLOW_ALWAYS,
        }
        future.set_result(mapping.get(str(data.get("response", "deny")), PermissionResponse.DENY))

    def _build_command_list(self) -> list[dict[str, str]]:
        return [
            {"name": command.name, "description": command.description}
            for command in self.command_registry.list_commands()
        ]

    async def _command_done(self) -> None:
        await self._broadcast({"type": "command_done", "data": None})

    async def _broadcast(self, message: dict[str, Any]) -> None:
        if not self._connections:
            return
        payload = json.dumps(message, ensure_ascii=False)
        closed: list[ServerConnection] = []
        for websocket in list(self._connections):
            try:
                await websocket.send(payload)
            except websockets.ConnectionClosed:
                closed.append(websocket)
            except Exception:
                closed.append(websocket)
        for websocket in closed:
            self._connections.discard(websocket)
