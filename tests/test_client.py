from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from seacode.client import (
    AnthropicClient,
    OpenAIClient,
    OpenAICompatClient,
    StreamComplete,
    TextDelta,
    ThinkingDelta,
    create_client,
)
from seacode.config import ProviderConfig
from seacode.conversation import Message


# 模拟 SDK 返回的异步事件序列，不触发真实网络。
class _AsyncEvents:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    # 逐个交付预先定义的 SDK 事件。
    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    # 提供兼容异步迭代器协议的测试事件。
    async def _iterate(self) -> AsyncIterator[Any]:
        for event in self._events:
            yield event


# 记录 Responses 请求并返回预设流。
class _FakeResponses:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.request: dict[str, Any] | None = None

    # 保存调用参数以验证 Responses 边界。
    async def create(self, **request: Any) -> _AsyncEvents:
        self.request = request
        return _AsyncEvents(self.events)


# 暴露 Responses 命名空间的最小测试客户端。
class _FakeOpenAIResponsesClient:
    def __init__(self, events: list[Any]) -> None:
        self.responses = _FakeResponses(events)


# 记录兼容 Chat Completions 请求并返回预设流。
class _FakeChatCompletions:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.request: dict[str, Any] | None = None

    # 保存调用参数以验证兼容协议边界。
    async def create(self, **request: Any) -> _AsyncEvents:
        self.request = request
        return _AsyncEvents(self.events)


# 暴露 Chat Completions 命名空间的最小测试客户端。
class _FakeOpenAICompatClient:
    def __init__(self, events: list[Any]) -> None:
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(events))


# 模拟 Anthropic 上下文管理流与最终用量。
class _FakeAnthropicStream:
    def __init__(self, events: list[Any], final: Any) -> None:
        self._events = events
        self._final = final

    # 进入测试流上下文。
    async def __aenter__(self) -> _FakeAnthropicStream:
        return self

    # 退出测试流上下文且不抑制异常。
    async def __aexit__(self, *_: Any) -> bool:
        return False

    # 交付预设 Anthropic 事件。
    def __aiter__(self) -> AsyncIterator[Any]:
        return _AsyncEvents(self._events).__aiter__()

    # 返回带用量的最终消息。
    async def get_final_message(self) -> Any:
        return self._final


# 暴露 Anthropic Messages 流入口的最小测试客户端。
class _FakeAnthropicClient:
    def __init__(self, events: list[Any], final: Any) -> None:
        self.events = events
        self.final = final
        self.request: dict[str, Any] | None = None
        self.messages = SimpleNamespace(stream=self.stream)

    # 保存调用参数并创建异步上下文流。
    def stream(self, **request: Any) -> _FakeAnthropicStream:
        self.request = request
        return _FakeAnthropicStream(self.events, self.final)


# 创建一个只含测试凭据的 Provider 配置。
def _provider(protocol: str) -> ProviderConfig:
    return ProviderConfig(
        name=f"{protocol}-profile",
        protocol=protocol,
        model="test-model",
        base_url="https://api.example.test",
        api_key="test-key",
        thinking=protocol == "anthropic",
    )


# 收集异步客户端事件，便于检查统一事件序列。
async def _events(client: Any) -> list[Any]:
    return [event async for event in client.stream([Message("user", "Hello")], "System prompt")]


# 验证 OpenAI 配置只调用 Responses API 并保留 instructions 字段。
# 预设响应同时覆盖文本、思考和完成用量的归一化。
@pytest.mark.asyncio
async def test_openai_client_uses_responses_protocol() -> None:
    completion = SimpleNamespace(usage=SimpleNamespace(input_tokens=11, output_tokens=7))
    fake = _FakeOpenAIResponsesClient(
        [
            SimpleNamespace(type="response.output_text.delta", delta="Hello"),
            SimpleNamespace(type="response.reasoning_summary_text.delta", delta="Thinking"),
            SimpleNamespace(type="response.completed", response=completion),
        ]
    )
    client = OpenAIClient(_provider("openai"), client=fake)

    events = await _events(client)

    assert isinstance(events[0], TextDelta)
    assert isinstance(events[1], ThinkingDelta)
    assert events[2] == StreamComplete(input_tokens=11, output_tokens=7)
    assert fake.responses.request is not None
    assert fake.responses.request["input"] == [{"role": "user", "content": "Hello"}]
    assert fake.responses.request["instructions"] == "System prompt"


# 验证兼容配置只调用 Chat Completions 并把系统消息置于首位。
# 流在 finish_reason 到达时完成，即使兼容端点不提供 usage 块。
@pytest.mark.asyncio
async def test_openai_compat_client_uses_chat_completions_protocol() -> None:
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="Hello", reasoning_content="Reasoning"),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    fake = _FakeOpenAICompatClient([chunk])
    client = OpenAICompatClient(_provider("openai-compat"), client=fake)

    events = await _events(client)

    assert [type(event) for event in events] == [TextDelta, ThinkingDelta, StreamComplete]
    request = fake.chat.completions.request
    assert request is not None
    assert request["messages"][0] == {"role": "system", "content": "System prompt"}
    assert request["messages"][1] == {"role": "user", "content": "Hello"}


# 验证 Anthropic 配置只调用 Messages，并在启用时传递 thinking 参数。
# 最终消息的用量应被统一为 StreamComplete。
@pytest.mark.asyncio
async def test_anthropic_client_uses_messages_protocol() -> None:
    delta = SimpleNamespace(type="text_delta", text="Hello")
    event = SimpleNamespace(type="content_block_delta", delta=delta)
    final = SimpleNamespace(usage=SimpleNamespace(input_tokens=5, output_tokens=3))
    fake = _FakeAnthropicClient([event], final)
    client = AnthropicClient(_provider("anthropic"), client=fake)

    events = await _events(client)

    assert events == [TextDelta("Hello"), StreamComplete(input_tokens=5, output_tokens=3)]
    assert fake.request is not None
    assert fake.request["messages"] == [{"role": "user", "content": "Hello"}]
    assert fake.request["thinking"] == {"type": "enabled", "budget_tokens": 4096}


# 验证客户端工厂只根据 protocol 创建对应适配器。
# 三个协议值均通过同一公开构造入口覆盖。
@pytest.mark.parametrize(
    ("protocol", "expected_type"),
    [
        ("anthropic", AnthropicClient),
        ("openai", OpenAIClient),
        ("openai-compat", OpenAICompatClient),
    ],
)
def test_create_client_uses_declared_protocol(protocol: str, expected_type: type[Any]) -> None:
    client = create_client(_provider(protocol))

    assert isinstance(client, expected_type)
