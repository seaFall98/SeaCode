"""工具注册中心、分批策略与默认工具装配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from seacode.client import ToolCallComplete
from seacode.tools.base import Tool


class ToolRegistry:
    """集中注册、按名查找、启用/禁用工具，并生成双协议 Schema。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        # 第 06 步 MCP 延迟工具搜索会消费此集合；本步六个工具 should_defer=False，不触发。
        self._discovered: set[str] = set()

    # 注册一个工具，按 name 覆盖旧实例。
    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    # 按名查找工具，不存在返回 None。
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # 返回工具是否已注册且未被禁用。
    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    # 启用指定工具。
    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    # 禁用指定工具。
    def disable(self, name: str) -> None:
        if name in self._tools:
            self._disabled.add(name)

    # 启用全部已注册工具。
    def enable_all(self) -> None:
        self._disabled.clear()

    # 标记延迟工具为已发现；第 06 步 MCP 会消费。
    def mark_discovered(self, name: str) -> None:
        self._discovered.add(name)

    # 返回延迟工具是否已被发现。
    def is_discovered(self, name: str) -> bool:
        return name in self._discovered

    # 返回尚未发现的延迟工具名列表；本步六个工具 should_defer=False，返回空。
    def get_deferred_tool_names(self) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if getattr(tool, "should_defer", False)
            and name not in self._discovered
            and name not in self._disabled
        ]

    # 按查询搜索延迟工具并返回 Schema；第 06 步 MCP 会消费。
    def search_deferred(
        self, query: str, max_results: int, protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        query_lower = query.lower()
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self._tools.items():
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            score = 0
            name_lower = name.lower()
            desc_lower = (tool.description or "").lower()
            if query_lower in name_lower:
                score += 10
            if query_lower in desc_lower:
                score += 5
            for word in query_lower.split():
                if word in name_lower:
                    score += 3
                if word in desc_lower:
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for _, _name, tool in scored[:max_results]:
            results.append(_schema_for_protocol(tool, protocol))
        return results

    # 按名精确查找延迟工具并返回 Schema；第 06 步 MCP 会消费。
    def find_deferred_by_names(
        self, names: list[str], protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            if not getattr(tool, "should_defer", False):
                continue
            results.append(_schema_for_protocol(tool, protocol))
        return results

    # 返回全部已注册工具实例。
    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    # 生成当前启用且非延迟的工具 Schema 列表，按协议适配格式。
    def get_all_schemas(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            # 延迟工具未发现前不纳入 Schema 列表；本步六个工具 should_defer=False。
            if getattr(tool, "should_defer", False) and name not in self._discovered:
                continue
            schemas.append(_schema_for_protocol(tool, protocol))
        return schemas


# 按协议把工具 Schema 适配为 Anthropic 或 OpenAI 风格。
def _schema_for_protocol(tool: Tool, protocol: str) -> dict[str, Any]:
    base = tool.get_schema()
    if protocol in ("openai", "openai-compat"):
        return {
            "type": "function",
            "name": base["name"],
            "description": base["description"],
            "parameters": base["input_schema"],
        }
    return base


# 装配六个核心工具的默认注册中心，内部创建并注入 FileStateCache 单例。
def create_default_registry() -> ToolRegistry:
    from seacode.tools.bash import Bash
    from seacode.tools.edit_file import EditFile
    from seacode.tools.file_state_cache import FileStateCache
    from seacode.tools.glob import Glob
    from seacode.tools.grep import Grep
    from seacode.tools.read_file import ReadFile
    from seacode.tools.write_file import WriteFile

    # FileStateCache 作为工具内部安全机制，注入三个文件工具。
    file_state_cache = FileStateCache()

    registry = ToolRegistry()
    registry.register(ReadFile(file_state_cache=file_state_cache))
    registry.register(WriteFile(file_history=None, file_state_cache=file_state_cache))
    registry.register(EditFile(file_history=None, file_state_cache=file_state_cache))
    registry.register(Bash())
    registry.register(Glob())
    registry.register(Grep())
    return registry


# ---------------------------------------------------------------------------
# 工具调用分批策略
# ---------------------------------------------------------------------------


@dataclass
class ToolBatch:
    """一组工具调用及其并发执行策略：concurrent=True 时可 asyncio.gather。"""

    concurrent: bool
    calls: list[ToolCallComplete]


# 按工具的并发安全属性把连续调用切分为可并发批次与独立串行批次。
# 连续的并发安全工具（ReadFile/Glob/Grep）合并到一个 batch；不安全工具各自成独立 batch。
def partition_tool_calls(
    tool_calls: list[ToolCallComplete],
    registry: ToolRegistry,
) -> list[ToolBatch]:
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        safe = (
            tool is not None
            and tool.is_concurrency_safe
            and registry.is_enabled(tc.tool_name)
        )
        # 上一个批次也是并发批次时，把当前并发安全工具合并进去；否则新建批次。
        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


__all__ = ["ToolRegistry", "create_default_registry", "ToolBatch", "partition_tool_calls"]
