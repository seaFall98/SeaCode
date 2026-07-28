"""Agent 与 app 的 Hook 集成测试：覆盖 8 个注入点与 prompt 注入。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic import BaseModel

from seacode.agent import (
    Agent,
    HookEvent,
    LoopComplete,
    StreamText,
    ToolResultEvent,
)
from seacode.app import SeaCodeApp
from seacode.client import (
    LLMClient,
    StreamComplete,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    ToolCallStart,
)
from seacode.conversation import ConversationManager
from seacode.hooks.engine import HookEngine, HookNotification
from seacode.hooks.models import HookContext, ToolRejectedError
from seacode.tools import ToolRegistry
from seacode.tools.base import Tool, ToolCategory, ToolResult

# ---------------------------------------------------------------------------
# 测试基础设施（与 test_agent.py 风格一致的最小重建）
# ---------------------------------------------------------------------------


class _MockParams(BaseModel):
    input: str = ""


class _MockTool(Tool):
    description = "Mock tool for hook integration tests."
    params_model = _MockParams
    category = ToolCategory.READ

    def __init__(
        self,
        name: str = "MockTool",
        result: ToolResult | None = None,
    ) -> None:
        self.name = name
        self._result = result or ToolResult(content="mock output")

    async def execute(self, params: BaseModel) -> ToolResult:
        return self._result


class _FakeClient(LLMClient):
    def __init__(
        self, outcomes: list[list[StreamEvent] | Exception]
    ) -> None:
        self._outcomes = outcomes
        self.requests: list[tuple[Message_t, ...]] = []
        self.systems_passed: list[str] = []

    async def stream(
        self,
        messages: Sequence[Any],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(tuple(messages))
        self.systems_passed.append(system)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for event in outcome:
            yield event


# 类型别名避免与 conversation.Message 命名冲突的导入混淆。
Message_t = Any


def _tool_call_stream(
    tool_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> list[StreamEvent]:
    return [
        ToolCallStart(tool_name=tool_name, tool_id=tool_id),
        ToolCallComplete(
            tool_id=tool_id,
            tool_name=tool_name,
            arguments=arguments or {},
        ),
        StreamComplete(input_tokens=1, output_tokens=1),
    ]


def _text_stream(text: str) -> list[StreamEvent]:
    return [TextDelta(text), StreamComplete(input_tokens=1, output_tokens=1)]


async def _collect(agent_run: Any) -> list[Any]:
    return [event async for event in agent_run]


# 受 HookEngine 契约约束的测试引擎，记录调用并支持预设结果。
class _FakeHookEngine(HookEngine):
    def __init__(
        self,
        *,
        rejection: ToolRejectedError | None = None,
        prompt_messages: list[str] | None = None,
        notifications: list[HookNotification] | None = None,
    ) -> None:
        super().__init__()
        self.run_hooks_calls: list[tuple[str, HookContext]] = []
        self.run_pre_tool_hooks_calls: list[HookContext] = []
        self.get_prompt_messages_calls = 0
        self.drain_notifications_calls = 0
        self._rejection = rejection
        self._prompt_messages = list(prompt_messages or [])
        self._notifications = list(notifications or [])

    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        self.run_hooks_calls.append((event, ctx))

    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        self.run_pre_tool_hooks_calls.append(ctx)
        return self._rejection

    def get_prompt_messages(self) -> list[str]:
        self.get_prompt_messages_calls += 1
        msgs = list(self._prompt_messages)
        # 取出后清空，匹配真实 HookEngine 语义。
        self._prompt_messages = []
        return msgs

    def drain_notifications(self) -> list[HookNotification]:
        self.drain_notifications_calls += 1
        notifications = list(self._notifications)
        self._notifications = []
        return notifications


def _build_agent(
    client: _FakeClient,
    *,
    hook_engine: HookEngine | None = None,
) -> tuple[Agent, ToolRegistry, ConversationManager]:
    """构造带 MockTool 的 Agent 与空对话；hook_engine 可选注入。"""
    tool = _MockTool(name="MockTool", result=ToolResult(content="ok"))
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(
        client=client,
        registry=registry,
        protocol="anthropic",
        hook_engine=hook_engine,
    )
    conversation = ConversationManager()
    return agent, registry, conversation


# ---------------------------------------------------------------------------
# set_hook_engine 注入
# ---------------------------------------------------------------------------


# 验证 set_hook_engine 把 engine 注入到 Agent.hook_engine 字段。
# 构造无 hook_engine 的 Agent，调用 set_hook_engine 后断言字段非 None。
def test_set_hook_engine_injects_engine() -> None:
    client = _FakeClient([_text_stream("hi")])
    agent, _, _ = _build_agent(client)
    assert agent.hook_engine is None
    engine = _FakeHookEngine()
    agent.set_hook_engine(engine)
    assert agent.hook_engine is engine


# 验证 set_hook_engine 传 None 关闭所有注入点。
# 已注入 engine 后再传 None，断言字段回到 None。
def test_set_hook_engine_none_disables_injection() -> None:
    client = _FakeClient([_text_stream("hi")])
    agent, _, _ = _build_agent(client, hook_engine=_FakeHookEngine())
    assert agent.hook_engine is not None
    agent.set_hook_engine(None)
    assert agent.hook_engine is None


# ---------------------------------------------------------------------------
# Agent.run 各生命周期注入点
# ---------------------------------------------------------------------------


# 验证 Agent.run 触发 session_start 与 session_end hook。
# 用 _FakeHookEngine 记录调用，运行一轮纯文本对话断言两个事件被调用。
async def test_agent_run_triggers_session_start_and_end_hooks() -> None:
    client = _FakeClient([_text_stream("hi")])
    engine = _FakeHookEngine()
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Hello")

    await _collect(agent.run(conversation))

    events_called = [e for e, _ in engine.run_hooks_calls]
    assert "session_start" in events_called
    assert "session_end" in events_called


# 验证 Agent.run 每轮触发 turn_start hook；turn_end 仅在工具回合触发（由工具回合用例覆盖）。
# 一轮纯文本对话断言 turn_start 出现在 run_hooks 调用中。
async def test_agent_run_triggers_turn_start_hook() -> None:
    client = _FakeClient([_text_stream("hi")])
    engine = _FakeHookEngine()
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Hello")

    await _collect(agent.run(conversation))

    events_called = [e for e, _ in engine.run_hooks_calls]
    assert "turn_start" in events_called


# 验证 Agent.run 在 LLM 调用前后触发 pre_send 与 post_receive hook。
# 一轮纯文本对话断言 pre_send 与 post_receive 各被调用一次。
async def test_agent_run_triggers_pre_send_and_post_receive_hooks() -> None:
    client = _FakeClient([_text_stream("hi")])
    engine = _FakeHookEngine()
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Hello")

    await _collect(agent.run(conversation))

    events_called = [e for e, _ in engine.run_hooks_calls]
    assert "pre_send" in events_called
    assert "post_receive" in events_called


# 验证 pre_tool_use hook 拦截工具调用转为 is_error=True 工具结果。
# _FakeHookEngine.run_pre_tool_hooks 返回 ToolRejectedError，断言 ToolResultEvent 含拒绝原因。
async def test_pre_tool_use_hook_rejects_tool_call() -> None:
    client = _FakeClient(
        [
            _tool_call_stream("c1", "MockTool", {"input": "x"}),
            _text_stream("Recovered"),
        ]
    )
    rejection = ToolRejectedError(
        tool="MockTool", reason="blocked by hook", hook_id="h1"
    )
    engine = _FakeHookEngine(rejection=rejection)
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Run tool")

    events = await _collect(agent.run(conversation))

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].is_error is True
    assert "Hook rejected" in result_events[0].output
    assert "blocked by hook" in result_events[0].output
    # run_pre_tool_hooks 被调用一次。
    assert len(engine.run_pre_tool_hooks_calls) == 1


# 验证 pre_tool_use 不拦截时工具正常执行。
# _FakeHookEngine.run_pre_tool_hooks 返回 None，断言工具结果正常返回。
async def test_pre_tool_use_hook_no_rejection_runs_tool() -> None:
    client = _FakeClient(
        [
            _tool_call_stream("c1", "MockTool", {"input": "x"}),
            _text_stream("Done"),
        ]
    )
    engine = _FakeHookEngine(rejection=None)
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Run tool")

    events = await _collect(agent.run(conversation))

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].is_error is False
    assert result_events[0].output == "ok"


# 验证 post_tool_use hook 在工具执行后被触发。
# 一轮工具调用对话断言 post_tool_use 出现在 run_hooks 调用中。
async def test_post_tool_use_hook_triggered_after_tool_execution() -> None:
    client = _FakeClient(
        [
            _tool_call_stream("c1", "MockTool", {"input": "x"}),
            _text_stream("Done"),
        ]
    )
    engine = _FakeHookEngine()
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Run tool")

    await _collect(agent.run(conversation))

    events_called = [e for e, _ in engine.run_hooks_calls]
    assert "post_tool_use" in events_called


# 验证 turn_end hook 在工具回合完成后被触发。
# 一轮工具调用对话断言 turn_end 出现在 run_hooks 调用中。
async def test_turn_end_hook_triggered_after_tool_round() -> None:
    client = _FakeClient(
        [
            _tool_call_stream("c1", "MockTool", {"input": "x"}),
            _text_stream("Done"),
        ]
    )
    engine = _FakeHookEngine()
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Run tool")

    await _collect(agent.run(conversation))

    events_called = [e for e, _ in engine.run_hooks_calls]
    assert "turn_end" in events_called


# ---------------------------------------------------------------------------
# build_system_prompt 注入 hook_prompts
# ---------------------------------------------------------------------------


# 验证 hook_prompts 出现在 build_system_prompt 的 # Hook Injected Context 段落。
# _FakeHookEngine.get_prompt_messages 返回 ["injected context"]，断言 system 含该段。
async def test_hook_prompts_injected_into_system_prompt() -> None:
    client = _FakeClient([_text_stream("hi")])
    engine = _FakeHookEngine(prompt_messages=["injected context"])
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Hello")

    await _collect(agent.run(conversation))

    assert client.systems_passed, "应当至少有一次模型调用"
    system = client.systems_passed[0]
    assert "# Hook Injected Context" in system
    assert "injected context" in system


# 验证无 hook_prompts 时 build_system_prompt 不含 # Hook Injected Context 段落。
# _FakeHookEngine.get_prompt_messages 返回空列表，断言 system 不含该段。
async def test_no_hook_prompts_no_injected_section() -> None:
    client = _FakeClient([_text_stream("hi")])
    engine = _FakeHookEngine(prompt_messages=[])
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Hello")

    await _collect(agent.run(conversation))

    assert client.systems_passed
    assert "# Hook Injected Context" not in client.systems_passed[0]


# ---------------------------------------------------------------------------
# HookEvent 被 yield
# ---------------------------------------------------------------------------


# 验证 HookNotification 经 drain_notifications 后被 yield 为 HookEvent。
# _FakeHookEngine.drain_notifications 返回非空通知，断言 HookEvent 出现在事件流。
async def test_hook_event_yielded_from_drain_notifications() -> None:
    notification = HookNotification(
        hook_id="h1", event="session_start", output="hi", success=True
    )
    engine = _FakeHookEngine(notifications=[notification])
    client = _FakeClient([_text_stream("hi")])
    agent, _, conversation = _build_agent(client, hook_engine=engine)
    conversation.add_user_message("Hello")

    events = await _collect(agent.run(conversation))

    hook_events = [e for e in events if isinstance(e, HookEvent)]
    assert len(hook_events) >= 1
    target = hook_events[0]
    assert target.hook_id == "h1"
    assert target.event == "session_start"
    assert target.output == "hi"
    assert target.success is True


# ---------------------------------------------------------------------------
# 无 hook_engine 时零开销
# ---------------------------------------------------------------------------


# 验证 hook_engine=None 时 Agent.run 不抛异常且正常 yield 事件。
# 构造无 engine 的 Agent 运行一轮对话，断言事件流含 StreamText 与 LoopComplete。
async def test_agent_run_without_hook_engine_does_not_raise() -> None:
    client = _FakeClient([_text_stream("hi")])
    agent, _, conversation = _build_agent(client, hook_engine=None)
    conversation.add_user_message("Hello")

    events = await _collect(agent.run(conversation))

    event_types = [type(e) for e in events]
    assert StreamText in event_types
    assert isinstance(events[-1], LoopComplete)


# ---------------------------------------------------------------------------
# app.py 构造时 hook_engine 注入
# ---------------------------------------------------------------------------


# 验证 SeaCodeApp 构造时 hook_engine 注入到 _hook_engine 字段。
# 构造 SeaCodeApp 传入 hook_engine，断言 _hook_engine 字段非 None 且为同一对象。
def test_seacode_app_constructor_injects_hook_engine() -> None:
    from seacode.config import ProviderConfig

    provider = ProviderConfig(
        name="test",
        protocol="anthropic",
        model="m",
        base_url="http://x",
        api_key="k",
    )
    engine = HookEngine(hooks=[])
    app = SeaCodeApp(
        providers=(provider,),
        client_factory=lambda p: _FakeClient([]),
        hook_engine=engine,
    )
    assert app._hook_engine is engine


# 验证 SeaCodeApp 默认 hook_engine 为 None。
# 不传 hook_engine 构造 SeaCodeApp，断言 _hook_engine 字段为 None。
def test_seacode_app_default_hook_engine_is_none() -> None:
    from seacode.config import ProviderConfig

    provider = ProviderConfig(
        name="test",
        protocol="anthropic",
        model="m",
        base_url="http://x",
        api_key="k",
    )
    app = SeaCodeApp(
        providers=(provider,),
        client_factory=lambda p: _FakeClient([]),
    )
    assert app._hook_engine is None


# 验证 SeaCodeApp._run_turn 把 _hook_engine 传给 Agent 构造参数。
# _run_turn 强依赖 TUI 控件无法直接运行，改用源码检查确认注入链完整。
def test_seacode_app_run_turn_passes_hook_engine_to_agent() -> None:
    import inspect

    from seacode.app import SeaCodeApp

    source = inspect.getsource(SeaCodeApp._run_turn)
    # 构造 Agent 时传入 hook_engine=self._hook_engine。
    assert "hook_engine=self._hook_engine" in source
