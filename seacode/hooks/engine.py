"""HookEngine 核心引擎：事件触发、条件匹配、动作执行、拦截回灌。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from seacode.hooks.executors import execute_action
from seacode.hooks.models import Hook, HookContext, ToolRejectedError

log = logging.getLogger(__name__)


@dataclass
class HookNotification:
    """Hook 执行通知；由 Agent 取出后转 HookEvent yield 给 TUI 呈现状态行。"""

    hook_id: str
    event: str
    output: str
    success: bool


class HookEngine:
    """管理 Hook 列表与运行时状态；提供常规触发与 pre_tool_use 拦截两套入口。"""

    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        # prompt 动作成功的输出累积到此，由 get_prompt_messages 取出注入系统提示词。
        self._prompt_messages: list[str] = []
        # 所有 Hook 执行通知累积到此，由 drain_notifications 取出转 HookEvent。
        self._notifications: list[HookNotification] = []

    # 按 event + should_run + condition 三条件过滤命中 Hook。
    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        matched: list[Hook] = []
        for hook in self.hooks:
            if hook.event != event:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            matched.append(hook)
        return matched

    # 常规事件触发：命中 Hook 后 mark_executed，async_exec 用 ensure_future 后台执行。
    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        matched = self.find_matching_hooks(event, ctx)
        for hook in matched:
            hook.mark_executed()
            if hook.async_exec:
                # 后台执行不阻塞主流程；pre_tool_use 禁止 async（loader 已校验）。
                asyncio.ensure_future(self._run_single(hook, ctx))
            else:
                await self._run_single(hook, ctx)

    # 执行单个 Hook；prompt 成功追加到 _prompt_messages；异常兜底记 warning 不传播。
    async def _run_single(self, hook: Hook, ctx: HookContext) -> None:
        try:
            result = await execute_action(hook.action, ctx)
            if hook.action.type == "prompt" and result.success:
                self._prompt_messages.append(result.output)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=result.output,
                    success=result.success,
                )
            )
            if not result.success:
                log.warning("Hook '%s' action failed: %s", hook.id, result.output)
        except Exception as e:
            # 兜底捕获所有 Exception（不含 KeyboardInterrupt/SystemExit），
            # 避免单条 Hook 异常阻塞主流程。
            log.warning("Hook '%s' execution error: %s", hook.id, e)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=f"Exception: {e}",
                    success=False,
                )
            )

    # pre_tool_use 专用拦截入口：同步遍历命中 Hook，命中 reject 立即返回 ToolRejectedError。
    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        matched = self.find_matching_hooks("pre_tool_use", ctx)
        for hook in matched:
            hook.mark_executed()
            try:
                result = await execute_action(hook.action, ctx)
                self._notifications.append(
                    HookNotification(
                        hook_id=hook.id,
                        event="pre_tool_use",
                        output=result.output,
                        success=result.success,
                    )
                )
                if hook.reject:
                    return ToolRejectedError(
                        tool=ctx.tool_name,
                        reason=result.output,
                        hook_id=hook.id,
                    )
            except Exception as e:
                log.warning("Pre-tool hook '%s' execution error: %s", hook.id, e)
                self._notifications.append(
                    HookNotification(
                        hook_id=hook.id,
                        event="pre_tool_use",
                        output=f"Exception: {e}",
                        success=False,
                    )
                )
        return None

    # 取出累积的 prompt 注入消息并清空；每次 build_system_prompt 前调用一次。
    def get_prompt_messages(self) -> list[str]:
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages

    # 取出累积的通知并清空；Agent 在每个注入点后调用，转 HookEvent yield 给 TUI。
    def drain_notifications(self) -> list[HookNotification]:
        notifications = list(self._notifications)
        self._notifications.clear()
        return notifications
