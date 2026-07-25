"""生命周期事件枚举：按会话/轮次/工具/消息/系统五组定义 Hook 触发点。"""

from __future__ import annotations

from enum import StrEnum


class LifecycleEvent(StrEnum):
    """Hook 生命周期事件常量；StrEnum 兼容字符串比较，便于配置文件直填。"""

    # 会话级：Agent.run 开始与结束各触发一次。
    SESSION_START = "session_start"
    SESSION_END = "session_end"

    # 轮次级：每轮迭代开头与结束各触发一次。
    TURN_START = "turn_start"
    TURN_END = "turn_end"

    # 工具级：pre_tool_use 走专用拦截入口支持 reject；post_tool_use 在工具执行后触发。
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"

    # 消息级：LLM 调用前与响应后各触发一次。
    PRE_SEND = "pre_send"
    POST_RECEIVE = "post_receive"

    # 系统级：startup/shutdown 由 App 启动关闭触发；其余事件保留常量供后续步骤接入。
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    ERROR = "error"
    COMPACT = "compact"
    PERMISSION_REQUEST = "permission_request"
    FILE_CHANGE = "file_change"
    COMMAND_EXECUTE = "command_execute"
