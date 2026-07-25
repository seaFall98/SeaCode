# 团队级共享任务板：单一 JSON 文件持久化，存储任务与自增 ID。
"""teams 子包的共享任务存储。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


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
        self._path = Path(path)

    def _load(self) -> dict:
        # 读取 JSON；不存在或损坏时返回空结构。
        if not self._path.exists():
            return {"next_id": 1, "tasks": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"next_id": 1, "tasks": {}}
            if "tasks" not in data:
                data["tasks"] = {}
            if "next_id" not in data:
                data["next_id"] = 1
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning("failed to load tasks: %s", e)
            return {"next_id": 1, "tasks": {}}

    def _save(self, data: dict) -> None:
        # 原子写入；ensure_ascii=False 保留中文。
        self._path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def init_empty(self) -> None:
        # 初始化空任务板；用于 create_team 时为新团队开板。
        self._save({"next_id": 1, "tasks": {}})

    def create(
        self,
        title: str,
        description: str = "",
        created_by: str = "",
        assignee: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> SharedTask:
        # 创建任务并自增 ID；持久化到磁盘。
        data = self._load()
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
        self._save(data)
        return task

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
        data = self._load()
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
        self._save(data)
        return SharedTask.from_dict(task_data)

    def add_blocks(self, task_id: str, blocks: list[str]) -> SharedTask | None:
        # 追加 blocks 依赖；去重保证唯一。
        data = self._load()
        task_data = data["tasks"].get(task_id)
        if task_data is None:
            return None
        existing = list(task_data.get("blocks", []))
        for b in blocks:
            if b not in existing:
                existing.append(b)
        task_data["blocks"] = existing
        self._save(data)
        return SharedTask.from_dict(task_data)

    def add_blocked_by(
        self, task_id: str, blocked_by: list[str]
    ) -> SharedTask | None:
        # 追加 blocked_by 反向依赖；去重保证唯一。
        data = self._load()
        task_data = data["tasks"].get(task_id)
        if task_data is None:
            return None
        existing = list(task_data.get("blocked_by", []))
        for b in blocked_by:
            if b not in existing:
                existing.append(b)
        task_data["blocked_by"] = existing
        self._save(data)
        return SharedTask.from_dict(task_data)
