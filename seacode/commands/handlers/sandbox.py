"""/sandbox 命令：沙箱模式切换与显示。"""

from __future__ import annotations

import sys

from seacode.commands.registry import Command, CommandContext, CommandType


# /sandbox：无参显示当前模式；on-auto/on/off 切换；Windows 上 on-auto/on 显示不支持。
async def handle_sandbox(ctx: CommandContext) -> None:
    agent = ctx.agent
    sandbox_cfg = getattr(agent, "sandbox_cfg", None)
    if sandbox_cfg is None:
        ctx.ui.add_system_message("沙箱未初始化")
        return

    arg = ctx.args.strip()
    if not arg:
        mode = getattr(sandbox_cfg, "mode", "未知")
        ctx.ui.add_system_message(f"当前沙箱模式：{mode}")
        return

    if arg not in ("on-auto", "on", "off"):
        ctx.ui.add_system_message("用法：/sandbox [on-auto|on|off]")
        return

    if arg in ("on-auto", "on") and sys.platform == "win32":
        ctx.ui.add_system_message("当前系统不支持沙箱")
        return

    setattr(sandbox_cfg, "mode", arg)
    ctx.ui.add_system_message(f"沙箱模式已切换为：{arg}")


# 命令定义：LOCAL 类型。
SANDBOX_COMMAND = Command(
    name="sandbox",
    description="沙箱模式切换",
    type=CommandType.LOCAL,
    handler=handle_sandbox,
    aliases=[],
    usage="/sandbox [on-auto|on|off]",
    arg_prompt="",
)
