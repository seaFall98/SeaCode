# 团队级共享任务板：单一 JSON 文件持久化，存储任务与自增 ID。
"""teams 子包的共享任务存储。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, Any] = {}
_LOCK_RETRY_COUNT = 200
_LOCK_RETRY_SECONDS = 0.025
_STALE_LOCK_SECONDS = 60


class TaskStoreError(RuntimeError):
    """共享任务板无法安全读取或提交时抛出的领域错误。"""


def _process_lock_for(path: Path) -> Any:
    # 同一规范化路径的所有 Store 实例共享一把可重入内存锁，避免同进程写入竞争。
    normalized = path.resolve()
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(normalized)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[normalized] = lock
        return lock


@dataclass
class SharedTask:
    # 一条共享任务；status 取 pending / in_progress / completed / blocked。
    id: str
    title: str
    description: str = ""
    status: str = "pending"
    assignee: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created_by: str = ""

    def to_dict(self) -> dict:
        # 序列化所有字段。
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "assignee": self.assignee,
            "blocks": self.blocks,
            "blocked_by": self.blocked_by,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SharedTask:
        # 反序列化；忽略未知键。
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            status=data.get("status", "pending"),
            assignee=data.get("assignee", ""),
            blocks=list(data.get("blocks", [])),
            blocked_by=list(data.get("blocked_by", [])),
            created_by=data.get("created_by", ""),
        )


class SharedTaskStore:
    # 团队级任务板；JSON 文件持久化，每次操作读取最新内容以支持跨进程并发。

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()
        self._process_lock = _process_lock_for(self._path)

    @property
    def _lock_path(self) -> Path:
        # 锁文件与任务板同目录，跨独立 SeaCode 进程建立排他边界。
        return self._path.with_name(f"{self._path.name}.lock")

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {"next_id": 1, "tasks": {}}

    def _validate_data(self, data: Any) -> dict[str, Any]:
        # 持久化结构异常必须显式失败，不能被降级为空任务板后覆盖。
        if not isinstance(data, dict):
            raise TaskStoreError(f"任务板格式错误: {self._path} 不是对象")
        next_id = data.get("next_id")
        tasks = data.get("tasks")
        if not isinstance(next_id, int) or next_id < 1:
            raise TaskStoreError(f"任务板格式错误: {self._path} 的 next_id 无效")
        if not isinstance(tasks, dict):
            raise TaskStoreError(f"任务板格式错误: {self._path} 的 tasks 无效")
        for task_id, task_data in tasks.items():
            if not isinstance(task_id, str) or not isinstance(task_data, dict):
                raise TaskStoreError(f"任务板格式错误: {self._path} 包含无效任务")
            try:
                task = SharedTask.from_dict(task_data)
            except (KeyError, TypeError, ValueError) as e:
                raise TaskStoreError(
                    f"任务板格式错误: {self._path} 中任务 {task_id} 无效"
                ) from e
            if task.id != task_id:
                raise TaskStoreError(
                    f"任务板格式错误: {self._path} 中任务 ID 不一致"
                )
        return data

    def _load(self) -> dict[str, Any]:
        # 只读路径依赖原子替换：读者只会看到旧完整文件或新完整文件。
        if not self._path.exists():
            return self._empty_data()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise TaskStoreError(f"无法读取任务板 {self._path}: {e}") from e
        return self._validate_data(data)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        # O_EXCL 锁文件提供 Windows 可用的跨进程互斥；stale 锁仅在超时后接管。
        lock_path = self._lock_path
        acquired = False
        for _ in range(_LOCK_RETRY_COUNT):
            try:
                fd = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
                try:
                    os.write(fd, str(os.getpid()).encode("ascii"))
                finally:
                    os.close(fd)
                acquired = True
                break
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > _STALE_LOCK_SECONDS:
                        log.warning("stale task board lock detected: %s", lock_path)
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(_LOCK_RETRY_SECONDS)
            except OSError as e:
                raise TaskStoreError(
                    f"无法创建任务板锁 {lock_path}: {e}"
                ) from e
        if not acquired:
            raise TaskStoreError(f"获取任务板锁超时: {lock_path}")
        try:
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as e:
                log.warning("failed to release task board lock %s: %s", lock_path, e)

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        # 固定锁序：进程内锁 -> 文件锁，完整读改写在两者保护下执行。
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise TaskStoreError(
                f"无法创建任务板目录 {self._path.parent}: {e}"
            ) from e
        with self._process_lock:
            with self._file_lock():
                yield

    def _save(self, data: dict[str, Any]) -> None:
        # 同目录临时文件 + flush/close + replace，未持锁读者不会看到半写 JSON。
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                json.dump(data, temp_file, indent=2, ensure_ascii=False)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, self._path)
        except (OSError, TypeError, ValueError) as e:
            raise TaskStoreError(f"无法提交任务板 {self._path}: {e}") from e
        finally:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _mutate(self, mutation: Callable[[dict[str, Any]], Any]) -> Any:
        with self._write_transaction():
            data = self._load()
            result = mutation(data)
            self._save(data)
            return result

    def init_empty(self) -> None:
        # 初始化空任务板；既有损坏文件不允许被静默重置。
        with self._write_transaction():
            if self._path.exists():
                self._load()
            self._save(self._empty_data())

    def create(
        self,
        title: str,
        description: str = "",
        created_by: str = "",
        assignee: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> SharedTask:
        # 创建任务并自增 ID；读取、修改和提交作为一个跨进程事务完成。
        def create_task(data: dict[str, Any]) -> SharedTask:
            task_id = str(data["next_id"])
            task = SharedTask(
                id=task_id,
                title=title,
                description=description,
                assignee=assignee,
                blocks=blocks or [],
                blocked_by=blocked_by or [],
                created_by=created_by,
            )
            data["tasks"][task_id] = task.to_dict()
            data["next_id"] += 1
            return task

        return self._mutate(create_task)

    def get(self, task_id: str) -> SharedTask | None:
        # 按 id 读取单条任务；不存在返回 None。
        data = self._load()
        task_data = data["tasks"].get(task_id)
        return SharedTask.from_dict(task_data) if task_data else None

    def list_tasks(
        self,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[SharedTask]:
        # 列出任务，可选按 status / assignee 过滤。
        data = self._load()
        tasks = [SharedTask.from_dict(t) for t in data["tasks"].values()]
        if status:
            tasks = [t for t in tasks if t.status == status]
        if assignee:
            tasks = [t for t in tasks if t.assignee == assignee]
        return tasks

    def update(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        assignee: str | None = None,
        created_by: str | None = None,
    ) -> SharedTask | None:
        # 增量更新；None 字段跳过，非 None 字段覆盖。task_id 不存在返回 None。
        def update_task(data: dict[str, Any]) -> SharedTask | None:
            task_data = data["tasks"].get(task_id)
            if task_data is None:
                return None
            if title is not None:
                task_data["title"] = title
            if description is not None:
                task_data["description"] = description
            if status is not None:
                task_data["status"] = status
            if assignee is not None:
                task_data["assignee"] = assignee
            if created_by is not None:
                task_data["created_by"] = created_by
            return SharedTask.from_dict(task_data)

        return self._mutate(update_task)

    def add_blocks(self, task_id: str, blocks: list[str]) -> SharedTask | None:
        # 追加 blocks 依赖；去重保证唯一。
        def append_blocks(data: dict[str, Any]) -> SharedTask | None:
            task_data = data["tasks"].get(task_id)
            if task_data is None:
                return None
            existing = list(task_data.get("blocks", []))
            for block in blocks:
                if block not in existing:
                    existing.append(block)
            task_data["blocks"] = existing
            return SharedTask.from_dict(task_data)

        return self._mutate(append_blocks)

    def add_blocked_by(
        self, task_id: str, blocked_by: list[str]
    ) -> SharedTask | None:
        # 追加 blocked_by 反向依赖；去重保证唯一。
        def append_blocked_by(data: dict[str, Any]) -> SharedTask | None:
            task_data = data["tasks"].get(task_id)
            if task_data is None:
                return None
            existing = list(task_data.get("blocked_by", []))
            for block in blocked_by:
                if block not in existing:
                    existing.append(block)
            task_data["blocked_by"] = existing
            return SharedTask.from_dict(task_data)

        return self._mutate(append_blocked_by)
