from __future__ import annotations

import pytest

from seacode.conversation import ConversationManager


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
