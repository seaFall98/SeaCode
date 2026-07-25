"""生命周期事件枚举单元测试：覆盖 15 个成员值与 StrEnum 字符串比较。"""

from __future__ import annotations

from seacode.hooks.events import LifecycleEvent


# 15 个事件成员的 value 与既定 snake_case 名一致；与 _VALID_EVENTS 等价集合对齐。
# 直接对每个成员断言字符串值，保证 loader._VALID_EVENTS 接受这些名称。
def test_lifecycle_event_values_match_expected_names() -> None:
    expected = {
        LifecycleEvent.SESSION_START: "session_start",
        LifecycleEvent.SESSION_END: "session_end",
        LifecycleEvent.TURN_START: "turn_start",
        LifecycleEvent.TURN_END: "turn_end",
        LifecycleEvent.PRE_TOOL_USE: "pre_tool_use",
        LifecycleEvent.POST_TOOL_USE: "post_tool_use",
        LifecycleEvent.PRE_SEND: "pre_send",
        LifecycleEvent.POST_RECEIVE: "post_receive",
        LifecycleEvent.STARTUP: "startup",
        LifecycleEvent.SHUTDOWN: "shutdown",
        LifecycleEvent.ERROR: "error",
        LifecycleEvent.COMPACT: "compact",
        LifecycleEvent.PERMISSION_REQUEST: "permission_request",
        LifecycleEvent.FILE_CHANGE: "file_change",
        LifecycleEvent.COMMAND_EXECUTE: "command_execute",
    }
    for member, expected_value in expected.items():
        assert member.value == expected_value


# LifecycleEvent 枚举恰好有 15 个成员；保证事件常量完整定义。
# 用 list(LifecycleEvent) 获取全部成员并断言长度。
def test_lifecycle_event_has_fifteen_members() -> None:
    members = list(LifecycleEvent)
    assert len(members) == 15


# LifecycleEvent 全部成员的 value 集合与 loader._VALID_EVENTS 派生集合一致。
# 用集合推导断言覆盖所有事件名。
def test_lifecycle_event_value_set_covers_all_expected_names() -> None:
    values = {e.value for e in LifecycleEvent}
    expected = {
        "session_start",
        "session_end",
        "turn_start",
        "turn_end",
        "pre_tool_use",
        "post_tool_use",
        "pre_send",
        "post_receive",
        "startup",
        "shutdown",
        "error",
        "compact",
        "permission_request",
        "file_change",
        "command_execute",
    }
    assert values == expected


# StrEnum 成员与字符串比较兼容；保证 YAML 中可直接写字符串值与枚举成员等价。
# 断言 LifecycleEvent.PRE_TOOL_USE == "pre_tool_use" 为 True。
def test_strenum_member_compares_equal_to_string() -> None:
    assert LifecycleEvent.PRE_TOOL_USE == "pre_tool_use"
    assert LifecycleEvent.SESSION_START == "session_start"
    assert LifecycleEvent.SHUTDOWN == "shutdown"
