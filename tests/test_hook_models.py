"""Hook 数据模型的单元测试：覆盖 Action/ActionResult/Hook 状态机/HookContext/ToolRejectedError。"""

from __future__ import annotations

from seacode.hooks.models import (
    Action,
    ActionResult,
    Hook,
    HookContext,
    ToolRejectedError,
)

# ---------------------------------------------------------------------------
# Action 数据类
# ---------------------------------------------------------------------------


# 验证 Action 九字段默认值与构造传值。
# 构造 Action(type="command") 检查默认值；再构造全字段版本断言所有传入值保留。
def test_action_defaults_match_expected_values() -> None:
    action = Action(type="command")
    assert action.type == "command"
    assert action.command == ""
    assert action.message == ""
    assert action.url == ""
    assert action.method == "POST"
    assert action.body == ""
    assert action.headers == {}
    assert action.prompt == ""
    assert action.timeout == 30


# 验证 Action 构造时传入的字段值被完整保留。
# 逐字段传入非默认值后断言每个字段都持有传入值。
def test_action_constructor_preserves_all_fields() -> None:
    action = Action(
        type="http",
        command="echo",
        message="hi",
        url="http://example.com",
        method="GET",
        body='{"k":1}',
        headers={"X-Tool": "y"},
        prompt="do something",
        timeout=60,
    )
    assert action.type == "http"
    assert action.command == "echo"
    assert action.message == "hi"
    assert action.url == "http://example.com"
    assert action.method == "GET"
    assert action.body == '{"k":1}'
    assert action.headers == {"X-Tool": "y"}
    assert action.prompt == "do something"
    assert action.timeout == 60


# 验证 Action.headers 默认值是独立实例。
# 两个 Action 实例的 headers 字段不应是同一对象，避免共享可变默认值。
def test_action_headers_default_is_independent_instance() -> None:
    a1 = Action(type="command")
    a2 = Action(type="command")
    assert a1.headers is not a2.headers


# ---------------------------------------------------------------------------
# ActionResult 数据类
# ---------------------------------------------------------------------------


# 验证 ActionResult 默认 output 为空字符串、success 为 True。
# 直接构造 ActionResult() 断言默认值。
def test_action_result_defaults() -> None:
    result = ActionResult()
    assert result.output == ""
    assert result.success is True


# 验证 ActionResult 字段保留传入值。
# 构造失败结果断言字段持有。
def test_action_result_preserves_passed_values() -> None:
    result = ActionResult(output="boom", success=False)
    assert result.output == "boom"
    assert result.success is False


# ---------------------------------------------------------------------------
# Hook 数据类与状态机
# ---------------------------------------------------------------------------


def _make_hook(**overrides: object) -> Hook:
    """构造带默认值的 Hook；测试用关键字覆盖具体字段。"""
    defaults: dict[str, object] = {
        "id": "h1",
        "event": "session_start",
        "action": Action(type="prompt", message="m"),
    }
    defaults.update(overrides)
    return Hook(**defaults)  # type: ignore[arg-type]


# 验证 Hook 默认值符合预期。
# 构造最小 Hook 断言 condition/reject/once/async_exec/executed 五个字段默认值。
def test_hook_defaults_match_expected_values() -> None:
    hook = _make_hook()
    assert hook.condition is None
    assert hook.reject is False
    assert hook.once is False
    assert hook.async_exec is False
    assert hook.executed is False


# 验证 should_run 在 once=False 时恒返回 True。
# 构造 once=False 且 executed=True 的 Hook，断言 should_run 仍返回 True。
def test_hook_should_run_returns_true_when_once_false() -> None:
    hook = _make_hook(once=False, executed=True)
    assert hook.should_run() is True


# 验证 should_run 在 once=True + executed=True 时返回 False。
# 构造已执行的 once Hook，断言 should_run 返回 False。
def test_hook_should_run_returns_false_when_once_and_executed() -> None:
    hook = _make_hook(once=True, executed=True)
    assert hook.should_run() is False


# 验证 should_run 在 once=True + executed=False 时返回 True。
# 构造未执行的 once Hook，断言 should_run 返回 True。
def test_hook_should_run_returns_true_when_once_but_not_executed() -> None:
    hook = _make_hook(once=True, executed=False)
    assert hook.should_run() is True


# 验证 mark_executed 把 executed 标记为 True。
# 构造 executed=False 的 Hook，调用 mark_executed 后断言字段为 True。
def test_hook_mark_executed_sets_flag() -> None:
    hook = _make_hook(executed=False)
    hook.mark_executed()
    assert hook.executed is True


# ---------------------------------------------------------------------------
# HookContext.get_field
# ---------------------------------------------------------------------------


# 验证 get_field 支持 tool 字段。
# 构造 tool_name 非空的 ctx，断言 get_field("tool") 返回该值。
def test_get_field_returns_tool_name() -> None:
    ctx = HookContext(tool_name="WriteFile")
    assert ctx.get_field("tool") == "WriteFile"


# 验证 get_field 支持 event 字段。
# 构造 event_name 非空的 ctx，断言 get_field("event") 返回该值。
def test_get_field_returns_event_name() -> None:
    ctx = HookContext(event_name="pre_tool_use")
    assert ctx.get_field("event") == "pre_tool_use"


# 验证 get_field 支持 args.<key> 形式。
# tool_args 含 file_path 字段，断言 get_field("args.file_path") 返回对应值。
def test_get_field_returns_args_value() -> None:
    ctx = HookContext(tool_args={"file_path": "/tmp/a.json"})
    assert ctx.get_field("args.file_path") == "/tmp/a.json"


# 验证 get_field 中 args.<key> 不存在时返回空字符串。
# tool_args 为空 dict，断言 get_field("args.missing") 返回 ""。
def test_get_field_returns_empty_for_missing_args_key() -> None:
    ctx = HookContext(tool_args={})
    assert ctx.get_field("args.missing") == ""


# 验证 get_field 对未知字段返回空字符串。
# 构造默认 ctx，断言 get_field("unknown") 返回 ""。
def test_get_field_returns_empty_for_unknown_field() -> None:
    ctx = HookContext()
    assert ctx.get_field("unknown") == ""


# ---------------------------------------------------------------------------
# HookContext.expand
# ---------------------------------------------------------------------------


# 验证 expand 替换 $EVENT 占位符。
# 构造 event_name 的 ctx，断言模板中 $EVENT 被替换。
def test_expand_replaces_event_placeholder() -> None:
    ctx = HookContext(event_name="pre_tool_use")
    assert ctx.expand("event=$EVENT") == "event=pre_tool_use"


# 验证 expand 替换 $TOOL_NAME 占位符。
# 构造 tool_name 的 ctx，断言模板中 $TOOL_NAME 被替换。
def test_expand_replaces_tool_name_placeholder() -> None:
    ctx = HookContext(tool_name="WriteFile")
    assert ctx.expand("tool=$TOOL_NAME") == "tool=WriteFile"


# 验证 expand 替换 $FILE_PATH 占位符。
# 构造 file_path 的 ctx，断言模板中 $FILE_PATH 被替换。
def test_expand_replaces_file_path_placeholder() -> None:
    ctx = HookContext(file_path="/tmp/a.json")
    assert ctx.expand("file=$FILE_PATH") == "file=/tmp/a.json"


# 验证 expand 替换 $MESSAGE 占位符。
# 构造 message 的 ctx，断言模板中 $MESSAGE 被替换。
def test_expand_replaces_message_placeholder() -> None:
    ctx = HookContext(message="hi")
    assert ctx.expand("msg=$MESSAGE") == "msg=hi"


# 验证 expand 替换 $ERROR 占位符。
# 构造 error 的 ctx，断言模板中 $ERROR 被替换。
def test_expand_replaces_error_placeholder() -> None:
    ctx = HookContext(error="boom")
    assert ctx.expand("err=$ERROR") == "err=boom"


# 验证 expand 替换 $TOOL_ARGS.<key> 占位符。
# tool_args 含 file_path，断言模板中 $TOOL_ARGS.file_path 被替换。
def test_expand_replaces_tool_args_placeholder() -> None:
    ctx = HookContext(tool_args={"file_path": "/tmp/a.json"})
    assert ctx.expand("path=$TOOL_ARGS.file_path") == "path=/tmp/a.json"


# 验证 expand 对未匹配占位符保留原样。
# 构造默认 ctx，断言 $UNKNOWN 不被替换。
def test_expand_preserves_unknown_placeholder() -> None:
    ctx = HookContext()
    assert ctx.expand("$UNKNOWN") == "$UNKNOWN"


# 验证 expand 同时替换多个占位符。
# 构造带 event 和 tool 的 ctx，断言模板中两个占位符都被替换。
def test_expand_replaces_multiple_placeholders() -> None:
    ctx = HookContext(event_name="e", tool_name="t")
    assert ctx.expand("$EVENT $TOOL_NAME") == "e t"


# ---------------------------------------------------------------------------
# ToolRejectedError
# ---------------------------------------------------------------------------


# 验证 ToolRejectedError 持有三字段。
# 构造错误对象断言 tool/reason/hook_id 字段值。
def test_tool_rejected_error_holds_three_fields() -> None:
    err = ToolRejectedError(tool="WriteFile", reason="blocked", hook_id="hook1")
    assert err.tool == "WriteFile"
    assert err.reason == "blocked"
    assert err.hook_id == "hook1"


# 验证 ToolRejectedError 字符串形式含 tool/hook_id/reason。
# 构造错误对象断言 str(err) 含三字段内容。
def test_tool_rejected_error_string_contains_fields() -> None:
    err = ToolRejectedError(tool="WriteFile", reason="blocked", hook_id="hook1")
    s = str(err)
    assert "WriteFile" in s
    assert "hook1" in s
    assert "blocked" in s
