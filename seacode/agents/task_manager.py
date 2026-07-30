"""后台任务管理器：状态机、通知队列与 ESC adopt_running 切换。

后台任务通过 ``asyncio.create_task`` 执行 ``run_to_completion``，完成后入
``_notify_queue: asyncio.Queue[str]``；``app.py`` 每 2 秒轮询一次，调用
``inject_task_notifications`` 把 ``<task-notification>`` XML 块以 user message
形式注入主对话；完成/失败会触发新一轮 LLM 调用，取消-only 通知不会重启回合。

状态机：``running → completed``（正常完成）/ ``failed``（捕获 Exception）/
``cancelled``（捕获 CancelledError）。``_notify_queue.put`` 在 finally 中执行，
确保任何结束状态都入队。

``adopt_running`` 把前台运行中的子 Agent 切换为后台任务，在已有 ``partial_result``
之上拼接新结果。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class ProgressInfo:
    """后台任务进度信息；由 Agent.run_to_completion 完成后回填。"""

    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_activity: float = 0.0


@dataclass
class BackgroundTask:
    """一个后台任务的完整状态；``cancel`` 持有 asyncio.Task.cancel 引用。"""

    id: str
    name: str
    agent: Any
    task: str
    status: str = "running"  # running / completed / failed / cancelled
    result: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    cancel: Any = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)


class TaskManager:
    """后台任务调度与状态机；线程模型假定单 event loop 内运行。"""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._async_tasks: dict[str, asyncio.Task[None]] = {}
        # 完成任务 id 队列；poll_completed 非阻塞取出。
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()

    # 后台启动一个子 Agent；fork 路径传 fork_conversation，定义式路径传 task 字符串。
    async def launch(
        self,
        agent: Any,
        task: str,
        name: str,
        fork_conversation: Any = None,
    ) -> str:
        task_id = uuid4().hex[:8]
        bg = BackgroundTask(id=task_id, name=name, agent=agent, task=task)
        self._tasks[task_id] = bg
        async_task = asyncio.create_task(
            self._run_background(task_id, agent, task, fork_conversation)
        )
        bg.cancel = async_task.cancel
        self._async_tasks[task_id] = async_task
        return task_id

    # 后台执行体：fork 路径调用 run_to_completion("", fork_conversation)，
    # 定义式路径调用 run_to_completion(task)；team 模式下进入长驻 follow-up 循环。
    async def _run_background(
        self,
        task_id: str,
        agent: Any,
        task: str,
        fork_conversation: Any,
    ) -> None:
        bg = self._tasks[task_id]
        try:
            if fork_conversation is not None:
                await agent.run_to_completion("", fork_conversation)
            else:
                await agent.run_to_completion(task)
            bg.status = "completed"
            bg.result = getattr(agent, "last_output", "") or ""
            # team 模式下进入长驻 follow-up 循环：worker 完成初始任务后不立即退出，
            # 而是发送 idle 通知给 lead，并轮询邮箱等待 lead 下发的后续任务。
            # 这是 team 协调模式的核心运转机制——让长驻 teammate 可被反复调度。
            await self._run_team_followup_loop(agent)
        except asyncio.CancelledError:
            bg.status = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001 — 后台任务需捕获所有异常避免吞掉错误
            bg.status = "failed"
            bg.result = str(e)
        finally:
            bg.end_time = time.monotonic()
            bg.progress.input_tokens = getattr(agent, "total_input_tokens", 0)
            bg.progress.output_tokens = getattr(agent, "total_output_tokens", 0)
            self._async_tasks.pop(task_id, None)
            # put_nowait 避免在 finally 中 await；队列无界不会阻塞。
            self._notify_queue.put_nowait(task_id)

    # team 模式长驻 follow-up 循环：worker 完成初始任务后发送 idle 通知，
    # 然后轮询邮箱最多 60 轮（每轮 1 秒）等待 lead 下发的后续任务。
    # 收到消息时调用 run_to_completion 执行；无消息则继续等待。
    # 非 team 模式（agent 无 team_name 或 _team_manager）直接返回不阻塞。
    async def _run_team_followup_loop(self, agent: Any) -> None:
        team_name = getattr(agent, "team_name", None)
        team_manager = getattr(agent, "_team_manager", None)
        if not team_name or not team_manager:
            return
        mailbox = team_manager.get_mailbox(team_name)
        if mailbox is None:
            return
        agent_id = getattr(agent, "agent_id", "")
        # 发送初始 idle 通知，告知 lead 当前 worker 已空闲可接新任务。
        idle_msg = self._build_idle_notification(agent_id)
        mailbox.write("lead", idle_msg)
        # 长驻轮询：最多 60 轮，每轮等 1 秒；收到消息则执行并重新发 idle。
        for _ in range(60):
            await asyncio.sleep(1)
            try:
                msgs = mailbox.consume(agent_id)
            except Exception:  # noqa: BLE001 — 邮箱读取失败不退出循环
                continue
            if not msgs:
                continue
            # 拼接所有消息内容作为新一轮 prompt；summary 优先用于上下文。
            prompt_parts = [m.content for m in msgs if m.content]
            if not prompt_parts:
                continue
            prompt = "\n\n".join(prompt_parts)
            try:
                await agent.run_to_completion(prompt)
                # 任务完成后重新发 idle，让 lead 知道 worker 可接新任务。
                mailbox.write("lead", idle_msg)
            except Exception:  # noqa: BLE001 — follow-up 失败不退出循环
                # 失败时也发 idle，让 lead 决定是否重试或下发新任务。
                mailbox.write("lead", idle_msg)

    # 构造 idle 通知消息；lead 收到后可将该 worker 纳入新一轮任务调度。
    def _build_idle_notification(self, agent_id: str) -> Any:
        from seacode.teams.mailbox import create_message

        return create_message(
            from_agent=agent_id,
            to_agent="lead",
            content="",
            summary="idle: ready for next task",
            message_type="text",
        )

    # 把前台运行中的子 Agent 切换为后台任务；partial_result 是已积累的部分输出。
    async def adopt_running(
        self,
        agent: Any,
        task_description: str,
        partial_result: str = "",
        name: str = "background task",
    ) -> str:
        task_id = uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name,
            agent=agent,
            task=task_description,
            result=partial_result,
        )
        self._tasks[task_id] = bg
        async_task = asyncio.create_task(
            self._continue_background(task_id, agent, task_description, partial_result)
        )
        bg.cancel = async_task.cancel
        self._async_tasks[task_id] = async_task
        return task_id

    # adopt_running 的执行体：在 partial_result 之上拼接新输出。
    async def _continue_background(
        self,
        task_id: str,
        agent: Any,
        task_description: str,
        partial_result: str,
    ) -> None:
        bg = self._tasks[task_id]
        try:
            await agent.run_to_completion(task_description)
            new_output = getattr(agent, "last_output", "") or ""
            bg.result = partial_result + new_output
            bg.status = "completed"
        except asyncio.CancelledError:
            bg.status = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001
            bg.status = "failed"
            bg.result = f"{partial_result}\n[error: {e}]"
        finally:
            bg.end_time = time.monotonic()
            bg.progress.input_tokens = getattr(agent, "total_input_tokens", 0)
            bg.progress.output_tokens = getattr(agent, "total_output_tokens", 0)
            self._async_tasks.pop(task_id, None)
            self._notify_queue.put_nowait(task_id)

    # 取消任务；仅对 status=running 且未完成的 async_task 生效。
    async def cancel(self, task_id: str) -> bool:
        bg = self._tasks.get(task_id)
        if bg is None or bg.status != "running":
            return False
        async_task = self._async_tasks.get(task_id)
        if async_task is None or async_task.done():
            return False
        async_task.cancel()
        return True

    # 按 id 取出任务；不存在返回 None。
    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    # 列出所有后台任务（按插入顺序）。
    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    # 非阻塞取出所有已完成的任务；流式输出期间不调用避免冲突。
    def poll_completed(self) -> list[BackgroundTask]:
        result: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            task_id = self._notify_queue.get_nowait()
            bg = self._tasks.get(task_id)
            if bg is not None:
                result.append(bg)
        return result
