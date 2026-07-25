"""/sandbox 命令：沙箱模式切换与显示。

提供三种模式切换：
  1. on-auto  开启 OS 沙箱 + 自动放行（推荐）
  2. on       开启 OS 沙箱 + 常规权限
  3. off      关闭 OS 沙箱
"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /sandbox：无参显示当前状态；on-auto/on 创建并挂载沙箱；off 卸载沙箱。
async def handle_sandbox(ctx: CommandContext) -> None:
    if ctx.agent is None:
        ctx.ui.add_system_message("Agent 未初始化")
        return

    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else ""

    if sub == "":
        _show_status(ctx)
    elif sub in ("1", "on-auto"):
        _enable_sandbox(ctx, auto_allow=True)
    elif sub in ("2", "on"):
        _enable_sandbox(ctx, auto_allow=False)
    elif sub in ("3", "off"):
        _disable_sandbox(ctx)
    else:
        ctx.ui.add_system_message(
            "用法：/sandbox [1|on-auto | 2|on | 3|off]\n"
            "\n"
            "模式：\n"
            "  1 (on-auto)  开启沙箱 + 自动放行（推荐）\n"
            "  2 (on)       开启沙箱 + 常规权限\n"
            "  3 (off)      关闭沙箱\n"
            "\n"
            "无参数时显示当前状态。"
        )


# 显示当前沙箱状态：OS 沙箱是否启用、自动放行、后端类型与可用性。
def _show_status(ctx: CommandContext) -> None:
    checker = ctx.agent.permission_checker
    sandbox_on = checker.sandbox_enabled if checker else False

    # Bash 工具上挂载的沙箱实例与可用性。
    bash_tool = ctx.agent.registry.get("Bash")
    os_sandbox = getattr(bash_tool, "sandbox", None) if bash_tool else None
    os_available = os_sandbox.available() if os_sandbox else False

    lines = [
        "沙箱状态",
        "─────────",
        f"  OS 沙箱：{'已启用' if sandbox_on else '未启用'}",
        f"  自动放行：{'是' if sandbox_on else '否'}",
        f"  沙箱后端：{type(os_sandbox).__name__ if os_sandbox else '无'}",
        f"  后端可用：{'是' if os_available else '否'}",
    ]
    ctx.ui.add_system_message("\n".join(lines))


# 启用 OS 沙箱：创建或复用沙箱实例、构建配置、挂载到 Bash 工具、设置 checker 标志。
def _enable_sandbox(ctx: CommandContext, auto_allow: bool) -> None:
    from seacode.sandbox import SandboxConfig, create_sandbox

    bash_tool = ctx.agent.registry.get("Bash")
    if bash_tool is None:
        ctx.ui.add_system_message("错误：未找到 Bash 工具")
        return

    # 复用已挂载的沙箱实例；不存在时按平台创建（macOS Seatbelt / Linux bwrap）。
    sandbox = getattr(bash_tool, "sandbox", None)
    if sandbox is None:
        sandbox = create_sandbox()
        if sandbox is None:
            ctx.ui.add_system_message("错误：当前系统不支持沙箱（仅支持 macOS / Linux）")
            return

    if not sandbox.available():
        backend = type(sandbox).__name__
        ctx.ui.add_system_message(f"错误：沙箱后端 {backend} 不可用，请安装对应工具")
        return

    # 构建沙箱配置：项目目录和 /tmp 可写，敏感配置文件强制只读以防被改写。
    work_dir = ctx.agent.work_dir
    config = SandboxConfig(
        allow_write=[work_dir, "/tmp"],
        deny_write=[
            f"{work_dir}/.seacode/config.yaml",
            f"{work_dir}/.seacode/permissions.local.yaml",
        ],
        network_enabled=False,
    )

    # 挂载到 Bash 工具；checker.sandbox_enabled 控制是否触发 Layer 1c 自动放行。
    bash_tool.sandbox = sandbox
    bash_tool.sandbox_config = config

    checker = ctx.agent.permission_checker
    if checker:
        checker.sandbox_enabled = auto_allow

    mode_desc = "自动放行" if auto_allow else "常规权限"
    ctx.ui.add_system_message(f"沙箱已启用（{mode_desc}）")
    ctx.ui.refresh_status()


# 禁用 OS 沙箱：从 Bash 工具卸载沙箱、清除 checker 标志。
def _disable_sandbox(ctx: CommandContext) -> None:
    bash_tool = ctx.agent.registry.get("Bash")
    if bash_tool:
        bash_tool.sandbox = None
        bash_tool.sandbox_config = None

    checker = ctx.agent.permission_checker
    if checker:
        checker.sandbox_enabled = False

    ctx.ui.add_system_message("沙箱已关闭")
    ctx.ui.refresh_status()


# 命令定义：LOCAL 类型。
SANDBOX_COMMAND = Command(
    name="sandbox",
    description="沙箱管理",
    type=CommandType.LOCAL,
    handler=handle_sandbox,
    aliases=[],
    usage="/sandbox [1|on-auto | 2|on | 3|off]",
    arg_prompt="",
)
