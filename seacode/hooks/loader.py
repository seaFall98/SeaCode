"""Hook 配置加载与字段校验：把 list[dict] 解析为 list[Hook]，任一校验失败抛 HookConfigError。"""

from __future__ import annotations

from typing import Any

from seacode.hooks.conditions import ConditionParseError, parse_condition
from seacode.hooks.events import LifecycleEvent
from seacode.hooks.models import Action, Hook

# 合法事件名集合；从 LifecycleEvent 派生，避免硬编码重复。
_VALID_EVENTS: set[str] = {e.value for e in LifecycleEvent}
# 合法动作类型集合；execute_action 按此分发。
_VALID_ACTION_TYPES: set[str] = {"command", "prompt", "http", "agent"}
# 各动作类型的必填字段；缺失即抛 HookConfigError。
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "command": ["command"],
    "prompt": ["message"],
    "http": ["url"],
    "agent": ["prompt"],
}


class HookConfigError(Exception):
    """Hook 配置语法或字段约束错误；由 __main__.py 捕获后打印并 sys.exit(1)。"""


# 生成错误信息中的 Hook 标识；有 id 用 id，否则用序号。
def _identify(entry: dict, index: int) -> str:
    hook_id = entry.get("id", "")
    return f"hook '{hook_id}'" if hook_id else f"hook #{index + 1}"


# 解析 raw_hooks 列表为 list[Hook]；按 event/action dict/action type/必填字段/
# reject 约束/async 约束/once 字段/if 条件/timeout 正整数 顺序校验。
def load_hooks(raw_hooks: list[dict] | None) -> list[Hook]:
    if not raw_hooks:
        return []

    hooks: list[Hook] = []
    for i, entry in enumerate(raw_hooks):
        # entry 必须先校验为 dict，否则 _identify 访问 .get 会抛 AttributeError。
        if not isinstance(entry, dict):
            raise HookConfigError(f"hook #{i + 1}: must be a mapping")
        label = _identify(entry, i)

        # 1. event 字段：必填且必须在 _VALID_EVENTS 中。
        event = entry.get("event")
        if not event:
            raise HookConfigError(f"{label}: missing 'event' field")
        if event not in _VALID_EVENTS:
            raise HookConfigError(
                f"{label}: invalid event '{event}', "
                f"must be one of: {', '.join(sorted(_VALID_EVENTS))}"
            )

        # 2. action 字段：必须是 dict，含 type 字段。
        raw_action = entry.get("action")
        if not isinstance(raw_action, dict):
            raise HookConfigError(f"{label}: missing or invalid 'action' field")

        action_type = raw_action.get("type")
        if action_type not in _VALID_ACTION_TYPES:
            raise HookConfigError(
                f"{label}: invalid action type '{action_type}', "
                f"must be one of: {', '.join(sorted(_VALID_ACTION_TYPES))}"
            )

        # 3. 必填字段：按动作类型校验对应的必填字段。
        for field_name in _REQUIRED_FIELDS[action_type]:
            if not raw_action.get(field_name):
                raise HookConfigError(
                    f"{label}: action type '{action_type}' requires "
                    f"'{field_name}' field"
                )

        # 4. reject 约束：只能配 pre_tool_use。
        reject = entry.get("reject", False)
        if not isinstance(reject, bool):
            raise HookConfigError(f"{label}: 'reject' must be boolean")
        if reject and event != "pre_tool_use":
            raise HookConfigError(
                f"{label}: 'reject' can only be used with 'pre_tool_use' event"
            )

        # 5. async 约束：pre_tool_use 禁止 async（拦截必须同步完成）。
        async_exec = entry.get("async", False)
        if not isinstance(async_exec, bool):
            raise HookConfigError(f"{label}: 'async' must be boolean")
        if async_exec and event == "pre_tool_use":
            raise HookConfigError(
                f"{label}: 'async' cannot be used with 'pre_tool_use' event"
            )

        # 6. once 字段：必须是 bool。
        once = entry.get("once", False)
        if not isinstance(once, bool):
            raise HookConfigError(f"{label}: 'once' must be boolean")

        # 7. if 条件：解析为 ConditionGroup；语法错误转为 HookConfigError。
        condition = None
        raw_if: Any = entry.get("if")
        if raw_if is not None:
            try:
                condition = parse_condition(str(raw_if))
            except ConditionParseError as e:
                raise HookConfigError(f"{label}: condition error: {e}") from e

        # 8. timeout 字段：必须是正整数（bool 不是合法值，因 bool 是 int 子类需显式排除）。
        timeout = raw_action.get("timeout", 30)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise HookConfigError(f"{label}: timeout must be a positive integer")

        # 9. 构造 Action 与 Hook；id 缺失时用 f"{event}_{i}" 派生。
        raw_headers = raw_action.get("headers", {})
        headers = (
            {str(k): str(v) for k, v in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        action = Action(
            type=action_type,
            command=str(raw_action.get("command", "")),
            message=str(raw_action.get("message", "")),
            url=str(raw_action.get("url", "")),
            method=str(raw_action.get("method", "POST")),
            body=str(raw_action.get("body", "")),
            headers=headers,
            prompt=str(raw_action.get("prompt", "")),
            timeout=timeout,
        )
        hook_id = entry.get("id") or f"{event}_{i}"
        hooks.append(
            Hook(
                id=hook_id,
                event=event,
                action=action,
                condition=condition,
                reject=reject,
                once=once,
                async_exec=async_exec,
            )
        )

    return hooks
