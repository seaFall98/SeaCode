"""/tasks 命令单元测试：覆盖 list / info / cancel 子命令与格式化函数。"""

from __future__ import annotations

import time

from seacode.agents.task_manager import BackgroundTask, TaskManager
from seacode.commands.handlers.tasks import (
    _format_elapsed,
    _format_status,
    create_tasks_command,
    create_tasks_handler,
)
from seacode.commands.registry import CommandContext


# 实现 UIController 协议的假对象：记录 add_system_message 调用供断言。
class _FakeUI:
    def __init__(self) -> None:
        self.system_messages: list[str] = []

    def add_system_message(self, text: str) -> None:
        self.system_messages.append(text)

    def send_user_message(self, text: str) -> None:
        del text

    def set_plan_mode(self, enabled: bool) -> None:
        del enabled

    def get_token_count(self) -> tuple[int, int]:
        return (0, 0)

    def refresh_status(self) -> None:
        return None


# 构造 CommandContext；args 为子命令参数字符串，ui 捕获输出。
def _make_ctx(args: str = "", ui: _FakeUI | None = None) -> CommandContext:
    return CommandContext(
        args=args,
        agent=None,
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui if ui is not None else _FakeUI(),
        config=None,
    )


# 直接往 TaskManager 插入预设 BackgroundTask，避免启动真实 async 任务。
def _seed_task(
    tm: TaskManager,
    *,
    task_id: str = "abc12345",
    name: str = "Explore",
    status: str = "running",
    result: str = "",
    start_time: float | None = None,
    end_time: float | None = None,
) -> BackgroundTask:
    bg = BackgroundTask(
        id=task_id,
        name=name,
        agent=None,
        task="task",
        status=status,
        result=result,
        start_time=start_time if start_time is not None else time.time(),
        end_time=end_time,
    )
    tm._tasks[task_id] = bg  # type: ignore[assignment]
    return bg


# ---------------------------------------------------------------------------
# _format_status 与 _format_elapsed
# ---------------------------------------------------------------------------


# 验证 _format_status 四种状态图标映射。
# 直接断言 running/completed/failed/cancelled 对应图标。
def test_format_status_four_icons() -> None:
    assert _format_status("running") == "⏳"
    assert _format_status("completed") == "✓"
    assert _format_status("failed") == "✗"
    assert _format_status("cancelled") == "⊘"


# 验证 _format_status 未知状态返回问号。
# 直接断言未知状态返回 "?"。
def test_format_status_unknown_returns_question() -> None:
    assert _format_status("unknown") == "?"


# 验证 _format_elapsed >= 60 秒显示 X.Xm 格式。
# 传入 65 秒，断言返回 "1.1m"。
def test_format_elapsed_minutes_format() -> None:
    assert _format_elapsed(65) == "1.1m"


# 验证 _format_elapsed < 60 秒显示 X.Xs 格式。
# 传入 5 秒，断言返回 "5.0s"。
def test_format_elapsed_seconds_format() -> None:
    assert _format_elapsed(5) == "5.0s"


# ---------------------------------------------------------------------------
# /tasks 默认列出
# ---------------------------------------------------------------------------


# 验证 /tasks 列出所有后台任务含 id / name / 状态图标 / elapsed。
# 构造 2 个 task（running 与 completed），断言输出含 task_id / name / 图标。
async def test_tasks_list_shows_all_tasks() -> None:
    tm = TaskManager()
    now = time.time()
    _seed_task(
        tm,
        task_id="abc12345",
        name="Explore",
        status="running",
        start_time=now - 5,
    )
    _seed_task(
        tm,
        task_id="def67890",
        name="Plan",
        status="completed",
        start_time=now - 10,
        end_time=now,
    )
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="", ui=ui)

    await handler(ctx)

    assert len(ui.system_messages) == 1
    output = ui.system_messages[0]
    assert "abc12345" in output
    assert "def67890" in output
    assert "Explore" in output
    assert "Plan" in output
    assert "⏳" in output
    assert "✓" in output


# 验证 /tasks 空列表返回 "没有后台任务"。
# 空 TaskManager，断言输出 == "没有后台任务"。
async def test_tasks_list_empty_returns_message() -> None:
    tm = TaskManager()
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="", ui=ui)

    await handler(ctx)

    assert ui.system_messages == ["没有后台任务"]


# ---------------------------------------------------------------------------
# /tasks info
# ---------------------------------------------------------------------------


# 验证 /tasks info <id> 显示详情与结果预览。
# 构造 task 含 result="done"，断言输出含 id / name / status / 耗时 / 结果。
async def test_tasks_info_shows_details() -> None:
    tm = TaskManager()
    now = time.time()
    _seed_task(
        tm,
        task_id="abc12345",
        name="Explore",
        status="completed",
        result="task completed successfully",
        start_time=now - 5,
        end_time=now,
    )
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="info abc12345", ui=ui)

    await handler(ctx)

    assert len(ui.system_messages) == 1
    output = ui.system_messages[0]
    assert "abc12345" in output
    assert "Explore" in output
    assert "completed" in output
    assert "task completed successfully" in output


# 验证 /tasks info <id> 结果截断 2000 字符。
# 构造 task result 长 3000 字符，断言输出结果部分不超过 2000。
async def test_tasks_info_truncates_long_result() -> None:
    tm = TaskManager()
    long_result = "x" * 3000
    _seed_task(
        tm,
        task_id="abc12345",
        name="Explore",
        status="completed",
        result=long_result,
    )
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="info abc12345", ui=ui)

    await handler(ctx)

    output = ui.system_messages[0]
    # 结果预览部分应不超过 2000 字符；截取 "结果:\n" 之后的内容判断。
    result_part = output.split("结果:\n", 1)[1] if "结果:\n" in output else output
    assert len(result_part) <= 2000


# 验证 /tasks info <id> 任务不存在返回 "未找到任务"。
# 不存在的 task_id，断言输出含 "未找到任务"。
async def test_tasks_info_unknown_returns_not_found() -> None:
    tm = TaskManager()
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="info nonexistent", ui=ui)

    await handler(ctx)

    assert "未找到任务" in ui.system_messages[0]


# 验证 /tasks info 无参数返回用法提示。
# args="info" 无 id，断言输出含 "用法"。
async def test_tasks_info_no_id_shows_usage() -> None:
    tm = TaskManager()
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="info", ui=ui)

    await handler(ctx)

    assert "用法" in ui.system_messages[0]


# ---------------------------------------------------------------------------
# /tasks cancel
# ---------------------------------------------------------------------------


# 验证 /tasks cancel <id> 取消成功。
# 构造 running task 且 fake cancel 返回 True，断言输出含 "已取消"。
async def test_tasks_cancel_success() -> None:
    tm = TaskManager()
    _seed_task(tm, task_id="abc12345", status="running")

    async def _fake_cancel(task_id: str) -> bool:
        del task_id
        return True

    tm.cancel = _fake_cancel  # type: ignore[method-assign]
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="cancel abc12345", ui=ui)

    await handler(ctx)

    assert "已取消" in ui.system_messages[0]


# 验证 /tasks cancel <id> 取消失败。
# fake cancel 返回 False，断言输出含 "取消失败"。
async def test_tasks_cancel_failure() -> None:
    tm = TaskManager()

    async def _fake_cancel(task_id: str) -> bool:
        del task_id
        return False

    tm.cancel = _fake_cancel  # type: ignore[method-assign]
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="cancel abc12345", ui=ui)

    await handler(ctx)

    assert "取消失败" in ui.system_messages[0]


# 验证 /tasks cancel 无参数返回用法提示。
# args="cancel" 无 id，断言输出含 "用法"。
async def test_tasks_cancel_no_id_shows_usage() -> None:
    tm = TaskManager()
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="cancel", ui=ui)

    await handler(ctx)

    assert "用法" in ui.system_messages[0]


# ---------------------------------------------------------------------------
# 未知子命令
# ---------------------------------------------------------------------------


# 验证 /tasks 未知子命令返回提示。
# args="unknown"，断言输出含 "未知子命令" 与 "info / cancel"。
async def test_tasks_unknown_subcommand_returns_hint() -> None:
    tm = TaskManager()
    handler = create_tasks_handler(tm)
    ui = _FakeUI()
    ctx = _make_ctx(args="unknown", ui=ui)

    await handler(ctx)

    output = ui.system_messages[0]
    assert "未知子命令" in output
    assert "info / cancel" in output


# ---------------------------------------------------------------------------
# create_tasks_command
# ---------------------------------------------------------------------------


# 验证 create_tasks_command 返回含 task 别名的 Command。
# 构造 command，断言 aliases 含 "task"。
def test_create_tasks_command_has_task_alias() -> None:
    tm = TaskManager()
    cmd = create_tasks_command(tm)
    assert cmd.name == "tasks"
    assert "task" in cmd.aliases


# 验证 create_tasks_command 返回的 Command 含 handler。
# 构造 command，断言 handler 可调用。
def test_create_tasks_command_has_handler() -> None:
    tm = TaskManager()
    cmd = create_tasks_command(tm)
    assert cmd.handler is not None
    assert callable(cmd.handler)
