"""TaskManager 单元测试：覆盖 launch、_run_background、adopt_running、
cancel、poll_completed、get/list_tasks。
"""

from __future__ import annotations

import asyncio
from typing import Any

from seacode.agents.task_manager import BackgroundTask, TaskManager


# 假 Agent：可配置 run_to_completion 的返回值或异常。
class _FakeAgent:
    def __init__(
        self,
        *,
        result: str = "done",
        error: Exception | None = None,
        cancel_error: bool = False,
    ) -> None:
        self._result = result
        self._error = error
        self._cancel_error = cancel_error
        self.last_output: str = ""
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.run_calls: list[tuple[Any, ...]] = []

    async def run_to_completion(self, task: str, conversation: Any = None) -> str:
        self.run_calls.append((task, conversation))
        if self._cancel_error:
            raise asyncio.CancelledError()
        if self._error is not None:
            raise self._error
        self.last_output = self._result
        return self._result


# 阻塞型假 Agent：run_to_completion 永远等待，用于测试 cancel。
class _BlockingAgent:
    def __init__(self) -> None:
        self.last_output: str = ""
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self._block = asyncio.Event()

    async def run_to_completion(self, task: str, conversation: Any = None) -> str:
        await self._block.wait()
        return "unblocked"


# 等待 task_manager 内部 async_task 完成并消费 CancelledError 异常避免警告。
# 先取引用再等待：cancel 后 _async_tasks 可能已被 finally 弹出。
async def _drain_async_task(tm: TaskManager, task_id: str) -> None:
    async_task = tm._async_tasks.get(task_id)
    if async_task is None:
        return
    try:
        await async_task
    except asyncio.CancelledError:
        pass


# 让事件循环有机会运行内部 task 到达首个 await 点；cancel 前调用避免任务未启动。
async def _let_task_start(tm: TaskManager, task_id: str) -> asyncio.Task[None] | None:
    async_task = tm._async_tasks.get(task_id)
    # 让事件循环调度一次，让 _run_background 进入首个 await。
    await asyncio.sleep(0)
    return async_task


# ---------------------------------------------------------------------------
# launch 与 _run_background
# ---------------------------------------------------------------------------


# 验证 launch 后台启动并返回 8 字符 hex 格式的 task_id。
# 调用 launch 后断言 task_id 长度为 8 且是十六进制。
async def test_launch_returns_eight_char_hex_task_id() -> None:
    tm = TaskManager()
    agent = _FakeAgent()
    task_id = await tm.launch(agent, "task", "name")
    assert len(task_id) == 8
    int(task_id, 16)


# 验证 _run_background 定义式路径调用 run_to_completion(task)。
# launch 后等待完成，断言 run_to_completion 被调用且第一个参数是 task 字符串。
async def test_run_background_defined_path_calls_run_to_completion_with_task() -> None:
    tm = TaskManager()
    agent = _FakeAgent(result="result")
    task_id = await tm.launch(agent, "do task", "name")
    await _drain_async_task(tm, task_id)
    assert len(agent.run_calls) == 1
    assert agent.run_calls[0][0] == "do task"
    # 定义式路径不传 conversation。
    assert agent.run_calls[0][1] is None


# 验证 _run_background fork 路径调用 run_to_completion("", fork_conversation)。
# launch 时传 fork_conversation，等待完成，断言 run_to_completion 被调用且参数为
# 空 task 与 fork_conversation。
async def test_run_background_fork_path_calls_run_to_completion_with_fork_conv() -> None:
    tm = TaskManager()
    agent = _FakeAgent(result="result")
    fork_conv = object()  # 任意对象作为 fork_conversation 标记
    task_id = await tm.launch(
        agent, "", "fork", fork_conversation=fork_conv
    )
    await _drain_async_task(tm, task_id)
    assert len(agent.run_calls) == 1
    assert agent.run_calls[0][0] == ""
    assert agent.run_calls[0][1] is fork_conv


# 验证正常完成入 _notify_queue。
# launch 后等待完成，poll_completed 断言返回 1 个 status=completed 的任务。
async def test_run_background_completed_enters_notify_queue() -> None:
    tm = TaskManager()
    agent = _FakeAgent(result="done output")
    task_id = await tm.launch(agent, "task", "name")
    await _drain_async_task(tm, task_id)
    completed = tm.poll_completed()
    assert len(completed) == 1
    assert completed[0].status == "completed"
    assert completed[0].result == "done output"
    assert completed[0].end_time is not None


# 验证 CancelledError 转为 cancelled 状态。
# launch 阻塞 agent，先让任务进入 try 块内的 await 点，cancel 后等待完成，断言 status=cancelled。
async def test_run_background_cancelled_sets_cancelled_status() -> None:
    tm = TaskManager()
    agent = _BlockingAgent()
    task_id = await tm.launch(agent, "task", "name")
    await _let_task_start(tm, task_id)
    success = await tm.cancel(task_id)
    assert success is True
    await _drain_async_task(tm, task_id)
    completed = tm.poll_completed()
    assert len(completed) == 1
    assert completed[0].status == "cancelled"


# 验证 Exception 转为 failed 状态且 result 含异常信息。
# launch 抛 RuntimeError 的 agent，等待完成，断言 status=failed 且 result 含异常文本。
async def test_run_background_exception_sets_failed_status() -> None:
    tm = TaskManager()
    agent = _FakeAgent(error=RuntimeError("boom"))
    task_id = await tm.launch(agent, "task", "name")
    await _drain_async_task(tm, task_id)
    completed = tm.poll_completed()
    assert len(completed) == 1
    assert completed[0].status == "failed"
    assert "boom" in completed[0].result


# 验证 CancelledError 直接由 run_to_completion 抛出时也转为 cancelled 状态。
# launch _cancel_error=True 的 agent，等待完成，断言 status=cancelled。
async def test_run_background_cancelled_error_from_agent_sets_cancelled() -> None:
    tm = TaskManager()
    agent = _FakeAgent(cancel_error=True)
    task_id = await tm.launch(agent, "task", "name")
    await _drain_async_task(tm, task_id)
    completed = tm.poll_completed()
    assert len(completed) == 1
    assert completed[0].status == "cancelled"


# ---------------------------------------------------------------------------
# adopt_running
# ---------------------------------------------------------------------------


# 验证 adopt_running 切换前台子 Agent 到后台并拼接 partial_result。
# 调用 adopt_running 传 partial_result，等待完成，断言 result 含 partial 与新输出。
async def test_adopt_running_concatenates_partial_result() -> None:
    tm = TaskManager()
    agent = _FakeAgent(result="new output")
    task_id = await tm.adopt_running(
        agent, "bg task", partial_result="partial", name="adopted"
    )
    await _drain_async_task(tm, task_id)
    bg = tm.get(task_id)
    assert bg is not None
    assert "partial" in bg.result
    assert "new output" in bg.result
    assert bg.status == "completed"


# 验证 adopt_running 返回 8 字符 hex task_id。
# 调用 adopt_running，断言 task_id 长度为 8 且是十六进制。
async def test_adopt_running_returns_eight_char_hex_task_id() -> None:
    tm = TaskManager()
    agent = _FakeAgent()
    task_id = await tm.adopt_running(agent, "task", partial_result="p")
    assert len(task_id) == 8
    int(task_id, 16)


# 验证 adopt_running 异常路径把错误信息拼入 result。
# adopt_running 抛 RuntimeError 的 agent，等待完成，断言 status=failed 且
# result 含 partial 与 error。
async def test_adopt_running_exception_path() -> None:
    tm = TaskManager()
    agent = _FakeAgent(error=RuntimeError("adopt boom"))
    task_id = await tm.adopt_running(
        agent, "bg task", partial_result="partial", name="adopted"
    )
    await _drain_async_task(tm, task_id)
    bg = tm.get(task_id)
    assert bg is not None
    assert bg.status == "failed"
    assert "partial" in bg.result
    assert "adopt boom" in bg.result


# ---------------------------------------------------------------------------
# cancel
# ------------------------------------------------------------------


# 验证 cancel 仅对 running 且未完成的任务生效。
# launch 阻塞 agent，先让任务进入 try 块内的 await 点，cancel 返回 True；等待后 status=cancelled。
async def test_cancel_succeeds_for_running_task() -> None:
    tm = TaskManager()
    agent = _BlockingAgent()
    task_id = await tm.launch(agent, "task", "name")
    await _let_task_start(tm, task_id)
    success = await tm.cancel(task_id)
    assert success is True
    await _drain_async_task(tm, task_id)
    bg = tm.get(task_id)
    assert bg is not None
    assert bg.status == "cancelled"


# 验证 cancel 对已完成任务返回 False。
# launch 立即完成的 agent，等待完成后 cancel，断言返回 False。
async def test_cancel_returns_false_for_completed_task() -> None:
    tm = TaskManager()
    agent = _FakeAgent(result="done")
    task_id = await tm.launch(agent, "task", "name")
    await _drain_async_task(tm, task_id)
    success = await tm.cancel(task_id)
    assert success is False


# 验证 cancel 对不存在任务返回 False。
# cancel 不存在的 task_id，断言返回 False。
async def test_cancel_returns_false_for_unknown_task() -> None:
    tm = TaskManager()
    success = await tm.cancel("nonexistent")
    assert success is False


# ---------------------------------------------------------------------------
# poll_completed 与 get / list_tasks
# ------------------------------------------------------------------


# 验证 poll_completed 非阻塞取出所有完成 task。
# 启动 3 个后台任务全部完成，poll_completed 断言返回 3 个；第二次调用返回空。
async def test_poll_completed_returns_all_completed() -> None:
    tm = TaskManager()
    ids = []
    for i in range(3):
        agent = _FakeAgent(result=f"r{i}")
        task_id = await tm.launch(agent, f"t{i}", f"n{i}")
        ids.append(task_id)
    for tid in ids:
        await _drain_async_task(tm, tid)
    completed = tm.poll_completed()
    assert len(completed) == 3
    # 第二次调用返回空。
    assert tm.poll_completed() == []


# 验证 get 与 list_tasks 返回正确数据。
# 启动 2 个后台任务，list_tasks 断言长度为 2；get 断言返回相同对象。
async def test_get_and_list_tasks_return_correct_data() -> None:
    tm = TaskManager()
    id1 = await tm.launch(_FakeAgent(), "t1", "n1")
    id2 = await tm.launch(_FakeAgent(), "t2", "n2")
    tasks = tm.list_tasks()
    assert len(tasks) == 2
    bg1 = tm.get(id1)
    assert bg1 is not None
    assert bg1.id == id1
    # list_tasks 返回的对象与 get 返回的是同一对象。
    assert tm.get(id2) is tasks[1] or tm.get(id2) is tasks[0]


# 验证 get 不存在返回 None。
# get 不存在的 task_id，断言返回 None。
def test_get_returns_none_for_unknown() -> None:
    tm = TaskManager()
    assert tm.get("nonexistent") is None


# 验证 list_tasks 空列表。
# 新建 TaskManager，list_tasks 断言返回空列表。
def test_list_tasks_empty() -> None:
    tm = TaskManager()
    assert tm.list_tasks() == []


# 验证 poll_completed 空队列返回空列表。
# 新建 TaskManager，poll_completed 断言返回空列表。
def test_poll_completed_empty_returns_empty() -> None:
    tm = TaskManager()
    assert tm.poll_completed() == []


# 验证 BackgroundTask 数据类默认值。
# 构造 BackgroundTask，断言 status=running、result=空、end_time=None、progress 默认。
def test_background_task_defaults() -> None:
    bg = BackgroundTask(id="abc", name="n", agent=None, task="t")
    assert bg.status == "running"
    assert bg.result == ""
    assert bg.end_time is None
    assert bg.progress.tool_call_count == 0
    assert bg.progress.input_tokens == 0
    assert bg.progress.output_tokens == 0
    assert bg.progress.last_activity == 0.0
