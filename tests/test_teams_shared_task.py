"""teams/shared_task.py 单测：create/get/list_tasks/update/add_blocks/init_empty 全分支。"""

from __future__ import annotations

import json
import multiprocessing as mp
import queue
from pathlib import Path
from typing import Any

import pytest

from seacode.teams.shared_task import (
    SharedTask,
    SharedTaskStore,
    TaskStoreError,
)


def _create_tasks_in_worker(
    path: str, count: int, start_event: Any, errors: Any
) -> None:
    try:
        if not start_event.wait(10):
            raise RuntimeError("worker start timeout")
        store = SharedTaskStore(path)
        for index in range(count):
            store.create(title=f"worker task {index}", created_by="worker")
    except Exception as error:
        errors.put(repr(error))


def _update_task_in_worker(
    path: str, field: str, value: str, start_event: Any, errors: Any
) -> None:
    try:
        if not start_event.wait(10):
            raise RuntimeError("worker start timeout")
        store = SharedTaskStore(path)
        if field == "status":
            store.update("1", status=value)
        else:
            store.update("1", assignee=value)
    except Exception as error:
        errors.put(repr(error))

# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


# 验证 create 自增 ID：连续创建 3 个任务，ID 依次为 "1" / "2" / "3"。
# 断言 id 字符串递增、其它字段保留传入值。
def test_create_auto_increments_id(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    t1 = store.create(title="task one", description="d1", created_by="lead")
    t2 = store.create(title="task two", assignee="alice")
    t3 = store.create(title="task three")

    assert t1.id == "1"
    assert t2.id == "2"
    assert t3.id == "3"
    assert t1.title == "task one"
    assert t1.description == "d1"
    assert t1.created_by == "lead"
    assert t1.status == "pending"
    assert t2.assignee == "alice"


# 验证 create 后持久化：重新构造 SharedTaskStore 实例仍可读到已建任务。
# 断言新实例 get 返回相同字段。
def test_create_persists_across_instances(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.create(title="persisted", description="d", created_by="lead")

    new_store = SharedTaskStore(tmp_path / "tasks.json")
    task = new_store.get("1")
    assert task is not None
    assert task.title == "persisted"
    assert task.description == "d"
    assert task.created_by == "lead"


# 验证 get 不存在的 id 返回 None。
def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.init_empty()
    assert store.get("999") is None


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------


# 验证 list_tasks 按 status 与 assignee 过滤。
# 创建 3 个任务（不同 status / assignee），分别按 status、assignee、组合过滤。
def test_list_tasks_filters_by_status_and_assignee(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.create(title="t1", assignee="alice")  # id=1, pending, alice
    store.create(title="t2", assignee="bob")  # id=2, pending, bob
    # 把 t2 改成 in_progress。
    store.update("2", status="in_progress")
    store.create(title="t3", assignee="alice")  # id=3, pending, alice

    # 全量：3 条。
    all_tasks = store.list_tasks()
    assert len(all_tasks) == 3

    # 按 status 过滤。
    pending = store.list_tasks(status="pending")
    assert len(pending) == 2
    assert {t.id for t in pending} == {"1", "3"}

    in_progress = store.list_tasks(status="in_progress")
    assert len(in_progress) == 1
    assert in_progress[0].id == "2"

    # 按 assignee 过滤。
    alice_tasks = store.list_tasks(assignee="alice")
    assert len(alice_tasks) == 2
    assert {t.id for t in alice_tasks} == {"1", "3"}

    # 组合过滤。
    alice_pending = store.list_tasks(status="pending", assignee="alice")
    assert len(alice_pending) == 2


# 验证 list_tasks 在空任务板上返回空列表。
def test_list_tasks_empty(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.init_empty()
    assert store.list_tasks() == []


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


# 验证 update 增量更新：title=None 跳过，status 覆盖，assignee 覆盖。
# 断言未传字段保留原值，传入字段被覆盖。
def test_update_incremental_fields(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.create(title="orig", description="orig-desc", assignee="alice")

    updated = store.update("1", title=None, status="in_progress", assignee="bob")
    assert updated is not None
    assert updated.title == "orig"  # None 跳过
    assert updated.description == "orig-desc"  # 未传保留
    assert updated.status == "in_progress"  # 覆盖
    assert updated.assignee == "bob"  # 覆盖


# 验证 update 不存在的 task_id 返回 None。
def test_update_missing_returns_none(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.init_empty()
    assert store.update("999", status="in_progress") is None


# 验证 update 持久化：重新构造实例仍读到更新后的字段。
def test_update_persists(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.create(title="t1")
    store.update("1", status="completed")

    new_store = SharedTaskStore(tmp_path / "tasks.json")
    task = new_store.get("1")
    assert task is not None
    assert task.status == "completed"


# ---------------------------------------------------------------------------
# add_blocks / add_blocked_by
# ---------------------------------------------------------------------------


# 验证 add_blocks 去重追加：已有 "1" 时再 add ["1", "2"]，结果为 ["1", "2"]。
def test_add_blocks_deduplicates(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.create(title="t1")
    store.add_blocks("1", ["1"])
    result = store.add_blocks("1", ["1", "2"])

    assert result is not None
    assert result.blocks == ["1", "2"]


# 验证 add_blocked_by 去重追加。
def test_add_blocked_by_deduplicates(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.create(title="t1")
    store.add_blocked_by("1", ["2"])
    result = store.add_blocked_by("1", ["2", "3"])

    assert result is not None
    assert result.blocked_by == ["2", "3"]


# 验证 add_blocks 不存在 task_id 返回 None。
def test_add_blocks_missing_returns_none(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.init_empty()
    assert store.add_blocks("999", ["1"]) is None


# 验证 add_blocked_by 不存在 task_id 返回 None。
def test_add_blocked_by_missing_returns_none(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.init_empty()
    assert store.add_blocked_by("999", ["1"]) is None


# ---------------------------------------------------------------------------
# init_empty
# ---------------------------------------------------------------------------


# 验证 init_empty 清空已有任务：先创建 2 个任务，init_empty 后 list_tasks 返回空。
def test_init_empty_clears_existing_tasks(tmp_path: Path) -> None:
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.create(title="t1")
    store.create(title="t2")
    assert len(store.list_tasks()) == 2

    store.init_empty()
    assert store.list_tasks() == []
    # next_id 重置后新任务从 "1" 开始。
    t = store.create(title="after-reset")
    assert t.id == "1"


# 验证 init_empty 在不存在的路径上创建文件。
def test_init_empty_creates_file(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    assert not path.exists()
    store = SharedTaskStore(path)
    store.init_empty()
    assert path.exists()


# ---------------------------------------------------------------------------
# 容错
# ---------------------------------------------------------------------------


# 验证任务板 JSON 损坏时返回可诊断错误，不会被空任务板覆盖。
# 先写入非法 JSON，再 create，断言抛 TaskStoreError 且原始文本保持不变。
def test_corrupt_json_raises_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    original = "not a valid json {"
    path.write_text(original, encoding="utf-8")
    store = SharedTaskStore(path)
    with pytest.raises(TaskStoreError, match="无法读取任务板"):
        store.create(title="must not overwrite")
    assert path.read_text(encoding="utf-8") == original


# 验证 SharedTask.from_dict 忽略未知键并填充默认值。
def test_from_dict_ignores_unknown_keys_and_fills_defaults() -> None:
    task = SharedTask.from_dict(
        {
            "id": "5",
            "title": "from dict",
            "unknown_field": "ignored",
        }
    )
    assert task.id == "5"
    assert task.title == "from dict"
    assert task.description == ""
    assert task.status == "pending"
    assert task.assignee == ""
    assert task.blocks == []
    assert task.blocked_by == []
    assert task.created_by == ""


# 验证 SharedTask.to_dict / from_dict 往返一致。
def test_to_dict_from_dict_roundtrip() -> None:
    original = SharedTask(
        id="7",
        title="roundtrip",
        description="desc",
        status="in_progress",
        assignee="alice",
        blocks=["1", "2"],
        blocked_by=["3"],
        created_by="lead",
    )
    data = original.to_dict()
    restored = SharedTask.from_dict(data)
    assert restored == original


# 验证多个 Windows 子进程同时创建任务时 ID 唯一且任务板始终是完整 JSON。
# 三个 spawn worker 同时各创建任务，断言全部退出、无错误、任务数和 ID 连续。
def test_shared_task_store_multiprocess_create_is_consistent(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    SharedTaskStore(path).init_empty()
    context = mp.get_context("spawn")
    start_event = context.Event()
    errors = context.Queue()
    processes = [
        context.Process(
            target=_create_tasks_in_worker,
            args=(str(path), 8, start_event, errors),
        )
        for _ in range(3)
    ]
    try:
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(20)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)

    worker_errors: list[str] = []
    while True:
        try:
            worker_errors.append(errors.get(timeout=0.1))
        except queue.Empty:
            break
    assert worker_errors == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["tasks"]) == 24
    assert set(data["tasks"]) == {str(index) for index in range(1, 25)}
    assert SharedTaskStore(path).list_tasks()


# 验证多个子进程更新同一任务的不同字段时不会互相覆盖。
# 两个 spawn worker 同时更新 status 和 assignee，断言最终任务同时保留两个字段。
def test_shared_task_store_multiprocess_updates_preserve_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tasks.json"
    store = SharedTaskStore(path)
    store.create(title="shared")
    context = mp.get_context("spawn")
    start_event = context.Event()
    errors = context.Queue()
    processes = [
        context.Process(
            target=_update_task_in_worker,
            args=(str(path), "status", "completed", start_event, errors),
        ),
        context.Process(
            target=_update_task_in_worker,
            args=(str(path), "assignee", "alice", start_event, errors),
        ),
    ]
    try:
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(20)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)

    worker_errors: list[str] = []
    while True:
        try:
            worker_errors.append(errors.get(timeout=0.1))
        except queue.Empty:
            break
    assert worker_errors == []
    updated = SharedTaskStore(path).get("1")
    assert updated is not None
    assert updated.status == "completed"
    assert updated.assignee == "alice"
