# 跨进程邮箱：单文件 JSON inbox + 文件锁互斥；每 agent 一个 inbox 文件。
"""teams 子包的跨进程邮箱实现。"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# stale 锁阈值（秒）：超过此时间未释放视为僵尸锁，可被其它写入者接管。
STALE_LOCK_SECONDS: int = 10

# 锁获取重试次数。
LOCK_RETRY_COUNT: int = 10

# 锁重试退避范围（毫秒）。
LOCK_RETRY_MIN_MS: int = 5
LOCK_RETRY_MAX_MS: int = 100


@dataclass
class MailboxMessage:
    # 邮箱中的一条消息；message_type 取 text / shutdown_request / shutdown_response。
    id: str
    from_agent: str
    to_agent: str
    content: str
    summary: str
    message_type: str
    timestamp: float
    read: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        # 序列化所有字段供持久化与传输。
        return {
            "id": self.id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "content": self.content,
            "summary": self.summary,
            "message_type": self.message_type,
            "timestamp": self.timestamp,
            "read": self.read,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MailboxMessage:
        # 反序列化；忽略未知键以兼容历史格式。
        return cls(
            id=data["id"],
            from_agent=data["from_agent"],
            to_agent=data["to_agent"],
            content=data["content"],
            summary=data.get("summary", ""),
            message_type=data.get("message_type", "text"),
            timestamp=data.get("timestamp", 0.0),
            read=data.get("read", False),
            metadata=data.get("metadata", {}),
        )


# 工厂函数：生成 12 位 id 与当前 timestamp 的新消息。
def create_message(
    from_agent: str,
    to_agent: str,
    content: str,
    summary: str,
    message_type: str = "text",
    metadata: dict | None = None,
) -> MailboxMessage:
    return MailboxMessage(
        id=uuid.uuid4().hex[:12],
        from_agent=from_agent,
        to_agent=to_agent,
        content=content,
        summary=summary,
        message_type=message_type,
        timestamp=time.time(),
        metadata=metadata or {},
    )


class Mailbox:
    # 每个 agent 一个 <agent_id>.json inbox 文件，配 .json.lock 文件锁。
    def __init__(self, mailbox_dir: str | Path) -> None:
        self._dir = Path(mailbox_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # 进程内互斥锁：与文件锁配合保证同进程多线程安全。
        self._mu = threading.Lock()

    def _lock_path(self, agent_id: str) -> Path:
        # 文件锁路径：与 inbox 同目录的 <agent_id>.json.lock。
        return self._dir / f"{agent_id}.json.lock"

    def _inbox_path(self, agent_id: str) -> Path:
        # inbox 路径：单文件 JSON 数组。
        return self._dir / f"{agent_id}.json"

    # 在文件锁保护下读取 → 变换 → 写回；fn 返回新的 inbox 列表。
    def _with_lock(
        self, agent_id: str, fn: Callable[[list[dict]], list[dict]]
    ) -> Any:
        with self._mu:
            lock_file = self._lock_path(agent_id)
            acquired = False
            for _ in range(LOCK_RETRY_COUNT):
                try:
                    fd = os.open(
                        str(lock_file),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o644,
                    )
                    os.close(fd)
                    acquired = True
                    break
                except FileExistsError:
                    # 锁已存在；检查是否 stale。
                    try:
                        mtime = lock_file.stat().st_mtime
                        if time.time() - mtime > STALE_LOCK_SECONDS:
                            log.warning(
                                "stale lock detected for %s, taking over",
                                agent_id,
                            )
                            lock_file.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    time.sleep(
                        random.uniform(
                            LOCK_RETRY_MIN_MS / 1000, LOCK_RETRY_MAX_MS / 1000
                        )
                    )
            if not acquired:
                raise OSError(
                    f"failed to acquire mailbox lock for {agent_id} "
                    f"after {LOCK_RETRY_COUNT} retries"
                )
            try:
                inbox = self._read_inbox(agent_id)
                new_inbox = fn(inbox)
                self._write_inbox(agent_id, new_inbox)
                return new_inbox
            finally:
                try:
                    lock_file.unlink(missing_ok=True)
                except OSError:
                    pass

    def _read_inbox(self, agent_id: str) -> list[dict]:
        # 读取 inbox 文件；不存在或损坏时返回空列表。
        path = self._inbox_path(agent_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            return data
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning("failed to read inbox %s: %s", agent_id, e)
            return []

    def _write_inbox(self, agent_id: str, data: list[dict]) -> None:
        # 原子写入 inbox；ensure_ascii=False 保留中文可读性。
        self._inbox_path(agent_id).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def write(self, agent_id: str, message: MailboxMessage) -> None:
        # 在文件锁保护下追加消息；强制 read=False、刷新 timestamp。

        def append_fn(inbox: list[dict]) -> list[dict]:
            msg_dict = message.to_dict()
            msg_dict["read"] = False
            msg_dict["timestamp"] = message.timestamp
            inbox.append(msg_dict)
            return inbox

        self._with_lock(agent_id, append_fn)

    def read(self, agent_id: str) -> list[MailboxMessage]:
        # 只读未读消息，不修改状态。
        with self._mu:
            inbox = self._read_inbox(agent_id)
            return [
                MailboxMessage.from_dict(m) for m in inbox if not m.get("read", False)
            ]

    # 消费未读消息并标记为已读；用 _with_lock 保证原子性。
    def consume(self, agent_id: str) -> list[MailboxMessage]:
        def mark_read_fn(inbox: list[dict]) -> list[dict]:
            for m in inbox:
                if not m.get("read", False):
                    m["read"] = True
            return inbox

        with self._mu:
            lock_file = self._lock_path(agent_id)
            acquired = False
            for _ in range(LOCK_RETRY_COUNT):
                try:
                    fd = os.open(
                        str(lock_file),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o644,
                    )
                    os.close(fd)
                    acquired = True
                    break
                except FileExistsError:
                    try:
                        mtime = lock_file.stat().st_mtime
                        if time.time() - mtime > STALE_LOCK_SECONDS:
                            lock_file.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    time.sleep(
                        random.uniform(
                            LOCK_RETRY_MIN_MS / 1000, LOCK_RETRY_MAX_MS / 1000
                        )
                    )
            if not acquired:
                # consume 锁获取失败时返回空列表，避免阻塞 lead 轮询。
                return []
            try:
                inbox = self._read_inbox(agent_id)
                unread = [m for m in inbox if not m.get("read", False)]
                for m in unread:
                    m["read"] = True
                self._write_inbox(agent_id, inbox)
                return [MailboxMessage.from_dict(m) for m in unread]
            finally:
                try:
                    lock_file.unlink(missing_ok=True)
                except OSError:
                    pass

    # 向多个收件人广播同一条消息；exclude 指定的收件人跳过。
    def broadcast(
        self,
        message: MailboxMessage,
        recipients: list[str],
        exclude: str | None = None,
    ) -> None:
        for recipient in recipients:
            if recipient == exclude:
                continue
            self.write(recipient, message)

    def cleanup(self, agent_id: str) -> None:
        # 删除单个 agent 的 inbox 与 lock 文件。
        self._inbox_path(agent_id).unlink(missing_ok=True)
        self._lock_path(agent_id).unlink(missing_ok=True)

    def cleanup_all(self) -> None:
        # 清空目录下所有 .json 与 .json.lock 文件。
        if not self._dir.exists():
            return
        for f in self._dir.iterdir():
            if f.name.endswith(".json") or f.name.endswith(".json.lock"):
                f.unlink(missing_ok=True)
