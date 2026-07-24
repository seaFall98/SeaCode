"""工具抽象基类、结果模型、分类与共享常量。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

# 文件搜索与 Glob/Grep 默认跳过的目录集合。
SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}
)

# 工具输出截断阈值常量；本步不消费，保留供后续上下文治理步骤使用。
MAX_OUTPUT_CHARS: int = 10000


class ToolCategory(StrEnum):
    """工具分类，服务于权限策略与并发分批。"""

    READ = "read"
    WRITE = "write"
    SYSTEM = "system"


class ToolResult(BaseModel):
    """工具执行的结构化结果，可回灌给模型。"""

    tool_use_id: str = ""
    content: str
    is_error: bool = False


class Tool(ABC):
    """所有工具的统一抽象：名称、描述、参数模型、分类元信息与异步执行入口。"""

    name: str
    description: str
    params_model: type[BaseModel]
    category: ToolCategory = ToolCategory.READ
    # 是否并发安全；第 03 步并发分批会读取此字段。
    is_concurrency_safe: bool = False
    # 是否为系统工具；权限系统据此区分内部工具。
    is_system_tool: bool = False
    # 是否延迟注册；第 06 步 MCP 延迟工具搜索会读取此字段。
    should_defer: bool = False

    @property
    def is_read_only(self) -> bool:
        """返回工具是否只读，供权限系统快速判断。"""
        return self.category == ToolCategory.READ

    # 生成 Anthropic 风格的工具 Schema，OpenAI 风格由注册中心适配。
    def get_schema(self) -> dict[str, Any]:
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult:
        """执行工具并返回结构化结果；异常由调用方捕获转为错误结果。"""
        raise NotImplementedError
