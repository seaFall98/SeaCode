"""子 Agent 系统：定义解析、加载、Fork、后台任务、调用追踪、通知注入、工具过滤。

本子包在第 12 步交付，提供以下能力：

- ``parser`` 把 ``.md`` frontmatter + body 解析为 ``AgentDef``。
- ``loader`` 按项目级、用户级、内置级三级搜索并热重载。
- ``fork`` 复制父对话历史并注入 Fork 提示词。
- ``task_manager`` 后台任务状态机与通知队列。
- ``trace`` 调用链追踪。
- ``notification`` 任务完成通知 XML 格式与注入。
- ``tool_filter`` 五层工具过滤与 Fork 工具克隆。
- ``builtins`` 内置子 Agent ``.md`` 定义。
"""

from __future__ import annotations

from seacode.agents.fork import (
    FORK_BOILERPLATE,
    FORK_QUERY_SOURCE,
    ForkError,
    build_forked_messages,
)
from seacode.agents.loader import AgentLoader
from seacode.agents.notification import format_task_notification, inject_task_notifications
from seacode.agents.parser import (
    VALID_ISOLATION_MODES,
    VALID_PERMISSION_MODES,
    AgentDef,
    AgentParseError,
    parse_agent_file,
    parse_frontmatter,
)
from seacode.agents.task_manager import BackgroundTask, ProgressInfo, TaskManager
from seacode.agents.tool_filter import (
    ALL_AGENT_DISALLOWED_TOOLS,
    ASYNC_AGENT_ALLOWED_TOOLS,
    CUSTOM_AGENT_DISALLOWED_TOOLS,
    clone_registry_for_fork,
    resolve_agent_tools,
)
from seacode.agents.trace import TraceManager, TraceNode

__all__ = [
    "AgentDef",
    "AgentParseError",
    "parse_frontmatter",
    "parse_agent_file",
    "VALID_PERMISSION_MODES",
    "VALID_ISOLATION_MODES",
    "AgentLoader",
    "ForkError",
    "FORK_BOILERPLATE",
    "FORK_QUERY_SOURCE",
    "build_forked_messages",
    "TaskManager",
    "BackgroundTask",
    "ProgressInfo",
    "TraceManager",
    "TraceNode",
    "format_task_notification",
    "inject_task_notifications",
    "resolve_agent_tools",
    "clone_registry_for_fork",
    "ALL_AGENT_DISALLOWED_TOOLS",
    "CUSTOM_AGENT_DISALLOWED_TOOLS",
    "ASYNC_AGENT_ALLOWED_TOOLS",
]
