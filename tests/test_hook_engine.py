"""HookEngine 核心引擎单元测试：覆盖匹配、运行、拦截与状态取出。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from seacode.hooks.engine import HookEngine, HookNotification
from seacode.hooks.models import Action, ActionResult, Hook, HookContext, ToolRejectedError


def _make_hook(
    *,
    id: str = "h1",
    event: str = "session_start",
    action_type: str = "prompt",
    once: bool = False,
    executed: bool = False,
    async_exec: bool = False,
    reject: bool = False,
    message: str = "m",
    command: str = "",
) -> Hook:
    """构造带默认值的 Hook；测试用关键字覆盖具体字段。"""
    if action_type == "command":
        action = Action(type="command", command=command or "echo hi")
    else:
        action = Action(type=action_type, message=message)
    return Hook(
        id=id,
        event=event,
        action=action,
        once=once,
        executed=executed,
        async_exec=async_exec,
        reject=reject,
    )


# ---------------------------------------------------------------------------
# find_matching_hooks
# ---------------------------------------------------------------------------


# 验证 find_matching_hooks 按 event 过滤命中。
# 三个 Hook 两个 session_start 一个 turn_start，断言只返回两个。
async def test_find_matching_hooks_filters_by_event() -> None:
    hooks = [
        _make_hook(id="h1", event="session_start"),
        _make_hook(id="h2", event="turn_start"),
        _make_hook(id="h3", event="session_start"),
    ]
    engine = HookEngine(hooks=hooks)
    matched = engine.find_matching_hooks("session_start", HookContext())
    assert {h.id for h in matched} == {"h1", "h3"}


# 验证 once=True + executed=True 的 Hook 被 find_matching_hooks 跳过。
# should_run 返回 False 时 find_matching_hooks 不返回该 Hook。
async def test_find_matching_hooks_skips_executed_once_hook() -> None:
    hooks = [
        _make_hook(id="h1", event="session_start", once=True, executed=True),
        _make_hook(id="h2", event="session_start"),
    ]
    engine = HookEngine(hooks=hooks)
    matched = engine.find_matching_hooks("session_start", HookContext())
    assert [h.id for h in matched] == ["h2"]


# 验证 find_matching_hooks 按 condition 过滤不匹配的 Hook。
# Hook 带 condition 且 evaluate 返回 False 时不被返回。
async def test_find_matching_hooks_filters_by_condition() -> None:
    from seacode.hooks.conditions import Condition, ConditionGroup

    cg = ConditionGroup(
        conditions=[Condition(field="tool", operator="==", value="WriteFile")]
    )
    hooks = [
        _make_hook(id="h1", event="pre_tool_use"),
        Hook(
            id="h2",
            event="pre_tool_use",
            action=Action(type="prompt", message="m"),
            condition=cg,
        ),
    ]
    engine = HookEngine(hooks=hooks)
    # tool_name 不匹配 condition，h2 被过滤。
    matched = engine.find_matching_hooks(
        "pre_tool_use", HookContext(tool_name="ReadFile")
    )
    assert [h.id for h in matched] == ["h1"]
    # tool_name 匹配时 h2 也命中。
    matched = engine.find_matching_hooks(
        "pre_tool_use", HookContext(tool_name="WriteFile")
    )
    assert {h.id for h in matched} == {"h1", "h2"}


# 验证 condition 为 None 的 Hook 无条件命中。
# 无 condition 的 Hook 不被 condition 过滤掉。
async def test_find_matching_hooks_condition_none_always_matches() -> None:
    hooks = [_make_hook(id="h1", event="session_start")]
    engine = HookEngine(hooks=hooks)
    matched = engine.find_matching_hooks("session_start", HookContext())
    assert [h.id for h in matched] == ["h1"]


# 验证空 hooks 列表时 find_matching_hooks 返回空。
# 无 Hook 配置时返回空 list。
async def test_find_matching_hooks_empty_hooks_returns_empty() -> None:
    engine = HookEngine(hooks=[])
    assert engine.find_matching_hooks("session_start", HookContext()) == []


# ---------------------------------------------------------------------------
# run_hooks 与 _run_single
# ---------------------------------------------------------------------------


# 验证 run_hooks 同步执行命中 Hook 并标记 executed。
# fake executor 记录调用，断言 mark_executed 被触发与 executor 被调用。
async def test_run_hooks_executes_matched_hook_synchronously() -> None:
    hook = _make_hook(id="h1", event="session_start", action_type="command")
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="ok", success=True))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
    assert hook.executed is True
    assert fake.await_count == 1


# 验证 run_hooks 的 async_exec 走后台执行路径。
# async_exec=True 的 Hook 不阻塞 run_hooks，await 一小段后 executor 被调用。
async def test_run_hooks_async_exec_runs_in_background() -> None:
    hook = _make_hook(
        id="h1", event="session_start", action_type="command", async_exec=True
    )
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="ok", success=True))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
        # 后台任务挂起一次让 ensure_future 的任务有机会执行。
        await asyncio.sleep(0.05)
    assert fake.await_count == 1


# 验证 prompt 类型 Hook 成功时把 output 追加到 _prompt_messages。
# fake executor 返回 success 的 prompt 结果，断言 get_prompt_messages 含该消息。
async def test_run_single_prompt_success_appends_prompt_messages() -> None:
    hook = _make_hook(id="h1", event="session_start", action_type="prompt", message="hi")
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="hi", success=True))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
    assert engine.get_prompt_messages() == ["hi"]


# 验证非 prompt 类型 Hook 不追加到 _prompt_messages。
# command 类型的 Hook 执行后 _prompt_messages 仍为空。
async def test_run_single_non_prompt_does_not_append_prompt_messages() -> None:
    hook = _make_hook(id="h1", event="session_start", action_type="command")
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="ok", success=True))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
    assert engine.get_prompt_messages() == []


# 验证 Hook 失败结果追加 success=False 通知。
# fake executor 返回 success=False 的结果，断言通知含 False。
async def test_run_single_failure_result_appends_failure_notification() -> None:
    hook = _make_hook(id="h1", event="session_start", action_type="command")
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="boom", success=False))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
    notifications = engine.drain_notifications()
    assert len(notifications) == 1
    assert notifications[0].success is False
    assert notifications[0].output == "boom"


# 验证 Hook 异常被捕获并追加 success=False 通知。
# fake executor 抛 RuntimeError，断言不传播且通知含 False。
async def test_run_single_exception_caught_and_appended_as_failure() -> None:
    hook = _make_hook(id="h1", event="session_start", action_type="command")
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(side_effect=RuntimeError("oops"))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
    notifications = engine.drain_notifications()
    assert len(notifications) == 1
    assert notifications[0].success is False
    assert "Exception" in notifications[0].output


# 验证 Hook 异常不阻塞后续 Hook 执行。
# 两个 Hook 第一个抛异常第二个正常，断言两个都被尝试执行。
async def test_run_hooks_exception_does_not_block_subsequent_hooks() -> None:
    h1 = _make_hook(id="h1", event="session_start", action_type="command")
    h2 = _make_hook(id="h2", event="session_start", action_type="command")
    engine = HookEngine(hooks=[h1, h2])

    call_log: list[str] = []

    async def _fake(action: Action, ctx: HookContext) -> ActionResult:
        call_log.append(action.command)
        if action.command == "echo hi" and h1.action is action:
            raise RuntimeError("first hook fails")
        return ActionResult(output="ok", success=True)

    with patch("seacode.hooks.engine.execute_action", side_effect=_fake):
        await engine.run_hooks("session_start", HookContext())

    # 两个 Hook 都被尝试执行。
    assert len(call_log) == 2
    notifications = engine.drain_notifications()
    assert len(notifications) == 2


# 验证 once 标记的 Hook 首次执行后第二次 find_matching_hooks 跳过。
# once=True 的 Hook 第一次 run_hooks 触发，第二次 find_matching_hooks 不返回。
async def test_once_hook_skipped_after_first_execution() -> None:
    hook = _make_hook(id="h1", event="session_start", action_type="command", once=True)
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="ok", success=True))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
        # 第二次触发同事件，find_matching_hooks 应返回空。
        await engine.run_hooks("session_start", HookContext())
    assert fake.await_count == 1
    assert hook.executed is True


# ---------------------------------------------------------------------------
# run_pre_tool_hooks
# ---------------------------------------------------------------------------


# 验证 run_pre_tool_hooks 命中 reject 返回 ToolRejectedError。
# reject=True 的 Hook + fake executor 返回 blocked，断言返回 ToolRejectedError。
async def test_run_pre_tool_hooks_returns_rejection_when_reject_true() -> None:
    hook = _make_hook(
        id="h1",
        event="pre_tool_use",
        action_type="prompt",
        message="blocked",
        reject=True,
    )
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="blocked", success=True))
    ctx = HookContext(event_name="pre_tool_use", tool_name="WriteFile")
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        rejection = await engine.run_pre_tool_hooks(ctx)
    assert rejection is not None
    assert isinstance(rejection, ToolRejectedError)
    assert rejection.tool == "WriteFile"
    assert rejection.reason == "blocked"
    assert rejection.hook_id == "h1"


# 验证 run_pre_tool_hooks 不命中 reject 返回 None。
# reject=False 的 Hook 执行后不返回 ToolRejectedError。
async def test_run_pre_tool_hooks_returns_none_when_no_reject() -> None:
    hook = _make_hook(
        id="h1",
        event="pre_tool_use",
        action_type="prompt",
        message="allowed",
        reject=False,
    )
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="ok", success=True))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        rejection = await engine.run_pre_tool_hooks(HookContext(tool_name="WriteFile"))
    assert rejection is None


# 验证无匹配 Hook 时 run_pre_tool_hooks 返回 None。
# 无 pre_tool_use Hook 配置时直接返回 None。
async def test_run_pre_tool_hooks_no_match_returns_none() -> None:
    engine = HookEngine(hooks=[])
    rejection = await engine.run_pre_tool_hooks(HookContext(tool_name="WriteFile"))
    assert rejection is None


# 验证 run_pre_tool_hooks 异常被捕获不返回 ToolRejectedError。
# fake executor 抛 RuntimeError 时 rejection 为 None 且通知含 False。
async def test_run_pre_tool_hooks_exception_caught_returns_none() -> None:
    hook = _make_hook(
        id="h1",
        event="pre_tool_use",
        action_type="prompt",
        message="blocked",
        reject=True,
    )
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(side_effect=RuntimeError("executor crashed"))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        rejection = await engine.run_pre_tool_hooks(HookContext(tool_name="WriteFile"))
    assert rejection is None
    notifications = engine.drain_notifications()
    assert any(n.success is False for n in notifications)


# 验证 run_pre_tool_hooks 多个命中 Hook 按顺序执行。
# 第一个 reject=False 第二个 reject=True，断言两者都被执行后命中第二个 reject。
async def test_run_pre_tool_hooks_executes_in_order_until_reject() -> None:
    h1 = _make_hook(
        id="h1",
        event="pre_tool_use",
        action_type="prompt",
        message="pass",
        reject=False,
    )
    h2 = _make_hook(
        id="h2",
        event="pre_tool_use",
        action_type="prompt",
        message="block",
        reject=True,
    )
    engine = HookEngine(hooks=[h1, h2])

    call_log: list[str] = []

    async def _fake(action: Action, ctx: HookContext) -> ActionResult:
        call_log.append(action.message)
        return ActionResult(output=action.message, success=True)

    with patch("seacode.hooks.engine.execute_action", side_effect=_fake):
        rejection = await engine.run_pre_tool_hooks(HookContext(tool_name="WriteFile"))

    assert rejection is not None
    assert rejection.hook_id == "h2"
    assert call_log == ["pass", "block"]


# 验证 run_pre_tool_hooks 命中 reject 后不再执行后续 Hook。
# 两个 reject=True 的 Hook，断言只执行第一个。
async def test_run_pre_tool_hooks_stops_after_first_reject() -> None:
    h1 = _make_hook(
        id="h1",
        event="pre_tool_use",
        action_type="prompt",
        message="block1",
        reject=True,
    )
    h2 = _make_hook(
        id="h2",
        event="pre_tool_use",
        action_type="prompt",
        message="block2",
        reject=True,
    )
    engine = HookEngine(hooks=[h1, h2])

    call_log: list[str] = []

    async def _fake(action: Action, ctx: HookContext) -> ActionResult:
        call_log.append(action.message)
        return ActionResult(output=action.message, success=True)

    with patch("seacode.hooks.engine.execute_action", side_effect=_fake):
        rejection = await engine.run_pre_tool_hooks(HookContext(tool_name="WriteFile"))

    assert rejection is not None
    assert rejection.hook_id == "h1"
    assert call_log == ["block1"]


# ---------------------------------------------------------------------------
# get_prompt_messages 与 drain_notifications
# ---------------------------------------------------------------------------


# 验证 get_prompt_messages 取出并清空 _prompt_messages。
# 累积消息后第一次取出非空，第二次取出为空。
async def test_get_prompt_messages_drains_and_clears() -> None:
    hook = _make_hook(id="h1", event="session_start", action_type="prompt", message="hi")
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="hi", success=True))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
    msgs1 = engine.get_prompt_messages()
    msgs2 = engine.get_prompt_messages()
    assert msgs1 == ["hi"]
    assert msgs2 == []


# 验证 drain_notifications 取出并清空 _notifications。
# 累积通知后第一次取出非空，第二次取出为空。
async def test_drain_notifications_drains_and_clears() -> None:
    hook = _make_hook(id="h1", event="session_start", action_type="command")
    engine = HookEngine(hooks=[hook])
    fake = AsyncMock(return_value=ActionResult(output="ok", success=True))
    with patch("seacode.hooks.engine.execute_action", side_effect=fake):
        await engine.run_hooks("session_start", HookContext())
    n1 = engine.drain_notifications()
    n2 = engine.drain_notifications()
    assert len(n1) == 1
    assert isinstance(n1[0], HookNotification)
    assert n2 == []


# 验证无消息时 get_prompt_messages 返回空列表。
# 新 engine 无任何触发，直接 get_prompt_messages 返回 []。
async def test_get_prompt_messages_empty_returns_empty_list() -> None:
    engine = HookEngine()
    assert engine.get_prompt_messages() == []


# 验证无通知时 drain_notifications 返回空列表。
# 新 engine 无任何触发，直接 drain_notifications 返回 []。
async def test_drain_notifications_empty_returns_empty_list() -> None:
    engine = HookEngine()
    assert engine.drain_notifications() == []
