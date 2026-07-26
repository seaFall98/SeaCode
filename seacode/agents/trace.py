"""调用链追踪：TraceNode 与 TraceManager。

仅内存态，不写入磁盘；Trace 调用图不跨会话合并。``get_tree(trace_id)`` 按
``trace_id`` 聚合返回所有相关节点，供 ``/trace`` 命令递归渲染调用树。
``get_total_tokens`` 合计指定调用链的 input/output token，供成本展示。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class TraceNode:
    """一个 Agent 调用节点；同一 trace_id 的所有节点属于同一调用链。"""

    agent_id: str
    agent_type: str
    parent_id: str | None
    trace_id: str
    status: str = "running"  # running / completed / failed / cancelled
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0


class TraceManager:
    """内存态调用链追踪；trace_id 聚合调用树，agent_id 主键查找节点。"""

    def __init__(self) -> None:
        self._nodes: dict[str, TraceNode] = {}

    # 创建一个 TraceNode；agent_id 由 uuid4 前 12 字符生成。
    def create(
        self, agent_type: str, parent_id: str | None, trace_id: str
    ) -> TraceNode:
        agent_id = uuid4().hex[:12]
        node = TraceNode(
            agent_id=agent_id,
            agent_type=agent_type,
            parent_id=parent_id,
            trace_id=trace_id,
        )
        self._nodes[agent_id] = node
        return node

    # 批量更新节点字段；不存在静默返回。
    def update(self, agent_id: str, **kwargs: Any) -> None:
        node = self._nodes.get(agent_id)
        if node is None:
            return
        for k, v in kwargs.items():
            if hasattr(node, k):
                setattr(node, k, v)

    # 完成节点；设置 end_time 与 status。
    def complete(self, agent_id: str, status: str = "completed") -> None:
        node = self._nodes.get(agent_id)
        if node is None:
            return
        node.end_time = time.monotonic()
        node.status = status

    # 按 agent_id 取出节点。
    def get(self, agent_id: str) -> TraceNode | None:
        return self._nodes.get(agent_id)

    # 按 trace_id 聚合返回所有相关节点。
    def get_tree(self, trace_id: str) -> list[TraceNode]:
        return [n for n in self._nodes.values() if n.trace_id == trace_id]

    # 移除节点；不存在静默返回。
    def remove(self, agent_id: str) -> None:
        self._nodes.pop(agent_id, None)

    # 完成指定父 Agent 的所有 running 子节点；用于父 Agent 提前结束时清理。
    def complete_all_running(self, parent_id: str) -> None:
        for node in self._nodes.values():
            if node.parent_id == parent_id and node.status == "running":
                node.end_time = time.monotonic()
                node.status = "completed"

    # 合计指定调用链的 input/output token。
    def get_total_tokens(self, trace_id: str) -> tuple[int, int]:
        nodes = self.get_tree(trace_id)
        total_in = sum(n.input_tokens for n in nodes)
        total_out = sum(n.output_tokens for n in nodes)
        return total_in, total_out
