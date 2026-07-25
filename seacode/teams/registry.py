# 进程级 Agent 名字注册表：name ↔ agent_id 双向映射，单例 + 线程锁。
"""teams 子包的 AgentNameRegistry 单例。"""

from __future__ import annotations

import threading


class AgentNameRegistry:
    # 进程级单例；双重检查锁定保证线程安全。
    _instance: AgentNameRegistry | None = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> AgentNameRegistry:
        # 双重检查锁定；首次调用创建唯一实例。
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        # name → agent_id 映射；同一 name 重复 register 会覆盖。
        self._name_to_id: dict[str, str] = {}
        self._mu = threading.Lock()

    def register(self, name: str, agent_id: str) -> None:
        # 注册或覆盖 name → agent_id 映射。
        with self._mu:
            self._name_to_id[name] = agent_id

    def resolve(self, name_or_id: str) -> str | None:
        # 接受 name 或 agent_id；命中返回 agent_id，未命中返回 None。
        with self._mu:
            if name_or_id in self._name_to_id:
                return self._name_to_id[name_or_id]
            if name_or_id in self._name_to_id.values():
                return name_or_id
            return None

    def unregister(self, name: str) -> None:
        # 按 name 移除映射；不存在时静默无操作。
        with self._mu:
            self._name_to_id.pop(name, None)

    def reset(self) -> None:
        # 清空映射；主要用于测试间隔离。
        with self._mu:
            self._name_to_id.clear()
