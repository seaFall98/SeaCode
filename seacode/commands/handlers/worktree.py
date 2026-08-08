"""/worktree 命令：Git worktree 隔离工作区管理（create / list / enter / exit / status）。

handler 通过闭包持有 WorktreeManager；按 ``ctx.args`` 解析子命令并通过
``ctx.ui.add_system_message`` 输出结果。``create`` 与 ``enter`` 成功时把
``ctx.agent.work_dir`` 切换到 worktree 路径；``exit`` 恢复原始工作目录。
"""

from __future__ import annotations

from typing import Any

from seacode.commands.registry import Command, CommandContext, CommandType
from seacode.worktree.manager import WorktreeError, WorktreeManager

# /worktree exit 子命令支持的参数开关。
_EXIT_REMOVE_FLAGS = {"--remove", "-r"}
_EXIT_DISCARD_FLAGS = {"--discard", "-d"}


# 优先走 App 注入的长期 Agent 回调；独立 handler 调用仍兼容直接注入的 Agent。
def _update_agent_work_dir(ctx: CommandContext, work_dir: str) -> None:
    set_work_dir = ctx.config.get("set_work_dir")
    if callable(set_work_dir):
        set_work_dir(work_dir)
    elif ctx.agent is not None:
        ctx.agent.work_dir = work_dir


# 构造 /worktree handler；闭包捕获 worktree_manager。
def create_worktree_handler(manager: WorktreeManager) -> Any:
    async def handler(ctx: CommandContext) -> None:
        args_str = ctx.args.strip()
        parts = args_str.split() if args_str else []

        if not parts:
            ctx.ui.add_system_message(
                "用法: /worktree [create|list|enter|exit|status] ..."
            )
            return

        sub = parts[0]
        rest = parts[1:]

        if sub == "create":
            await _handle_create(ctx, manager, rest)
        elif sub == "list":
            await _handle_list(ctx, manager)
        elif sub == "enter":
            await _handle_enter(ctx, manager, rest)
        elif sub == "exit":
            await _handle_exit(ctx, manager, rest)
        elif sub == "status":
            await _handle_status(ctx, manager)
        else:
            ctx.ui.add_system_message(
                f"未知子命令: {sub}，可用: create / list / enter / exit / status"
            )

    return handler


# /worktree create <name> [base]：创建并进入 worktree，切换 agent.work_dir。
async def _handle_create(
    ctx: CommandContext, manager: WorktreeManager, rest: list[str]
) -> None:
    if not rest:
        ctx.ui.add_system_message("用法: /worktree create <name> [base-branch]")
        return
    name = rest[0]
    base_branch = rest[1] if len(rest) > 1 else "HEAD"
    try:
        wt = await manager.create(name, base_branch)
        session = await manager.enter(name)
        # 切换 agent 工作目录到 worktree 路径，让后续工具调用在隔离环境内执行。
        _update_agent_work_dir(ctx, session.worktree_path)
        ctx.ui.add_system_message(
            f"已创建并进入 worktree: {wt.name} ({wt.path})\n"
            f"分支: {wt.branch}"
        )
    except WorktreeError as e:
        ctx.ui.add_system_message(f"创建失败: {e}")


# /worktree list：列出 active worktrees 并标记当前 session。
async def _handle_list(ctx: CommandContext, manager: WorktreeManager) -> None:
    worktrees = manager.list_worktrees()
    current = manager.get_current_session()
    if not worktrees:
        ctx.ui.add_system_message("当前无 active worktree")
        return
    lines = ["Active worktrees:"]
    for wt in worktrees:
        marker = " (current)" if (
            current and current.worktree_name == wt.name
        ) else ""
        lines.append(f"  {wt.name}{marker} -> {wt.path} [{wt.branch}]")
    ctx.ui.add_system_message("\n".join(lines))


# /worktree enter <name>：进入已存在的 worktree 并切换 work_dir。
async def _handle_enter(
    ctx: CommandContext, manager: WorktreeManager, rest: list[str]
) -> None:
    if not rest:
        ctx.ui.add_system_message("用法: /worktree enter <name>")
        return
    name = rest[0]
    try:
        session = await manager.enter(name)
        _update_agent_work_dir(ctx, session.worktree_path)
        ctx.ui.add_system_message(f"已进入 worktree: {name} ({session.worktree_path})")
    except WorktreeError as e:
        ctx.ui.add_system_message(f"进入失败: {e}")


# /worktree exit [--remove] [--discard]：退出当前 session 并恢复 work_dir。
async def _handle_exit(
    ctx: CommandContext, manager: WorktreeManager, rest: list[str]
) -> None:
    session = manager.get_current_session()
    if session is None:
        ctx.ui.add_system_message("未在 worktree session 中")
        return
    remove = any(flag in _EXIT_REMOVE_FLAGS for flag in rest)
    discard = any(flag in _EXIT_DISCARD_FLAGS for flag in rest)
    try:
        await manager.exit(
            session.worktree_name,
            action="remove" if remove else "keep",
            discard_changes=discard,
        )
        # 恢复 agent 工作目录到进入 worktree 前的原始路径。
        _update_agent_work_dir(ctx, session.original_cwd)
        action_str = "removed" if remove else "kept"
        ctx.ui.add_system_message(f"已退出 worktree ({action_str})")
    except WorktreeError as e:
        ctx.ui.add_system_message(f"退出失败: {e}")


# /worktree status：显示当前 worktree session 状态。
async def _handle_status(ctx: CommandContext, manager: WorktreeManager) -> None:
    session = manager.get_current_session()
    if session is None:
        ctx.ui.add_system_message("未在 worktree session 中")
        return
    lines = [
        "Worktree session 状态:",
        f"  名称: {session.worktree_name}",
        f"  路径: {session.worktree_path}",
        f"  原工作目录: {session.original_cwd}",
        f"  原分支: {session.original_branch}",
        f"  原 HEAD: {session.original_head_commit}",
    ]
    ctx.ui.add_system_message("\n".join(lines))


# 构造 /worktree 命令定义；别名 wt。
def create_worktree_command(manager: WorktreeManager) -> Command:
    return Command(
        name="worktree",
        description="管理 Git worktree 隔离工作区",
        type=CommandType.LOCAL,
        handler=create_worktree_handler(manager),
        aliases=["wt"],
        usage="/worktree [create|list|enter|exit|status] ...",
        arg_prompt="子命令 [参数]",
    )
