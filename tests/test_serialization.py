from __future__ import annotations

import json
from typing import Any

from seacode.conversation import Message, ThinkingBlock, ToolResultBlock, ToolUseBlock
from seacode.serialization import build_messages


# 构造一条包含思考、工具调用与工具结果的完整助手回合，供多协议序列化复用。
def _round_trip_messages() -> list[Message]:
    return [
        Message(role="user", content="Hello"),
        Message(
            role="assistant",
            content="Let me read the file.",
            thinking_blocks=[ThinkingBlock(thinking="Planning...", signature="sig-1")],
            tool_uses=[
                ToolUseBlock(
                    tool_use_id="call-1",
                    tool_name="ReadFile",
                    arguments={"file_path": "a.py"},
                )
            ],
        ),
        Message(
            role="user",
            tool_results=[
                ToolResultBlock(
                    tool_use_id="call-1", content="file body", is_error=False
                )
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


# 验证 Anthropic 协议把思考、正文、工具调用与工具结果编码为 content 块列表。
# 对完整回合序列化，断言助手消息含 thinking/text/tool_use 块、用户消息含 tool_result 块。
def test_anthropic_encodes_thinking_tool_uses_and_results() -> None:
    messages = _round_trip_messages()

    result = build_messages(messages, protocol="anthropic")

    assert result[0] == {"role": "user", "content": "Hello"}

    assistant = result[1]
    assert assistant["role"] == "assistant"
    content = assistant["content"]
    assert content[0] == {
        "type": "thinking",
        "thinking": "Planning...",
        "signature": "sig-1",
    }
    assert content[1] == {"type": "text", "text": "Let me read the file."}
    assert content[2] == {
        "type": "tool_use",
        "id": "call-1",
        "name": "ReadFile",
        "input": {"file_path": "a.py"},
    }

    user = result[2]
    assert user["role"] == "user"
    assert user["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": "file body",
        "is_error": False,
    }


# 验证 Anthropic 协议中 tool_use 与 tool_result 的 ID 保持配对。
# 序列化后提取两处 ID，断言它们相等。
def test_anthropic_pairs_tool_call_and_result_ids() -> None:
    messages = _round_trip_messages()

    result = build_messages(messages, protocol="anthropic")

    tool_use_id = result[1]["content"][2]["id"]
    tool_result_id = result[2]["content"][0]["tool_use_id"]
    assert tool_use_id == tool_result_id == "call-1"


# 验证 Anthropic 协议对纯文本消息生成简单的 role/content 结构。
# 只含 user 与 assistant 文本，断言没有 content 块列表。
def test_anthropic_serializes_plain_text_messages() -> None:
    messages = [
        Message(role="user", content="Hi"),
        Message(role="assistant", content="Hello there"),
    ]

    result = build_messages(messages, protocol="anthropic")

    assert result == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello there"},
    ]


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------


# 验证 OpenAI Responses 协议把思考编码为 reasoning、工具调用编码为 function_call。
# 对完整回合序列化，断言 reasoning、function_call 与 function_call_output 类型正确。
def test_openai_encodes_reasoning_and_function_calls() -> None:
    messages = _round_trip_messages()

    result = build_messages(messages, protocol="openai")

    assert result[0] == {"role": "user", "content": "Hello"}

    reasoning = result[1]
    assert reasoning["type"] == "reasoning"
    assert reasoning["id"] == "sig-1"
    assert reasoning["summary"] == [{"type": "summary_text", "text": "Planning..."}]

    assistant = result[2]
    assert assistant == {"role": "assistant", "content": "Let me read the file."}

    function_call = result[3]
    assert function_call["type"] == "function_call"
    assert function_call["name"] == "ReadFile"
    assert function_call["call_id"] == "call-1"
    assert json.loads(function_call["arguments"]) == {"file_path": "a.py"}

    function_output = result[4]
    assert function_output["type"] == "function_call_output"
    assert function_output["call_id"] == "call-1"
    assert function_output["output"] == "file body"


# 验证 OpenAI 协议中 function_call 与 function_call_output 的 call_id 配对。
# 序列化后提取两处 call_id，断言它们相等。
def test_openai_pairs_function_call_and_output_ids() -> None:
    messages = _round_trip_messages()

    result = build_messages(messages, protocol="openai")

    call_ids = [item["call_id"] for item in result if "call_id" in item]
    assert call_ids == ["call-1", "call-1"]


# 验证 OpenAI 协议对纯文本消息生成简单的 role/content 结构。
# 只含 user 与 assistant 文本，断言不产生 reasoning 或 function_call 项。
def test_openai_serializes_plain_text_messages() -> None:
    messages = [
        Message(role="user", content="Hi"),
        Message(role="assistant", content="Hello there"),
    ]

    result = build_messages(messages, protocol="openai")

    assert result == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello there"},
    ]


# ---------------------------------------------------------------------------
# OpenAI-compatible Chat Completions
# ---------------------------------------------------------------------------


# 验证兼容协议把工具调用编码为 tool_calls、结果编码为 role=tool 消息。
# 对完整回合序列化，断言 tool_calls 结构与 tool 消息字段正确。
def test_openai_compat_encodes_tool_calls_and_tool_role() -> None:
    messages = _round_trip_messages()

    result = build_messages(messages, protocol="openai-compat")

    assert result[0] == {"role": "user", "content": "Hello"}

    assistant = result[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Let me read the file."
    assert assistant["reasoning_content"] == "Planning..."
    tool_calls = assistant["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call-1"
    assert tool_calls[0]["type"] == "function"
    assert tool_calls[0]["function"]["name"] == "ReadFile"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"file_path": "a.py"}

    tool_msg = result[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call-1"
    assert tool_msg["content"] == "file body"


# 验证兼容协议中 tool_calls id 与 tool 消息 tool_call_id 配对。
# 序列化后提取两处 ID，断言它们相等。
def test_openai_compat_pairs_tool_call_and_tool_message_ids() -> None:
    messages = _round_trip_messages()

    result = build_messages(messages, protocol="openai-compat")

    call_id = result[1]["tool_calls"][0]["id"]
    tool_call_id = result[2]["tool_call_id"]
    assert call_id == tool_call_id == "call-1"


# 验证兼容协议对纯文本消息生成简单的 role/content 结构且无 reasoning_content。
# 只含 user 与 assistant 文本，断言不含 tool_calls 或 reasoning_content 键。
def test_openai_compat_serializes_plain_text_messages() -> None:
    messages = [
        Message(role="user", content="Hi"),
        Message(role="assistant", content="Hello there"),
    ]

    result = build_messages(messages, protocol="openai-compat")

    assert result == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello there"},
    ]


# ---------------------------------------------------------------------------
# 多工具调用顺序
# ---------------------------------------------------------------------------


# 验证三协议在多工具调用时保持声明顺序。
# 构造含两个工具调用的助手消息，断言各协议结果顺序一致。
def test_multiple_tool_calls_preserve_order_across_protocols() -> None:
    messages = [
        Message(
            role="assistant",
            content="Running tools.",
            tool_uses=[
                ToolUseBlock(
                    tool_use_id="c1", tool_name="Bash", arguments={"command": "ls"}
                ),
                ToolUseBlock(
                    tool_use_id="c2", tool_name="Glob", arguments={"pattern": "*"}
                ),
            ],
        ),
        Message(
            role="user",
            tool_results=[
                ToolResultBlock(tool_use_id="c1", content="out1"),
                ToolResultBlock(tool_use_id="c2", content="out2"),
            ],
        ),
    ]

    for protocol in ("anthropic", "openai", "openai-compat"):
        result = build_messages(messages, protocol=protocol)
        ids = _extract_tool_ids(result, protocol)
        assert ids == ["c1", "c2", "c1", "c2"], f"order mismatch for {protocol}"


# 按协议从序列化结果中提取工具调用与结果的 ID 序列。
def _extract_tool_ids(result: list[dict[str, Any]], protocol: str) -> list[str]:
    ids: list[str] = []
    for item in result:
        if protocol == "anthropic":
            content = item.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        ids.append(block["id"])
                    elif block.get("type") == "tool_result":
                        ids.append(block["tool_use_id"])
        elif protocol == "openai":
            if item.get("type") == "function_call":
                ids.append(item["call_id"])
            elif item.get("type") == "function_call_output":
                ids.append(item["call_id"])
        elif protocol == "openai-compat":
            if "tool_calls" in item:
                for tc in item["tool_calls"]:
                    ids.append(tc["id"])
            elif item.get("role") == "tool":
                ids.append(item["tool_call_id"])
    return ids


# ---------------------------------------------------------------------------
# 连续 user 纯文本消息合并（压缩后摘要 user 与尾部 user 相邻时的协议合法化）
# ---------------------------------------------------------------------------


# 验证 Anthropic 协议合并连续 user 纯文本消息。
# 构造两条相邻 user 消息，断言序列化后合并为一条并以 \n 分隔。
def test_anthropic_merges_consecutive_plain_user_messages() -> None:
    messages = [
        Message(role="user", content="summary from compact"),
        Message(role="user", content="continue work"),
    ]

    result = build_messages(messages, protocol="anthropic")

    assert result == [
        {"role": "user", "content": "summary from compact\ncontinue work"},
    ]


# 验证 Anthropic 协议不在 user 与 assistant 之间合并。
# 构造 user → assistant → user，断言三条独立消息不被合并。
def test_anthropic_does_not_merge_across_assistant() -> None:
    messages = [
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content="second"),
    ]

    result = build_messages(messages, protocol="anthropic")

    assert result == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]


# 验证 Anthropic 协议不把 user 纯文本合并到 user(tool_results) 消息中。
# 构造 user(plain) → user(tool_results)，断言两条消息独立保留。
def test_anthropic_does_not_merge_plain_user_into_tool_result_user() -> None:
    messages = [
        Message(role="user", content="plain"),
        Message(
            role="user",
            tool_results=[
                ToolResultBlock(tool_use_id="c1", content="result"),
            ],
        ),
    ]

    result = build_messages(messages, protocol="anthropic")

    # 第一条 plain user 保留；第二条 user 含 tool_result content 块，不合并。
    assert len(result) == 2
    assert result[0] == {"role": "user", "content": "plain"}
    assert result[1]["role"] == "user"
    assert isinstance(result[1]["content"], list)
    assert result[1]["content"][0]["type"] == "tool_result"


# 验证 OpenAI Responses 协议合并连续 user 纯文本消息。
# 构造两条相邻 user 消息，断言序列化后合并为一条并以 \n 分隔。
def test_openai_merges_consecutive_plain_user_messages() -> None:
    messages = [
        Message(role="user", content="summary from compact"),
        Message(role="user", content="continue work"),
    ]

    result = build_messages(messages, protocol="openai")

    assert result == [
        {"role": "user", "content": "summary from compact\ncontinue work"},
    ]


# 验证 OpenAI Responses 协议不把含 thinking_blocks 的 user 消息参与合并。
# 构造 user(plain) → user(thinking_blocks)，断言两条独立保留。
def test_openai_does_not_merge_user_with_thinking_blocks() -> None:
    messages = [
        Message(role="user", content="plain"),
        Message(
            role="user",
            content="with-thinking",
            thinking_blocks=[ThinkingBlock(thinking="thoughts", signature="s1")],
        ),
    ]

    result = build_messages(messages, protocol="openai")

    # 第二条 user 含 thinking_blocks，不参与合并，作为独立 message + reasoning 项。
    # 第一条 plain user 不应被合并到含 thinking 的消息中。
    user_plain_count = sum(
        1 for item in result
        if item.get("role") == "user" and item.get("content") == "plain"
    )
    assert user_plain_count == 1


# 验证 OpenAI Responses 协议不把 user 纯文本合并到 assistant 消息之后。
# 构造 user → assistant → user，断言三条独立消息不被合并。
def test_openai_does_not_merge_across_assistant() -> None:
    messages = [
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content="second"),
    ]

    result = build_messages(messages, protocol="openai")

    # user → assistant → user，三条独立消息。
    assert {"role": "user", "content": "first"} in result
    assert {"role": "assistant", "content": "reply"} in result
    assert {"role": "user", "content": "second"} in result


# 验证 OpenAI-compatible Chat Completions 协议合并连续 user 纯文本消息。
# 构造两条相邻 user 消息，断言序列化后合并为一条并以 \n 分隔。
def test_openai_compat_merges_consecutive_plain_user_messages() -> None:
    messages = [
        Message(role="user", content="summary from compact"),
        Message(role="user", content="continue work"),
    ]

    result = build_messages(messages, protocol="openai-compat")

    assert result == [
        {"role": "user", "content": "summary from compact\ncontinue work"},
    ]


# 验证兼容协议不把含 reasoning_content 的 user 消息参与合并。
# 构造 user(plain) → user(thinking_blocks)，断言 plain 不被合并到含 reasoning 的消息。
def test_openai_compat_does_not_merge_user_with_reasoning_content() -> None:
    messages = [
        Message(role="user", content="plain"),
        Message(
            role="user",
            content="with-reasoning",
            thinking_blocks=[ThinkingBlock(thinking="thoughts", signature="s1")],
        ),
    ]

    result = build_messages(messages, protocol="openai-compat")

    # 第一条 plain user 不应被合并到含 reasoning_content 的消息中。
    user_plain_count = sum(
        1 for item in result
        if item.get("role") == "user" and item.get("content") == "plain"
    )
    assert user_plain_count == 1


# 验证兼容协议不把 user 纯文本合并到 user(tool_results) 消息中。
# 构造 user(plain) → assistant(tool_use) → user(tool_results) → user(plain)，
# 断言尾部 plain 不合并到 tool 消息。
def test_openai_compat_does_not_merge_plain_user_into_tool_role() -> None:
    messages = [
        Message(role="user", content="plain"),
        Message(
            role="assistant",
            tool_uses=[
                ToolUseBlock(tool_use_id="c1", tool_name="Bash", arguments={})
            ],
        ),
        Message(
            role="user",
            tool_results=[
                ToolResultBlock(tool_use_id="c1", content="result"),
            ],
        ),
        Message(role="user", content="follow-up"),
    ]

    result = build_messages(messages, protocol="openai-compat")

    # tool 消息（role=tool）不应被合并 plain user；follow-up plain user 独立保留。
    tool_msgs = [item for item in result if item.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "result"
    follow_up_count = sum(
        1 for item in result
        if item.get("role") == "user" and item.get("content") == "follow-up"
    )
    assert follow_up_count == 1


# 验证三协议对三条连续 user 纯文本消息都合并为一条。
# 构造三条相邻 user 消息，断言各协议结果都只剩一条 user 消息。
def test_all_protocols_merge_three_consecutive_plain_user_messages() -> None:
    messages = [
        Message(role="user", content="first"),
        Message(role="user", content="second"),
        Message(role="user", content="third"),
    ]

    for protocol in ("anthropic", "openai", "openai-compat"):
        result = build_messages(messages, protocol=protocol)
        user_msgs = [item for item in result if item.get("role") == "user"]
        assert len(user_msgs) == 1, f"protocol {protocol} should merge to one user"
        assert user_msgs[0]["content"] == "first\nsecond\nthird", (
            f"protocol {protocol} should join with \\n"
        )
