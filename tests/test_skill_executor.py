"""Skill 执行器单元测试：覆盖 execute_inline、_build_fork_context、execute_fork。"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from seacode.agent import ErrorEvent, StreamText
from seacode.conversation import Message, ToolResultBlock
from seacode.skills import SkillDef, SkillExecutor, substitute_arguments


# 假 Agent：携带 SkillExecutor 所需全部属性，记录 activate_skill 调用。
class _FakeAgent:
    def __init__(
        self,
        *,
        conversation: Any = None,
        recovery_state: Any = None,
        client: Any = None,
        registry: Any = None,
        protocol: str = "anthropic",
        work_dir: str = ".",
        max_iterations: int = 100,
        context_window: int = 200_000,
    ) -> None:
        self.conversation = conversation
        self.recovery_state = recovery_state
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.context_window = context_window
        self.active_skills: dict[str, str] = {}
        self.activate_calls: list[tuple[str, str]] = []

    def activate_skill(self, name: str, prompt: str) -> None:
        self.active_skills[name] = prompt
        self.activate_calls.append((name, prompt))


# 假 recovery_state：记录 record_skill_invocation 调用。
class _FakeRecoveryState:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def record_skill_invocation(self, name: str, body: str) -> None:
        self.calls.append((name, body))


# 假会话：get_messages 返回预设列表，记录 add_user_message 以检测主对话污染。
class _FakeConversation:
    def __init__(self, messages: list[Message] | None = None) -> None:
        self._messages = messages or []
        self.add_user_calls: list[str] = []

    def get_messages(self) -> list[Message]:
        return list(self._messages)

    def add_user_message(self, content: str) -> None:
        self.add_user_calls.append(content)


# 假子 Agent：构造时记录 kwargs，run() 读取类级 events 列表。
# 类级 events 与 raise_on_run 便于在 execute_fork 内部构造子 Agent 后注入事件或异常。
class _FakeForkAgent:
    events: list[Any] = []
    raise_on_run: BaseException | None = None
    last: _FakeForkAgent | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.permission_checker = kwargs.get("permission_checker")
        # 真实 Agent 默认 skill_catalog=""；子 Agent 构造时不传该参数，保持默认空。
        self.skill_catalog = ""
        self.run_calls = 0
        self.run_conversation: Any = None
        _FakeForkAgent.last = self

    async def run(self, conversation: Any) -> Any:
        self.run_calls += 1
        self.run_conversation = conversation
        if _FakeForkAgent.raise_on_run is not None:
            raise _FakeForkAgent.raise_on_run
        for event in _FakeForkAgent.events:
            yield event


# 每个测试前后重置 _FakeForkAgent 类级状态，避免跨测试污染。
@pytest.fixture(autouse=True)
def _reset_fork_agent_state() -> Any:
    _FakeForkAgent.events = []
    _FakeForkAgent.raise_on_run = None
    _FakeForkAgent.last = None
    yield
    _FakeForkAgent.events = []
    _FakeForkAgent.raise_on_run = None
    _FakeForkAgent.last = None


# ---------- 包重导出 ----------


# 验证 seacode.skills 包重导出 SkillExecutor。
# 从 seacode.skills 导入 SkillExecutor，断言与直接从 executor 模块导入一致。
def test_skills_reexports_executor() -> None:
    from seacode.skills import SkillExecutor as _SE

    assert _SE is SkillExecutor


# ---------- execute_inline ----------


# 验证 execute_inline 调 substitute_arguments 替换 $ARGUMENTS 占位符。
# 构造含 $ARGUMENTS 的 skill，断言返回 prompt 含 args 且占位符已替换。
async def test_execute_inline_substitutes_arguments_placeholder() -> None:
    skill = SkillDef(name="commit", description="提交", prompt_body="执行 $ARGUMENTS")
    agent = _FakeAgent()
    executor = SkillExecutor(agent)
    prompt = await executor.execute_inline(skill, "fix typo")
    assert "fix typo" in prompt
    assert "$ARGUMENTS" not in prompt


# 验证 execute_inline 调 activate_skill 记录激活状态。
# 调用 execute_inline 后断言 agent.activate_calls 含 (skill.name, prompt)。
async def test_execute_inline_calls_activate_skill() -> None:
    skill = SkillDef(name="commit", description="提交", prompt_body="执行提交")
    agent = _FakeAgent()
    executor = SkillExecutor(agent)
    prompt = await executor.execute_inline(skill, "args")
    assert agent.activate_calls == [("commit", prompt)]


# 验证 execute_inline 调 recovery_state.record_skill_invocation。
# mock agent 携带 recovery_state，断言该方法收到 (name, prompt)。
async def test_execute_inline_calls_recovery_state_record() -> None:
    skill = SkillDef(name="commit", description="提交", prompt_body="执行提交")
    recovery = _FakeRecoveryState()
    agent = _FakeAgent(recovery_state=recovery)
    executor = SkillExecutor(agent)
    prompt = await executor.execute_inline(skill, "args")
    assert recovery.calls == [("commit", prompt)]


# 验证 execute_inline 在 agent 无 recovery_state 时不抛异常。
# 删除 agent.recovery_state 属性，调用 execute_inline(skill, "") 不抛异常且原样返回 body。
async def test_execute_inline_without_recovery_state_does_not_raise() -> None:
    skill = SkillDef(name="commit", description="提交", prompt_body="执行提交")
    agent = _FakeAgent()
    del agent.recovery_state
    executor = SkillExecutor(agent)
    prompt = await executor.execute_inline(skill, "")
    assert prompt == "执行提交"


# 验证 execute_inline 返回 substitute_arguments 替换后的 prompt。
# 断言返回值等于 substitute_arguments(skill.prompt_body, args) 结果。
async def test_execute_inline_returns_substituted_prompt() -> None:
    skill = SkillDef(name="commit", description="提交", prompt_body="执行 $ARGUMENTS")
    agent = _FakeAgent()
    executor = SkillExecutor(agent)
    result = await executor.execute_inline(skill, "fix typo")
    assert result == substitute_arguments("执行 $ARGUMENTS", "fix typo")


# 验证 execute_inline 无 args 时原样返回 prompt_body。
# 调用 execute_inline(skill, "")，断言返回值等于原始 prompt_body。
async def test_execute_inline_no_args_returns_original_body() -> None:
    skill = SkillDef(name="commit", description="提交", prompt_body="执行提交")
    agent = _FakeAgent()
    executor = SkillExecutor(agent)
    result = await executor.execute_inline(skill, "")
    assert result == "执行提交"


# ---------- _build_fork_context ----------


# 验证 _build_fork_context none 模式返回空列表。
# 构造有消息的会话，调用 _build_fork_context("none")，断言返回空。
def test_build_fork_context_none_returns_empty() -> None:
    messages = [Message(role="user", content="hello")]
    agent = _FakeAgent(conversation=_FakeConversation(messages))
    executor = SkillExecutor(agent)
    assert executor._build_fork_context("none") == []


# 验证 _build_fork_context recent 模式返回最近 5 条内容消息（过滤工具结果）。
# 构造 5 条内容消息 + 5 条工具结果消息，断言返回 5 条且不含工具结果消息。
def test_build_fork_context_recent_returns_last_five_filtered() -> None:
    messages: list[Message] = []
    for i in range(5):
        messages.append(Message(role="user", content=f"content-{i}"))
    for i in range(5):
        messages.append(
            Message(
                role="assistant",
                content=f"tool-{i}",
                tool_results=[ToolResultBlock(tool_use_id="t", content="r")],
            )
        )
    agent = _FakeAgent(conversation=_FakeConversation(messages))
    executor = SkillExecutor(agent)
    result = executor._build_fork_context("recent")
    assert len(result) == 5
    for m in result:
        assert not m.tool_results
    assert result[0].content == "content-0"


# 验证 _build_fork_context recent 模式不足 5 条返回全部。
# 构造 3 条内容消息，断言 recent 模式返回 3 条。
def test_build_fork_context_recent_fewer_than_five_returns_all() -> None:
    messages = [Message(role="user", content=f"m{i}") for i in range(3)]
    agent = _FakeAgent(conversation=_FakeConversation(messages))
    executor = SkillExecutor(agent)
    result = executor._build_fork_context("recent")
    assert len(result) == 3


# 验证 _build_fork_context full 模式返回单条摘要消息。
# 构造 3 条消息，断言返回 1 条 user 消息含 ## Previous conversation summary。
def test_build_fork_context_full_returns_summary_message() -> None:
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="world"),
        Message(role="user", content="bye"),
    ]
    agent = _FakeAgent(conversation=_FakeConversation(messages))
    executor = SkillExecutor(agent)
    result = executor._build_fork_context("full")
    assert len(result) == 1
    assert result[0].role == "user"
    assert "## Previous conversation summary" in result[0].content
    assert "hello" in result[0].content
    assert "world" in result[0].content


# 验证 _build_fork_context full 模式把消息截断到 200 字符。
# 构造 1 条 300 字符长消息，断言摘要中该消息内容被截断到 200 字符。
def test_build_fork_context_full_truncates_to_200_chars() -> None:
    long_content = "x" * 300
    messages = [Message(role="user", content=long_content)]
    agent = _FakeAgent(conversation=_FakeConversation(messages))
    executor = SkillExecutor(agent)
    result = executor._build_fork_context("full")
    summary = result[0].content
    # 摘要含 "- " 前缀 + 截断后的内容（200 字符），不应出现 201 个连续 x。
    assert "x" * 200 in summary
    assert "x" * 201 not in summary


# 验证 _build_fork_context 未知 context 值 fallback 到 full 行为。
# 调用 _build_fork_context("invalid")，断言返回 full 模式摘要。
def test_build_fork_context_unknown_falls_back_to_full() -> None:
    messages = [Message(role="user", content="hello")]
    agent = _FakeAgent(conversation=_FakeConversation(messages))
    executor = SkillExecutor(agent)
    result = executor._build_fork_context("invalid")
    assert len(result) == 1
    assert "## Previous conversation summary" in result[0].content


# 验证 _build_fork_context 在 agent 无 conversation 时返回空。
# mock agent.conversation 为 None，断言返回空列表。
def test_build_fork_context_no_conversation_returns_empty() -> None:
    agent = _FakeAgent(conversation=None)
    executor = SkillExecutor(agent)
    assert executor._build_fork_context("full") == []


# ---------- execute_fork ----------


# 验证 execute_fork 调 substitute_arguments 替换 $ARGUMENTS 参数。
# 构造含 $ARGUMENTS 的 fork skill，mock 子 Agent，断言子 Agent prompt 含 args。
async def test_execute_fork_substitutes_arguments() -> None:
    skill = SkillDef(
        name="commit", description="提交", prompt_body="执行 $ARGUMENTS", mode="fork"
    )
    _FakeForkAgent.events = []
    agent = _FakeAgent(client=object(), registry=object())
    executor = SkillExecutor(agent)
    with patch("seacode.agent.Agent", side_effect=_FakeForkAgent):
        await executor.execute_fork(skill, "fix typo")
    fork_agent = _FakeForkAgent.last
    assert fork_agent is not None
    fork_conv = fork_agent.run_conversation
    last_msg = fork_conv.get_messages()[-1]
    assert "fix typo" in last_msg.content
    assert "$ARGUMENTS" not in last_msg.content


# 验证 execute_fork 子 Agent 创建参数正确。
# mock Agent 构造函数，断言 6 项运行时依赖复用主 Agent、permission_checker=None。
async def test_execute_fork_subagent_constructor_kwargs() -> None:
    skill = SkillDef(name="x", description="d", prompt_body="body", mode="fork")
    _FakeForkAgent.events = []
    main_client = object()
    main_registry = object()
    agent = _FakeAgent(
        client=main_client,
        registry=main_registry,
        protocol="openai",
        work_dir="/tmp/work",
        max_iterations=50,
        context_window=128_000,
    )
    executor = SkillExecutor(agent)
    with patch("seacode.agent.Agent", side_effect=_FakeForkAgent):
        await executor.execute_fork(skill, "args")
    fork_agent = _FakeForkAgent.last
    assert fork_agent is not None
    kwargs = fork_agent.kwargs
    assert kwargs["client"] is main_client
    assert kwargs["registry"] is main_registry
    assert kwargs["protocol"] == "openai"
    assert kwargs["work_dir"] == "/tmp/work"
    assert kwargs["max_iterations"] == 50
    assert kwargs["context_window"] == 128_000
    assert kwargs["permission_checker"] is None
    # 真实 Agent 构造时不传 skill_catalog，默认值为空字符串。
    assert fork_agent.skill_catalog == ""


# 验证 execute_fork 收集 StreamText 事件并拼接。
# mock 子 Agent yield 两个 StreamText，断言 execute_fork 返回拼接结果。
async def test_execute_fork_collects_streamtext_events() -> None:
    skill = SkillDef(name="x", description="d", prompt_body="body", mode="fork")
    _FakeForkAgent.events = [StreamText("hello"), StreamText(" world")]
    agent = _FakeAgent(client=object(), registry=object())
    executor = SkillExecutor(agent)
    with patch("seacode.agent.Agent", side_effect=_FakeForkAgent):
        result = await executor.execute_fork(skill, "args")
    assert result == "hello world"


# 验证 execute_fork 收集 ErrorEvent 并拼接错误标记。
# mock 子 Agent yield ErrorEvent("boom")，断言 execute_fork 返回 "[error] boom"。
async def test_execute_fork_collects_error_event() -> None:
    skill = SkillDef(name="x", description="d", prompt_body="body", mode="fork")
    _FakeForkAgent.events = [ErrorEvent("boom")]
    agent = _FakeAgent(client=object(), registry=object())
    executor = SkillExecutor(agent)
    with patch("seacode.agent.Agent", side_effect=_FakeForkAgent):
        result = await executor.execute_fork(skill, "args")
    assert result == "[error] boom"


# 验证 execute_fork 不污染主对话历史。
# mock 主会话，调用 execute_fork，断言主会话消息数与 add_user_message 调用不变。
async def test_execute_fork_does_not_pollute_main_conversation() -> None:
    skill = SkillDef(name="x", description="d", prompt_body="body", mode="fork")
    _FakeForkAgent.events = [StreamText("result")]
    main_conv = _FakeConversation([Message(role="user", content="orig")])
    agent = _FakeAgent(client=object(), registry=object(), conversation=main_conv)
    executor = SkillExecutor(agent)
    before_count = len(main_conv.get_messages())
    with patch("seacode.agent.Agent", side_effect=_FakeForkAgent):
        await executor.execute_fork(skill, "args")
    assert len(main_conv.get_messages()) == before_count
    assert main_conv.add_user_calls == []


# 验证 execute_fork 混合事件按出现顺序拼接。
# mock 子 Agent yield StreamText + ErrorEvent + StreamText，断言返回 "a[error] bc"。
async def test_execute_fork_mixed_events_concatenated_in_order() -> None:
    skill = SkillDef(name="x", description="d", prompt_body="body", mode="fork")
    _FakeForkAgent.events = [
        StreamText("a"),
        ErrorEvent("b"),
        StreamText("c"),
    ]
    agent = _FakeAgent(client=object(), registry=object())
    executor = SkillExecutor(agent)
    with patch("seacode.agent.Agent", side_effect=_FakeForkAgent):
        result = await executor.execute_fork(skill, "args")
    assert result == "a[error] bc"


# 验证 execute_fork 在子 Agent run() 抛异常时传播异常。
# mock 子 Agent run() 抛 RuntimeError，断言 execute_fork 传播该异常。
async def test_execute_fork_propagates_subagent_exception() -> None:
    skill = SkillDef(name="x", description="d", prompt_body="body", mode="fork")
    _FakeForkAgent.raise_on_run = RuntimeError("subagent crashed")
    agent = _FakeAgent(client=object(), registry=object())
    executor = SkillExecutor(agent)
    with patch("seacode.agent.Agent", side_effect=_FakeForkAgent):
        with pytest.raises(RuntimeError, match="subagent crashed"):
            await executor.execute_fork(skill, "args")
