"""Hook 数据模型：Action/ActionResult/Hook/HookContext/ToolRejectedError。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from seacode.hooks.conditions import ConditionGroup


@dataclass
class Action:
    """Hook 动作配置；type 决定执行器分发路径，其余字段按类型消费。"""

    type: str
    command: str = ""
    message: str = ""
    url: str = ""
    method: str = "POST"
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    timeout: int = 30


@dataclass
class ActionResult:
    """动作执行结果；output 承载文本输出，success 标识是否成功。"""

    output: str = ""
    success: bool = True


@dataclass
class Hook:
    """单条 Hook 定义；should_run 控制 once 标记后的跳过。"""

    id: str
    event: str
    action: Action
    condition: ConditionGroup | None = None
    reject: bool = False
    once: bool = False
    async_exec: bool = False
    executed: bool = False

    # once=True 且已执行过则跳过；否则允许执行。
    def should_run(self) -> bool:
        if self.once and self.executed:
            return False
        return True

    # 标记已执行；配合 once 控制单次执行语义。
    def mark_executed(self) -> None:
        self.executed = True


@dataclass
class HookContext:
    """Hook 触发上下文；提供字段提取与模板展开能力。"""

    event_name: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""

    # 支持 tool / event / args.<key> 三类字段名；未知字段返回空字符串。
    def get_field(self, name: str) -> str:
        if name == "tool":
            return self.tool_name
        if name == "event":
            return self.event_name
        if name.startswith("args."):
            key = name[5:]
            value = self.tool_args.get(key, "")
            return str(value) if value else ""
        return ""

    # 替换 $EVENT/$TOOL_NAME/$FILE_PATH/$MESSAGE/$ERROR/$TOOL_ARGS.<key> 占位符。
    # 未匹配的占位符保留原样，便于发现配置错误。
    def expand(self, template: str) -> str:
        result = template
        result = result.replace("$EVENT", self.event_name)
        result = result.replace("$TOOL_NAME", self.tool_name)
        result = result.replace("$FILE_PATH", self.file_path)
        result = result.replace("$MESSAGE", self.message)
        result = result.replace("$ERROR", self.error)
        for key, value in self.tool_args.items():
            result = result.replace(f"$TOOL_ARGS.{key}", str(value))
        return result


class ToolRejectedError(Exception):
    """pre_tool_use Hook 拦截工具调用时返回；携带 tool/reason/hook_id 三字段。"""

    def __init__(self, tool: str, reason: str, hook_id: str) -> None:
        self.tool = tool
        self.reason = reason
        self.hook_id = hook_id
        super().__init__(f"Tool '{tool}' rejected by hook '{hook_id}': {reason}")
