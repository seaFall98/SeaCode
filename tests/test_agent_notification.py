"""任务通知单元测试：覆盖 format_task_notification 格式与 inject_task_notifications 注入。"""

from __future__ import annotations

from dataclasses import dataclass, field

from seacode.agents.notification import (
    format_task_notification,
    inject_task_notifications,
)
from seacode.agents.task_manager import BackgroundTask, ProgressInfo


# 假对话：记录 add_user_message 调用供断言。
@dataclass
class _FakeConversation:
    user_messages: list[str] = field(default_factory=list)

    def add_user_message(self, content: str) -> None:
        self.user_messages.append(content)


# 构造一个完整字段的 BackgroundTask 供格式化测试使用。
def _make_task(
    *,
    id: str = "abc12345",
    name: str = "Explore",
    status: str = "completed",
    result: str = "done",
    start_time: float = 1000.0,
    end_time: float = 1005.0,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> BackgroundTask:
    progress = ProgressInfo(input_tokens=input_tokens, output_tokens=output_tokens)
    return BackgroundTask(
        id=id,
        name=name,
        agent=None,
        task="explore code",
        status=status,
        result=result,
        start_time=start_time,
        end_time=end_time,
        progress=progress,
    )


# ---------------------------------------------------------------------------
# format_task_notification
# ---------------------------------------------------------------------------


# 验证 format_task_notification 输出含全部关键字段。
# 构造完整 task，断言输出含 <task-notification> / Task ID / Agent / Status /
# Elapsed / Tokens / Result。
def test_format_task_notification_contains_all_fields() -> None:
    task = _make_task()
    notification = format_task_notification(task)
    assert "<task-notification>" in notification
    assert "Task ID: abc12345" in notification
    assert "Agent: Explore" in notification
    assert "Status: completed" in notification
    assert "Elapsed:" in notification
    assert "Tokens:" in notification
    assert "Result:" in notification
    assert "done" in notification


# 验证 format_task_notification 结果超 5000 字符截断。
# 构造 result 为 6000 字符的 task，断言输出含 ...[truncated] 且总长度小于 6000。
def test_format_task_notification_truncates_long_result() -> None:
    task = _make_task(result="x" * 6000)
    notification = format_task_notification(task)
    assert "...[truncated]" in notification
    assert len(notification) < 6000


# 验证 format_task_notification elapsed >= 60 显示 X.Xm 格式。
# 构造 start_time/end_time 间隔 65 秒，断言输出含 "1.1m"。
def test_format_task_notification_elapsed_minutes_format() -> None:
    task = _make_task(start_time=1000.0, end_time=1065.0)
    notification = format_task_notification(task)
    assert "1.1m" in notification


# 验证 format_task_notification elapsed < 60 显示 X.Xs 格式。
# 构造 start_time/end_time 间隔 5 秒，断言输出含 "5.0s"。
def test_format_task_notification_elapsed_seconds_format() -> None:
    task = _make_task(start_time=1000.0, end_time=1005.0)
    notification = format_task_notification(task)
    assert "5.0s" in notification


# 验证 format_task_notification 含 input/output token。
# 构造 task 含 token 信息，断言输出含 ↑100 ↓50 格式。
def test_format_task_notification_contains_tokens() -> None:
    task = _make_task(input_tokens=100, output_tokens=50)
    notification = format_task_notification(task)
    assert "↑100" in notification
    assert "↓50" in notification


# 验证取消任务的通知明确禁止主 Agent 自动重试。
# 构造 cancelled task，断言通知包含用户取消事实与禁止重新发起的动作约束。
def test_format_task_notification_cancelled_instructs_no_retry() -> None:
    task = _make_task(status="cancelled")
    notification = format_task_notification(task)
    assert "cancelled by the user" in notification
    assert "Do not retry" in notification


# 验证 format_task_notification end_time 为 None 时使用当前时间。
# 构造 end_time=None 的 task，断言不抛异常且输出含 Elapsed。
def test_format_task_notification_with_null_end_time() -> None:
    task = _make_task(end_time=0.0)
    task.end_time = None
    notification = format_task_notification(task)
    assert "Elapsed:" in notification


# ---------------------------------------------------------------------------
# inject_task_notifications
# ------------------------------------------------------------------


# 验证 inject_task_notifications 以 user message 注入主对话。
# 构造 2 个 task 调用 inject，断言 add_user_message 被调用 2 次且参数含 <task-notification>。
def test_inject_task_notifications_adds_user_messages() -> None:
    conv = _FakeConversation()
    tasks = [_make_task(id="t1", name="A"), _make_task(id="t2", name="B")]
    inject_task_notifications(conv, tasks)
    assert len(conv.user_messages) == 2
    for msg in conv.user_messages:
        assert "<task-notification>" in msg


# 验证 inject_task_notifications 空列表不注入。
# 调用 inject 传空列表，断言 add_user_message 未被调用。
def test_inject_task_notifications_empty_list_does_nothing() -> None:
    conv = _FakeConversation()
    inject_task_notifications(conv, [])
    assert conv.user_messages == []


# 验证 inject_task_notifications 单个任务注入一次。
# 构造 1 个 task 调用 inject，断言 add_user_message 被调用 1 次。
def test_inject_task_notifications_single_task() -> None:
    conv = _FakeConversation()
    tasks = [_make_task(id="only", name="Solo")]
    inject_task_notifications(conv, tasks)
    assert len(conv.user_messages) == 1
    assert "Solo" in conv.user_messages[0]
