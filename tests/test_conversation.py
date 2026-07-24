from __future__ import annotations

import pytest

from seacode.conversation import ConversationManager, Message


# 验证完成的用户和助手文本作为同一个回合提交到历史。
# 请求视图在提交前包含暂存用户消息，提交后历史顺序稳定。
def test_complete_turn_commits_user_and_assistant_atomically() -> None:
    conversation = ConversationManager()
    conversation.begin_turn("Hello")

    assert [message.content for message in conversation.messages_for_request()] == ["Hello"]

    conversation.complete_turn("Hi there")

    assert [(message.role, message.content) for message in conversation.messages] == [
        ("user", "Hello"),
        ("assistant", "Hi there"),
    ]


# 验证流失败后暂存用户和不完整助手内容不会进入逻辑历史。
# 放弃回合后可立即开始下一轮并成功提交。
def test_abandoned_turn_does_not_pollute_following_request() -> None:
    conversation = ConversationManager()
    conversation.begin_turn("First request")
    conversation.abandon_turn()
    conversation.begin_turn("Second request")

    assert [message.content for message in conversation.messages_for_request()] == [
        "Second request"
    ]

    conversation.complete_turn("Second answer")

    assert [message.content for message in conversation.messages] == [
        "Second request",
        "Second answer",
    ]


# 验证同一时刻只能有一个暂存回合。
# 双重提交应快速失败，避免并发请求共享错误历史。
def test_conversation_rejects_second_active_turn() -> None:
    conversation = ConversationManager()
    conversation.begin_turn("First request")

    with pytest.raises(RuntimeError, match="already active"):
        conversation.begin_turn("Second request")


# ---------------------------------------------------------------------------
# batch04：add_system_reminder / inject_environment / replace_history
# ---------------------------------------------------------------------------


# 验证 add_system_reminder 在末尾追加 <system-reminder> 包裹的 user 消息。
# 调用后断言末条消息 role=user 且内容含 XML 标签与原 content。
def test_add_system_reminder_appends_wrapped_user_message() -> None:
    conversation = ConversationManager()
    conversation.add_user_message("first")

    conversation.add_system_reminder("Plan mode active")

    assert len(conversation.messages) == 2
    last = conversation.messages[-1]
    assert last.role == "user"
    assert "<system-reminder>" in last.content
    assert "</system-reminder>" in last.content
    assert "Plan mode active" in last.content


# 验证 inject_environment 在 position 0 插入会话级上下文且只注入一次。
# 连续调用两次 inject_environment，断言只在首条插入一次。
def test_inject_environment_inserts_at_head_only_once() -> None:
    conversation = ConversationManager()
    conversation.add_user_message("user msg")

    conversation.inject_environment("env context A")
    conversation.inject_environment("env context B")

    assert len(conversation.messages) == 2
    assert conversation.messages[0].role == "user"
    assert conversation.messages[0].content == "env context A"
    assert conversation.messages[1].content == "user msg"
    assert conversation.env_injected is True


# 验证 inject_environment 在空历史时也正常插入。
# 空历史调用 inject_environment 后应只有一条环境消息。
def test_inject_environment_works_on_empty_history() -> None:
    conversation = ConversationManager()

    conversation.inject_environment("env context")

    assert len(conversation.messages) == 1
    assert conversation.messages[0].content == "env context"
    assert conversation.env_injected is True


# 验证 replace_history 替换整个历史并重置 env_injected。
# 注入环境后 replace_history，断言历史被替换且 env_injected 为 False。
def test_replace_history_resets_env_injected() -> None:
    conversation = ConversationManager()
    conversation.add_user_message("old")
    conversation.inject_environment("env")
    assert conversation.env_injected is True

    conversation.replace_history([Message(role="user", content="new")])

    assert len(conversation.messages) == 1
    assert conversation.messages[0].content == "new"
    assert conversation.env_injected is False


# 验证 replace_history 后可再次注入环境上下文。
# 重置后调用 inject_environment 应再次插入到 position 0。
def test_replace_history_allows_re_injection() -> None:
    conversation = ConversationManager()
    conversation.inject_environment("first env")
    conversation.replace_history([Message(role="user", content="summarized")])

    conversation.inject_environment("second env")

    assert conversation.messages[0].content == "second env"
    assert conversation.messages[1].content == "summarized"
    assert conversation.env_injected is True


# 验证 add_system_reminder 在 inject_environment 之后追加到末尾而非头部。
# 先注入环境再追加提醒，断言提醒在末尾、环境在头部。
def test_add_system_reminder_appends_after_inject_environment() -> None:
    conversation = ConversationManager()
    conversation.add_user_message("user")
    conversation.inject_environment("env context")
    conversation.add_system_reminder("reminder")

    assert len(conversation.messages) == 3
    assert conversation.messages[0].content == "env context"
    assert conversation.messages[1].content == "user"
    assert "<system-reminder>" in conversation.messages[2].content
