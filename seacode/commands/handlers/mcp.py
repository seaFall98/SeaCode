"""/mcp 命令：显示 MCP 服务器与工具状态。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /mcp：从应用命令上下文取 MCP 管理器，列出每台服务器的工具数与连接状态。
async def handle_mcp(ctx: CommandContext) -> None:
    manager = ctx.mcp_manager
    if manager is None:
        # 保留旧上下文调用方的兼容路径；正式 TUI/远程入口使用 context 字段。
        manager = getattr(ctx.agent, "mcp_manager", None)
    if manager is None:
        ctx.ui.add_system_message("未配置 MCP 服务器")
        return
    servers = manager.list_servers()
    if not servers:
        ctx.ui.add_system_message("无连接的 MCP 服务器")
        return
    lines = ["MCP 服务器状态："]
    status_labels = {
        "configured": "已配置，尚未连接",
        "connected": "已连接",
        "error": "连接失败",
    }
    for server in servers:
        name = getattr(server, "name", "未知")
        tool_count = getattr(server, "tool_count", 0)
        raw_status = getattr(server, "status", "未知")
        status = status_labels.get(raw_status, raw_status)
        lines.append(f"  {name}：{tool_count} 个工具，状态：{status}")
    ctx.ui.add_system_message("\n".join(lines))


# 命令定义：LOCAL 类型。
MCP_COMMAND = Command(
    name="mcp",
    description="显示 MCP 服务器与工具状态",
    type=CommandType.LOCAL,
    handler=handle_mcp,
    aliases=[],
    usage="/mcp",
    arg_prompt="",
)
