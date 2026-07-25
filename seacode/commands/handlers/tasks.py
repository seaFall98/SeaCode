"""/tasks 命令：后台任务管理（list / info / cancel）。

handler 通过闭包持有 TaskManager；按 ctx.args 解析子命令并通过 ctx.ui.add_system_message
输出结果。``_format_status`` 用图标表示四种状态；``_format_elapsed`` 格式化耗时。
"""

from __future__ import annotations

import time
from typing import Any

from seacode.agents.task_manager import TaskManager
from seacode.commands.registry import Command, CommandContext, CommandType

# 任务详情结果预览字符上限。
_TASK_RESULT_PREVIEW_LIMIT: int = 2000


# 状态图标映射；未知状态用 ? 兜底。
def _format_status(status: str) -> str:
    return {
        "running": "⏳",
        "completed": "✓",
        "failed": "✗",
        "cancelled": "⊘",
    }.get(status, "?")


# 耗时格式化；>= 60 秒显示 X.Xm，否则显示 X.Xs。
def _format_elapsed(seconds: float) -> str:
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.1f}s"


# 构造 /tasks handler；闭包捕获 task_manager。
def create_tasks_handler(task_manager: TaskManager) -> Any:
    async def handler(ctx: CommandContext) -> None:
        args_str = ctx.args.strip()
        args = args_str.split() if args_str else []

        if not args:
            # /tasks：列出所有后台任务。
            tasks = task_manager.list_tasks()
            if not tasks:
                ctx.ui.add_system_message("没有后台任务")
                return
            now = time.time()
            lines = []
            for t in tasks:
                elapsed = (t.end_time or now) - t.start_time
                lines.append(
                    f"[{t.id}] {t.name} {_format_status(t.status)} "
                    f"{_format_elapsed(elapsed)}"
                )
            ctx.ui.add_system_message("\n".join(lines))
            return

        sub = args[0]

        if sub == "info":
            # /tasks info <id>：显示详情与结果预览（截断 2000 字符）。
            if len(args) < 2:
                ctx.ui.add_system_message("用法: /tasks info <id>")
                return
            task_id = args[1]
            task = task_manager.get(task_id)
            if task is None:
                ctx.ui.add_system_message(f"未找到任务: {task_id}")
                return
            now = time.time()
            elapsed = (task.end_time or now) - task.start_time
            result_preview = task.result[:_TASK_RESULT_PREVIEW_LIMIT]
            ctx.ui.add_system_message(
                f"任务 {task.id}\n"
                f"名称: {task.name}\n"
                f"状态: {task.status}\n"
                f"耗时: {_format_elapsed(elapsed)}\n"
                f"结果:\n{result_preview}"
            )
            return

        if sub == "cancel":
            # /tasks cancel <id>：取消后台任务。
            if len(args) < 2:
                ctx.ui.add_system_message("用法: /tasks cancel <id>")
                return
            task_id = args[1]
            success = await task_manager.cancel(task_id)
            if success:
                ctx.ui.add_system_message(f"任务 {task_id} 已取消")
            else:
                ctx.ui.add_system_message(
                    f"任务 {task_id} 取消失败（不存在或已完成）"
                )
            return

        ctx.ui.add_system_message(
            f"未知子命令: {sub}，可用: info / cancel"
        )

    return handler


# 构造 /tasks 命令定义；别名 task。
def create_tasks_command(task_manager: TaskManager) -> Command:
    return Command(
        name="tasks",
        description="后台任务管理 (list/info/cancel)",
        type=CommandType.LOCAL,
        handler=create_tasks_handler(task_manager),
        aliases=["task"],
        usage="/tasks [info <id> | cancel <id>]",
        arg_prompt="子命令",
    )
