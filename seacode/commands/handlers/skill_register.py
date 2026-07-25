"""自动注册每个 Skill 为 /<skill-name> 斜杠命令。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from seacode.commands.registry import Command, CommandContext, CommandRegistry, CommandType
from seacode.skills.executor import SkillExecutor
from seacode.skills.loader import SkillLoader

logger = logging.getLogger(__name__)

# 模块级集合跟踪已注册 Skill 命令名；reload 时先清理再重注册。
_REGISTERED_SKILL_NAMES: set[str] = set()


# 清理旧 Skill 命令；reload 时调用避免重复注册。
def _clear_registered(registry: CommandRegistry) -> None:
    for name in list(_REGISTERED_SKILL_NAMES):
        registry.unregister(name)
    _REGISTERED_SKILL_NAMES.clear()


# 工厂函数立即绑定 skill_name，避免闭包延迟绑定问题。
def make_skill_handler(
    skill_name: str, loader: SkillLoader, executor: SkillExecutor
) -> Callable[[CommandContext], Awaitable[None]]:
    async def handler(ctx: CommandContext) -> None:
        skill = loader.get(skill_name)
        if skill is None:
            ctx.ui.add_system_message(
                f"Skill {skill_name} 已不可用，请 /skill reload"
            )
            return
        if skill.mode == "fork":
            # fork 模式后台异步执行，完成后回系统消息。
            asyncio.create_task(_run_fork(skill, ctx, executor))
        else:
            # inline 模式：执行后发送 prompt 触发 Agent 回合。
            prompt = await executor.execute_inline(skill, ctx.args)
            ctx.ui.send_user_message(prompt)

    return handler


# fork 模式后台执行：调 execute_fork 后把结果作为系统消息返回主对话。
async def _run_fork(
    skill: object, ctx: CommandContext, executor: SkillExecutor
) -> None:
    try:
        result = await executor.execute_fork(skill, ctx.args)  # type: ignore[arg-type]
        ctx.ui.add_system_message(
            f"[Skill {getattr(skill, 'name', '')} fork 结果]\n\n{result}"
        )
    except Exception as e:
        ctx.ui.add_system_message(
            f"[Skill {getattr(skill, 'name', '')} fork 失败] {e}"
        )


# 遍历 loader.get_catalog() 把每个 Skill 注册为 PROMPT 类型斜杠命令。
# 重名 Skill 命令跳过并 warning 日志，避免覆盖已有命令。
def register_skill_commands(
    registry: CommandRegistry, loader: SkillLoader, executor: SkillExecutor
) -> None:
    _clear_registered(registry)
    for skill_name, description in loader.get_catalog():
        if registry.find(skill_name) is not None:
            logger.warning(
                "Skill 命令 %s 与已有命令重名，跳过注册", skill_name
            )
            continue
        cmd = Command(
            name=skill_name,
            description=description,
            type=CommandType.PROMPT,
            handler=make_skill_handler(skill_name, loader, executor),
        )
        try:
            registry.register_sync(cmd)
            _REGISTERED_SKILL_NAMES.add(skill_name)
        except ValueError as e:
            logger.warning("Skill 命令 %s 注册失败: %s", skill_name, e)


# 返回闭包供 app.py 注册到 loader.register_reload_callback。
def make_skill_register_callback(
    registry: CommandRegistry, loader: SkillLoader, executor: SkillExecutor
) -> Callable[[], None]:
    def callback() -> None:
        register_skill_commands(registry, loader, executor)

    return callback
