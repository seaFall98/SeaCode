"""Hook 配置加载器单元测试：覆盖 load_hooks 各类校验路径与 id 派生。"""

from __future__ import annotations

import pytest

from seacode.hooks.events import LifecycleEvent
from seacode.hooks.loader import HookConfigError, load_hooks

# ---------------------------------------------------------------------------
# 空输入
# ---------------------------------------------------------------------------


# 验证 load_hooks 对 None 输入返回空列表。
# None 视为未配置，返回空 list 不抛异常。
def test_load_hooks_none_returns_empty_list() -> None:
    assert load_hooks(None) == []


# 验证 load_hooks 对空列表输入返回空列表。
# 空 list 等价于未配置任何 Hook。
def test_load_hooks_empty_list_returns_empty_list() -> None:
    assert load_hooks([]) == []


# ---------------------------------------------------------------------------
# 合法 Hook 各类型
# ---------------------------------------------------------------------------


# 验证合法 command 类型 Hook 被解析为 Hook 对象。
# event + action(type=command, command=echo) 输入返回 Hook，字段保留。
def test_load_hooks_valid_command_hook() -> None:
    raw = [
        {
            "id": "h1",
            "event": "session_start",
            "action": {"type": "command", "command": "echo hi"},
        }
    ]
    hooks = load_hooks(raw)
    assert len(hooks) == 1
    assert hooks[0].id == "h1"
    assert hooks[0].event == "session_start"
    assert hooks[0].action.type == "command"
    assert hooks[0].action.command == "echo hi"


# 验证合法 prompt 类型 Hook 被解析为 Hook 对象。
# action type=prompt + message 输入返回 Hook，message 字段保留。
def test_load_hooks_valid_prompt_hook() -> None:
    raw = [
        {
            "id": "h1",
            "event": "session_start",
            "action": {"type": "prompt", "message": "hi"},
        }
    ]
    hooks = load_hooks(raw)
    assert len(hooks) == 1
    assert hooks[0].action.type == "prompt"
    assert hooks[0].action.message == "hi"


# 验证合法 http 类型 Hook 被解析为 Hook 对象。
# action type=http + url 输入返回 Hook，url 字段保留。
def test_load_hooks_valid_http_hook() -> None:
    raw = [
        {
            "id": "h1",
            "event": "session_start",
            "action": {"type": "http", "url": "http://example.com"},
        }
    ]
    hooks = load_hooks(raw)
    assert len(hooks) == 1
    assert hooks[0].action.type == "http"
    assert hooks[0].action.url == "http://example.com"


# 验证合法 agent 类型 Hook 被解析为 Hook 对象。
# action type=agent + prompt 输入返回 Hook，prompt 字段保留。
def test_load_hooks_valid_agent_hook() -> None:
    raw = [
        {
            "id": "h1",
            "event": "session_start",
            "action": {"type": "agent", "prompt": "do something"},
        }
    ]
    hooks = load_hooks(raw)
    assert len(hooks) == 1
    assert hooks[0].action.type == "agent"
    assert hooks[0].action.prompt == "do something"


# 验证合法 if 条件被解析为 ConditionGroup。
# pre_tool_use + reject + if 条件输入返回 Hook，condition 非 None。
def test_load_hooks_valid_condition_and_reject() -> None:
    raw = [
        {
            "id": "h1",
            "event": "pre_tool_use",
            "if": 'tool == "WriteFile"',
            "reject": True,
            "action": {"type": "prompt", "message": "blocked"},
        }
    ]
    hooks = load_hooks(raw)
    assert len(hooks) == 1
    assert hooks[0].condition is not None
    assert len(hooks[0].condition.conditions) == 1
    assert hooks[0].reject is True


# 验证多条 Hook 全部被解析。
# 三条合法 Hook 输入返回三个 Hook 对象。
def test_load_hooks_multiple_hooks_all_parsed() -> None:
    raw = [
        {
            "id": "h1",
            "event": "session_start",
            "action": {"type": "prompt", "message": "a"},
        },
        {
            "id": "h2",
            "event": "turn_start",
            "action": {"type": "prompt", "message": "b"},
        },
        {
            "id": "h3",
            "event": "turn_end",
            "action": {"type": "prompt", "message": "c"},
        },
    ]
    hooks = load_hooks(raw)
    assert len(hooks) == 3
    assert [h.id for h in hooks] == ["h1", "h2", "h3"]


# ---------------------------------------------------------------------------
# event 字段校验
# ---------------------------------------------------------------------------


# 验证缺 event 字段抛 HookConfigError。
# raw 缺 event 字段时抛错，错误信息含 missing 'event'。
def test_load_hooks_missing_event_raises() -> None:
    raw = [{"id": "h1", "action": {"type": "prompt", "message": "hi"}}]
    with pytest.raises(HookConfigError, match="missing 'event'"):
        load_hooks(raw)


# 验证未知 event 抛 HookConfigError。
# event 值不在 _VALID_EVENTS 中时抛错。
def test_load_hooks_invalid_event_raises() -> None:
    raw = [
        {
            "event": "unknown_event",
            "action": {"type": "prompt", "message": "hi"},
        }
    ]
    with pytest.raises(HookConfigError, match="invalid event"):
        load_hooks(raw)


# 验证全部 15 个合法 event 都被接受。
# 分别构造 15 条 Hook 使用全部 event 名，断言全部解析成功。
def test_load_hooks_all_valid_events_accepted() -> None:
    raw = [
        {
            "id": f"h_{e.value}",
            "event": e.value,
            "action": {"type": "prompt", "message": "m"},
        }
        for e in LifecycleEvent
    ]
    hooks = load_hooks(raw)
    assert len(hooks) == 15
    assert {h.event for h in hooks} == {e.value for e in LifecycleEvent}


# ---------------------------------------------------------------------------
# action 字段校验
# ---------------------------------------------------------------------------


# 验证缺 action 字段抛 HookConfigError。
# raw 缺 action 字段时抛错，错误信息含 missing or invalid 'action'。
def test_load_hooks_missing_action_raises() -> None:
    raw = [{"event": "session_start"}]
    with pytest.raises(HookConfigError, match="missing or invalid 'action'"):
        load_hooks(raw)


# 验证 action 非 dict 抛 HookConfigError。
# action 为字符串时抛错。
def test_load_hooks_action_not_dict_raises() -> None:
    raw = [{"event": "session_start", "action": "not dict"}]
    with pytest.raises(HookConfigError):
        load_hooks(raw)


# 验证非法 action type 抛 HookConfigError。
# action.type 为 unknown 时抛错。
def test_load_hooks_invalid_action_type_raises() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "unknown", "message": "hi"},
        }
    ]
    with pytest.raises(HookConfigError, match="invalid action type"):
        load_hooks(raw)


# 验证 command 缺 command 字段抛 HookConfigError。
# type=command 但无 command 字段时抛错。
def test_load_hooks_command_missing_field_raises() -> None:
    raw = [{"event": "session_start", "action": {"type": "command"}}]
    with pytest.raises(HookConfigError, match="requires 'command'"):
        load_hooks(raw)


# 验证 prompt 缺 message 字段抛 HookConfigError。
# type=prompt 但无 message 字段时抛错。
def test_load_hooks_prompt_missing_field_raises() -> None:
    raw = [{"event": "session_start", "action": {"type": "prompt"}}]
    with pytest.raises(HookConfigError, match="requires 'message'"):
        load_hooks(raw)


# 验证 http 缺 url 字段抛 HookConfigError。
# type=http 但无 url 字段时抛错。
def test_load_hooks_http_missing_field_raises() -> None:
    raw = [{"event": "session_start", "action": {"type": "http"}}]
    with pytest.raises(HookConfigError, match="requires 'url'"):
        load_hooks(raw)


# 验证 agent 缺 prompt 字段抛 HookConfigError。
# type=agent 但无 prompt 字段时抛错。
def test_load_hooks_agent_missing_field_raises() -> None:
    raw = [{"event": "session_start", "action": {"type": "agent"}}]
    with pytest.raises(HookConfigError, match="requires 'prompt'"):
        load_hooks(raw)


# ---------------------------------------------------------------------------
# reject / async / timeout 约束
# ---------------------------------------------------------------------------


# 验证 reject 与非 pre_tool_use event 组合抛 HookConfigError。
# reject=True 但 event=session_start 时抛错。
def test_load_hooks_reject_with_non_pre_tool_raises() -> None:
    raw = [
        {
            "event": "session_start",
            "reject": True,
            "action": {"type": "prompt", "message": "hi"},
        }
    ]
    with pytest.raises(HookConfigError, match="reject.*pre_tool_use"):
        load_hooks(raw)


# 验证 reject 与 pre_tool_use 合法组合。
# reject=True + event=pre_tool_use 时解析成功，hooks[0].reject is True。
def test_load_hooks_reject_with_pre_tool_use_is_valid() -> None:
    raw = [
        {
            "event": "pre_tool_use",
            "reject": True,
            "action": {"type": "prompt", "message": "blocked"},
        }
    ]
    hooks = load_hooks(raw)
    assert hooks[0].reject is True


# 验证 async 与 pre_tool_use event 组合抛 HookConfigError。
# async=True + event=pre_tool_use 时抛错。
def test_load_hooks_async_with_pre_tool_use_raises() -> None:
    raw = [
        {
            "event": "pre_tool_use",
            "async": True,
            "action": {"type": "prompt", "message": "hi"},
        }
    ]
    with pytest.raises(HookConfigError, match="async.*pre_tool_use"):
        load_hooks(raw)


# 验证 async 与非 pre_tool_use 合法组合。
# async=True + event=session_start 时解析成功，async_exec 字段为 True。
def test_load_hooks_async_with_non_pre_tool_use_is_valid() -> None:
    raw = [
        {
            "event": "session_start",
            "async": True,
            "action": {"type": "command", "command": "echo hi"},
        }
    ]
    hooks = load_hooks(raw)
    assert hooks[0].async_exec is True


# 验证 timeout=0 抛 HookConfigError。
# timeout 非正整数时抛错。
def test_load_hooks_timeout_zero_raises() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "command", "command": "echo", "timeout": 0},
        }
    ]
    with pytest.raises(HookConfigError, match="timeout must be a positive integer"):
        load_hooks(raw)


# 验证 timeout=-1 抛 HookConfigError。
# timeout 负数时抛错。
def test_load_hooks_timeout_negative_raises() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "command", "command": "echo", "timeout": -1},
        }
    ]
    with pytest.raises(HookConfigError, match="timeout must be a positive integer"):
        load_hooks(raw)


# 验证 timeout 非整数抛 HookConfigError。
# timeout 为字符串时抛错。
def test_load_hooks_timeout_string_raises() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "command", "command": "echo", "timeout": "30"},
        }
    ]
    with pytest.raises(HookConfigError, match="timeout must be a positive integer"):
        load_hooks(raw)


# 验证 timeout 为 bool 抛 HookConfigError（bool 是 int 子类需显式排除）。
# timeout=True 时抛错。
def test_load_hooks_timeout_bool_raises() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "command", "command": "echo", "timeout": True},
        }
    ]
    with pytest.raises(HookConfigError, match="timeout must be a positive integer"):
        load_hooks(raw)


# 验证 timeout 为浮点数抛 HookConfigError。
# timeout=1.5 时抛错。
def test_load_hooks_timeout_float_raises() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "command", "command": "echo", "timeout": 1.5},
        }
    ]
    with pytest.raises(HookConfigError, match="timeout must be a positive integer"):
        load_hooks(raw)


# 验证 timeout 正整数合法。
# timeout=60 时解析成功且字段保留。
def test_load_hooks_timeout_positive_integer_is_valid() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "command", "command": "echo hi", "timeout": 60},
        }
    ]
    hooks = load_hooks(raw)
    assert hooks[0].action.timeout == 60


# 验证 timeout 默认 30。
# 不传 timeout 时解析成功且字段为 30。
def test_load_hooks_timeout_default_is_thirty() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "command", "command": "echo hi"},
        }
    ]
    hooks = load_hooks(raw)
    assert hooks[0].action.timeout == 30


# ---------------------------------------------------------------------------
# if 条件与 id 派生
# ---------------------------------------------------------------------------


# 验证 if 解析失败抛 HookConfigError。
# && 与 || 混用导致 ConditionParseError 时转为 HookConfigError。
def test_load_hooks_invalid_condition_raises() -> None:
    raw = [
        {
            "event": "session_start",
            "if": "tool == 'WriteFile' || tool == 'EditFile' && tool == 'ReadFile'",
            "action": {"type": "prompt", "message": "hi"},
        }
    ]
    with pytest.raises(HookConfigError, match="condition error"):
        load_hooks(raw)


# 验证合法 if 条件解析为 ConditionGroup。
# if='tool == "WriteFile"' 时解析成功，condition 非 None。
def test_load_hooks_valid_condition_parsed() -> None:
    raw = [
        {
            "event": "pre_tool_use",
            "if": 'tool == "WriteFile"',
            "action": {"type": "prompt", "message": "blocked"},
        }
    ]
    hooks = load_hooks(raw)
    assert hooks[0].condition is not None
    assert len(hooks[0].condition.conditions) == 1


# 验证缺 id 时从 event+index 派生。
# 无 id 字段时 hook_id 为 f"{event}_{i}"。
def test_load_hooks_derives_id_when_missing() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "prompt", "message": "hi"},
        }
    ]
    hooks = load_hooks(raw)
    assert hooks[0].id == "session_start_0"


# 验证缺 id 时多条 Hook 派生不同 id。
# 两条相同 event 的 Hook 派生 _0 和 _1。
def test_load_hooks_derives_unique_ids_for_multiple_hooks() -> None:
    raw = [
        {
            "event": "session_start",
            "action": {"type": "prompt", "message": "a"},
        },
        {
            "event": "session_start",
            "action": {"type": "prompt", "message": "b"},
        },
    ]
    hooks = load_hooks(raw)
    assert hooks[0].id == "session_start_0"
    assert hooks[1].id == "session_start_1"
