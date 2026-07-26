"""/status 命令：显示当前会话、模型、Token、工具与记忆状态。"""

from __future__ import annotations

import os

from seacode.commands.registry import Command, CommandContext, CommandType


# /status：聚合 agent / session / memory / 工具 / 工作目录 / 版本信息输出到聊天区。
async def handle_status(ctx: CommandContext) -> None:
    used, limit = ctx.ui.get_token_count()
    agent = ctx.agent
    session = ctx.session
    memory_manager = ctx.memory_manager

    model = getattr(agent, "model", "未知")
    plan_mode = getattr(agent, "plan_mode", False)
    # 权限模式：default / acceptEdits / bypassPermissions / plan；agent 为 None 时 unknown。
    if agent is not None:
        permission_mode = getattr(agent, "permission_mode", None)
        mode_str = permission_mode.value if permission_mode is not None else "unknown"
    else:
        mode_str = "unknown"
    session_id = getattr(session, "session_id", "无") if session else "无"
    tool_count = 0
    tool_registry = getattr(agent, "tool_registry", None)
    if tool_registry is not None:
        tool_count = len(tool_registry.list_tools())
    memory_count = 0
    if memory_manager is not None:
        memories = getattr(memory_manager, "memories", None)
        if memories is not None:
            memory_count = len(memories)
    work_dir = os.getcwd()
    version = "0.1.0"

    lines = [
        "SeaCode 当前状态",
        f"  模型：{model}",
        f"  权限模式：{mode_str}",
        f"  Plan 模式：{'是' if plan_mode else '否'}",
        f"  会话 ID：{session_id}",
        f"  Token：{used} / {limit}",
        f"  工具数：{tool_count}",
        f"  记忆数：{memory_count}",
        f"  工作目录：{work_dir}",
        f"  版本：{version}",
    ]
    ctx.ui.add_system_message("\n".join(lines))


# 命令定义：LOCAL 类型，别名 s。
STATUS_COMMAND = Command(
    name="status",
    description="显示当前状态",
    type=CommandType.LOCAL,
    handler=handle_status,
    aliases=["s"],
    usage="/status",
    arg_prompt="",
)
