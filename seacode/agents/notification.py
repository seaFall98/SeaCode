"""任务完成通知：``<task-notification>`` XML 格式与注入。

通知以 user message 形式注入主对话（system message 不被模型视为可消费内容），
让模型在下一轮看到并基于通知回复。结果超 5000 字符截断；elapsed 格式为
``X.Xm``（>= 60 秒）或 ``X.Xs``（< 60 秒）。
"""

from __future__ import annotations

import time
from typing import Any

from seacode.agents.task_manager import BackgroundTask

# 通知结果字符上限；超过则截断并附 ...[truncated] 标记。
_NOTIFICATION_RESULT_LIMIT: int = 5000


# 拼接 <task-notification> XML 块；含 Task ID / Agent / Status / Elapsed / Tokens / Result。
def format_task_notification(task: BackgroundTask) -> str:
    elapsed = (task.end_time or time.time()) - task.start_time
    if elapsed >= 60:
        elapsed_str = f"{elapsed / 60:.1f}m"
    else:
        elapsed_str = f"{elapsed:.1f}s"

    result = task.result
    if len(result) > _NOTIFICATION_RESULT_LIMIT:
        result = result[:_NOTIFICATION_RESULT_LIMIT] + "...[truncated]"

    return (
        "<task-notification>\n"
        f"Task ID: {task.id}\n"
        f"Agent: {task.name}\n"
        f"Status: {task.status}\n"
        f"Elapsed: {elapsed_str}\n"
        f"Tokens: ↑{task.progress.input_tokens} ↓{task.progress.output_tokens}\n"
        "Result:\n"
        f"{result}\n"
        "</task-notification>"
    )


# 以 user message 注入主对话；空列表不注入。
def inject_task_notifications(
    conversation: Any, tasks: list[BackgroundTask]
) -> None:
    for task in tasks:
        notification = format_task_notification(task)
        conversation.add_user_message(notification)
