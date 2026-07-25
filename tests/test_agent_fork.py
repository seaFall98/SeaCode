"""Fork 路径单元测试：覆盖 FORK_BOILERPLATE 常量与 build_forked_messages 行为。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from seacode.agents.fork import (
    FORK_BOILERPLATE,
    FORK_QUERY_SOURCE,
    ForkError,
    build_forked_messages,
)
from seacode.conversation import Message, ToolResultBlock, ToolUseBlock


# 假对话对象：持有 messages 列表与 env_injected/ltm_injected 状态标记。
@dataclass
class _FakeConversation:
    messages: list[Message] = field(default_factory=list)
    env_injected: bool = False
    ltm_injected: bool = False


# ---------------------------------------------------------------------------
# FORK_BOILERPLATE 与 FORK_QUERY_SOURCE 常量
# ---------------------------------------------------------------------------


# 验证 FORK_BOILERPLATE 含 <fork_boilerplate> 标签。
# 直接断言常量字符串含标签。
def test_fork_boilerplate_contains_tag() -> None:
    assert "<fork_boilerplate>" in FORK_BOILERPLATE


# 验证 FORK_BOILERPLATE 含 "forked worker" 与 "Scope:" 关键字。
# 直接断言常量字符串含两个关键字。
def test_fork_boilerplate_contains_key_phrases() -> None:
    assert "forked worker" in FORK_BOILERPLATE
    assert "Scope:" in FORK_BOILERPLATE


# 验证 FORK_QUERY_SOURCE 值为 "agent:builtin:fork"。
# 直接断言常量值。
def test_fork_query_source_value() -> None:
    assert FORK_QUERY_SOURCE == "agent:builtin:fork"


# ---------------------------------------------------------------------------
# build_forked_messages
# ---------------------------------------------------------------------------


# 验证 build_forked_messages 深拷贝父对话历史。
# 构造父对话含一条消息，调用 build_forked_messages，修改返回列表不影响原对话。
def test_build_forked_messages_deep_copies_parent_messages() -> None:
    parent = _FakeConversation(
        messages=[Message(role="user", content="original")]
    )
    fork_messages = build_forked_messages(parent, "task")
    # 修改返回列表中的消息内容（Message 是 frozen，用 object.__setattr__ 绕过）。
    object.__setattr__(fork_messages[0], "content", "modified")
    # 原父对话的消息不被影响。
    assert parent.messages[0].content == "original"


# 验证 build_forked_messages 追加 FORK_BOILERPLATE 与 task 作为新 user message。
# 构造父对话，调用 build_forked_messages，断言最后一条消息含 FORK_BOILERPLATE 与 task。
def test_build_forked_messages_appends_boilerplate_and_task() -> None:
    parent = _FakeConversation(
        messages=[Message(role="user", content="hello")]
    )
    fork_messages = build_forked_messages(parent, "do something")
    last = fork_messages[-1]
    assert last.role == "user"
    assert "<fork_boilerplate>" in last.content
    assert "do something" in last.content


# 验证 build_forked_messages 父对话历史含 <fork_boilerplate> 抛 ForkError。
# 构造父对话消息含 <fork_boilerplate>，断言抛错且消息含 nested fork。
def test_build_forked_messages_raises_on_nested_fork() -> None:
    parent = _FakeConversation(
        messages=[Message(role="user", content="<fork_boilerplate> nested")]
    )
    with pytest.raises(ForkError, match="nested fork"):
        build_forked_messages(parent, "task")


# 验证 build_forked_messages 父对话最后一条 assistant 含 pending tool_uses 时注入占位。
# 构造父对话最后一条 assistant 含 tool_uses 但无 tool_results，断言被注入 interrupted 占位。
def test_build_forked_messages_injects_interrupted_tool_results() -> None:
    tool_use = ToolUseBlock(tool_use_id="tu_1", tool_name="Bash", arguments={})
    parent = _FakeConversation(
        messages=[
            Message(role="user", content="run bash"),
            Message(
                role="assistant",
                content="",
                tool_uses=[tool_use],
            ),
        ]
    )
    fork_messages = build_forked_messages(parent, "task")
    # 最后一条 assistant 消息（fork_messages[-2] 是 assistant，fork_messages[-1] 是新 user）。
    assistant_msg = fork_messages[-2]
    injected_results = getattr(assistant_msg, "tool_results", [])
    assert len(injected_results) == 1
    assert injected_results[0].tool_use_id == "tu_1"
    assert "interrupted" in injected_results[0].content
    assert injected_results[0].is_error is True


# 验证 build_forked_messages 父对话最后一条 assistant 无 tool_uses 时不注入占位。
# 构造父对话最后一条 assistant 无 tool_uses，断言 tool_results 为空。
def test_build_forked_messages_no_injection_when_no_tool_uses() -> None:
    parent = _FakeConversation(
        messages=[
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ]
    )
    fork_messages = build_forked_messages(parent, "task")
    assistant_msg = fork_messages[-2]
    injected_results = getattr(assistant_msg, "tool_results", [])
    assert len(injected_results) == 0


# 验证 build_forked_messages 父对话最后一条 assistant 已有对应 tool_results 时不重复注入。
# 构造父对话 assistant 含 tool_uses 与对应 tool_results，断言不重复注入。
def test_build_forked_messages_skips_existing_tool_results() -> None:
    tool_use = ToolUseBlock(tool_use_id="tu_1", tool_name="Bash", arguments={})
    existing_result = ToolResultBlock(
        tool_use_id="tu_1", content="real result", is_error=False
    )
    parent = _FakeConversation(
        messages=[
            Message(
                role="assistant",
                content="",
                tool_uses=[tool_use],
                tool_results=[existing_result],
            ),
        ]
    )
    fork_messages = build_forked_messages(parent, "task")
    assistant_msg = fork_messages[-2]
    injected_results = getattr(assistant_msg, "tool_results", [])
    # 已有 1 个真实结果，不应再注入占位。
    assert len(injected_results) == 1
    assert injected_results[0].content == "real result"


# 验证 build_forked_messages 父对话为空时仅追加 fork user 消息。
# 构造空父对话，调用 build_forked_messages，断言返回列表长度为 1。
def test_build_forked_messages_with_empty_parent() -> None:
    parent = _FakeConversation(messages=[])
    fork_messages = build_forked_messages(parent, "task")
    assert len(fork_messages) == 1
    assert fork_messages[0].role == "user"
    assert "<fork_boilerplate>" in fork_messages[0].content
    assert "task" in fork_messages[0].content


# 验证 build_forked_messages 扫描 tool_uses arguments 中的 <fork_boilerplate>。
# 构造父对话消息的 tool_uses arguments 含 <fork_boilerplate>，断言抛 ForkError。
def test_build_forked_messages_scans_tool_use_arguments_for_nested_fork() -> None:
    tool_use = ToolUseBlock(
        tool_use_id="tu_1",
        tool_name="Agent",
        arguments={"prompt": "<fork_boilerplate> nested"},
    )
    parent = _FakeConversation(
        messages=[
            Message(role="user", content="hi", tool_uses=[tool_use]),
        ]
    )
    with pytest.raises(ForkError, match="nested fork"):
        build_forked_messages(parent, "task")
