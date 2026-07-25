# 同进程 teammate 后端：在 asyncio Task 中长驻执行 teammate Agent 主循环。
"""teams 子包的 in-process spawn 实现。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from seacode.teams.mailbox import Mailbox, MailboxMessage, create_message
from seacode.teams.progress import TeammateProgress, random_verb

if TYPE_CHECKING:
    from seacode.conversation import ConversationManager

log = logging.getLogger(__name__)

# Idle 轮询间隔（秒）：teammate 完成一轮后按此间隔检查邮箱新消息。
IDLE_POLL_INTERVAL = 0.5

# shutdown 消息前缀：content 以此开头视为关闭请求。
SHUTDOWN_PREFIX = "[shutdown]"

# lead 名称：idle 通知与 drain 的统一收件人键。
LEAD_NAME = "lead"


# 判断邮箱消息是否为关闭请求：message_type 标记或 content 前缀。
def _is_shutdown_request(msg: MailboxMessage) -> bool:
    if msg.message_type == "shutdown_request":
        return True
    return msg.content.strip().startswith(SHUTDOWN_PREFIX)


# 构造 idle 通知消息，发给 lead 表明 teammate 当前轮次已完成。
def _create_idle_notification(member_name: str, reason: str) -> MailboxMessage:
    return create_message(
        from_agent=member_name,
        to_agent=LEAD_NAME,
        content=f"[idle] {member_name} (reason: {reason})",
        summary="idle",
    )


# 消费 teammate 邮箱中的未读消息并以 system-reminder 形式注入对话；无消息时不注入。
def _inject_pending_messages(
    mailbox: Mailbox, agent_id: str, conversation: ConversationManager
) -> None:
    msgs = mailbox.consume(agent_id)
    if not msgs:
        return
    text = "You have new messages:\n" + "\n".join(
        f"From {m.from_agent}: {m.content}" for m in msgs
    )
    conversation.add_system_reminder(text)


# 阻塞轮询邮箱，等到有新消息后返回 (prompt, is_shutdown)；shutdown 返回 ("", True)。
async def _wait_for_next_prompt_or_shutdown(
    mailbox: Mailbox, agent_id: str
) -> tuple[str, bool]:
    while True:
        await asyncio.sleep(IDLE_POLL_INTERVAL)
        msgs = mailbox.consume(agent_id)
        if not msgs:
            continue
        # 分离 shutdown 与普通消息；shutdown 优先返回。
        if any(_is_shutdown_request(m) for m in msgs):
            return ("", True)
        prompt = "You have new messages from your team:\n" + "\n".join(
            f"From {m.from_agent}: {m.content}" for m in msgs
        )
        return (prompt, False)


class InProcessTeammateHandle:
    # 同进程 teammate 的运行时句柄；持有 agent、asyncio Task、progress。
    def __init__(
        self,
        agent: Any,
        task: asyncio.Task[str],
        name: str,
        progress: TeammateProgress,
    ) -> None:
        self.agent = agent
        self.task = task
        self.name = name
        self.progress = progress
        self._result: str = ""

    @property
    def done(self) -> bool:
        # Task 完成态；含正常结束、取消与异常。
        return self.task.done()

    @property
    def result(self) -> str | None:
        # 已完成时返回结果字符串；任务异常时返回 None 区分"空完成"与"失败"；
        # 未完成返回 None。通过 task.exception() 区分失败，不抛异常到调用方。
        if not self.task.done():
            return None
        if self.task.cancelled():
            return None
        exc = self.task.exception()
        if exc is not None:
            return None
        return self._result

    def cancel(self) -> None:
        # 取消底层 asyncio Task；幂等。
        if not self.task.done():
            self.task.cancel()


# 在同进程中 spawn 一个 teammate：创建 progress、绑定事件回调、启动 asyncio Task 主循环。
def spawn_inprocess_teammate(
    agent: Any,
    task: str,
    name: str,
    team_manager: Any,
    mailbox: Mailbox | None = None,
) -> InProcessTeammateHandle:
    # team_name 优先从 team_manager 反查；查不到时 progress.team_name 暂为空。
    team_name = ""
    if team_manager is not None:
        team_name = team_manager.get_team_for_teammate(name) or ""
    progress = TeammateProgress(
        name=name, team_name=team_name, spinner_verb=random_verb()
    )
    # 若成员已注册，附加 progress 供 TeamManager.get_all_teammate_progress 收集。
    if team_name and team_manager is not None:
        team = team_manager.get_team(team_name)
        if team is not None:
            member = team.get_member(name)
            if member is not None:
                member.progress = progress

    def _on_event(event: dict[str, Any]) -> None:
        # run_to_completion 的 event_callback 以 dict 形式调用；
        # 按 event_type 分发：tool_use 记录真实工具名，usage 记录 token 用量，
        # stream_text 更新 last_message。对齐主 Agent 的事件协议。
        event_type = event.get("type")
        if event_type == "tool_use":
            tool_name = event.get("toolName", event.get("tool_name", "tool"))
            args = event.get("args", {})
            progress.record_tool_use(tool_name, args)
        elif event_type == "usage":
            usage = event.get("usage", {})
            input_tokens = usage.get("inputTokens", usage.get("input_tokens", 0))
            output_tokens = usage.get("outputTokens", usage.get("output_tokens", 0))
            # TeammateProgress.token_count 是单一累加值；输入+输出合并计入。
            progress.record_tokens(input_tokens + output_tokens)
        elif event_type == "stream_text":
            text = event.get("text", "")
            if text:
                with progress._lock:
                    progress.last_message = text
        # 兼容旧事件格式：无 type 字段时回退到 text / tool_calls 提取。
        elif "text" in event:
            text = event.get("text", "")
            if text:
                with progress._lock:
                    progress.last_message = text

    async def _run() -> str:
        # teammate 主循环：有 mailbox 长驻（执行→idle 通知→等待新任务）；无 mailbox 单次返回。
        try:
            from seacode.conversation import ConversationManager as CM

            conv = CM()
            next_prompt: str = task
            idle_reason = "available"

            while True:
                # 第 1 步：注入本轮开始前邮箱里堆积的消息。
                if mailbox is not None:
                    _inject_pending_messages(mailbox, agent.agent_id, conv)

                # 第 2 步：执行一个完整 agent turn。
                result = await agent.run_to_completion(
                    next_prompt, conv, event_callback=_on_event
                )
                handle._result = result
                next_prompt = ""

                # 第 3 步：无 mailbox 时退化为单次返回。
                if mailbox is None:
                    progress.status = "completed"
                    return result

                # 第 4 步：更新进度状态。
                if idle_reason == "failed":
                    progress.status = "failed"
                else:
                    progress.status = "idle"

                # 第 5 步：通知 lead 本轮已完成。
                mailbox.write(LEAD_NAME, _create_idle_notification(name, idle_reason))
                idle_reason = "available"

                # 第 6 步：轮询等待 lead 下发新任务或 shutdown。
                new_prompt, shutdown = await _wait_for_next_prompt_or_shutdown(
                    mailbox, agent.agent_id
                )
                if shutdown:
                    progress.status = "completed"
                    return result
                next_prompt = new_prompt

        except asyncio.CancelledError:
            progress.status = "stopped"
            raise
        except Exception as e:
            log.error("teammate %s failed: %s", name, e)
            progress.status = "failed"
            if mailbox is not None:
                try:
                    mailbox.write(
                        LEAD_NAME, _create_idle_notification(name, f"failed: {e}")
                    )
                except Exception:
                    pass
            raise

    task_handle = asyncio.create_task(_run(), name=f"teammate-{name}")
    handle = InProcessTeammateHandle(
        agent=agent, task=task_handle, name=name, progress=progress
    )
    log.info("spawned in-process teammate %s", name)
    return handle
