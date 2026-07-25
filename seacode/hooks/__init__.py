"""生命周期 Hook 引擎：事件触发、条件匹配、动作执行、拦截回灌。

子包公开接口统一在此重导出，外部模块从 `seacode.hooks` 导入即可。
"""

from seacode.hooks.conditions import (
    Condition,
    ConditionGroup,
    ConditionParseError,
    parse_condition,
)
from seacode.hooks.engine import HookEngine, HookNotification
from seacode.hooks.events import LifecycleEvent
from seacode.hooks.executors import execute_action
from seacode.hooks.loader import HookConfigError, load_hooks
from seacode.hooks.models import (
    Action,
    ActionResult,
    Hook,
    HookContext,
    ToolRejectedError,
)

__all__ = [
    "Action",
    "ActionResult",
    "Condition",
    "ConditionGroup",
    "ConditionParseError",
    "Hook",
    "HookConfigError",
    "HookContext",
    "HookEngine",
    "HookNotification",
    "LifecycleEvent",
    "ToolRejectedError",
    "execute_action",
    "load_hooks",
    "parse_condition",
]
