from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from seacode.client import (
    ANTHROPIC_MODEL_FETCH_TIMEOUT,
    AnthropicClient,
    OpenAIClient,
    OpenAICompatClient,
    StreamComplete,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    _supports_adaptive_thinking,
    create_client,
    resolve_context_window,
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


# 验证三个协议客户端都公开 ProviderConfig 中的模型名称。
# 使用真实客户端适配器但替换外部 SDK，断言状态层可依赖统一模型契约。
@pytest.mark.parametrize("protocol", ["anthropic", "openai", "openai-compat"])
def test_clients_expose_configured_model(protocol: str) -> None:
    config = _provider(protocol)
    client_type = {
        "anthropic": AnthropicClient,
        "openai": OpenAIClient,
        "openai-compat": OpenAICompatClient,
    }[protocol]
    client = client_type(config, client=object())
    assert client.model == "test-model"


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
                delta=SimpleNamespace(
                    content="Hello", reasoning_content="Reasoning", tool_calls=None
                ),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    fake = _FakeOpenAICompatClient([chunk])
    client = OpenAICompatClient(_provider("openai-compat"), client=fake)

    events = await _events(client)

    assert [type(event) for event in events] == [
        TextDelta,
        ThinkingDelta,
        ThinkingComplete,
        StreamComplete,
    ]
    request = fake.chat.completions.request
    assert request is not None
    assert request["messages"][0] == {"role": "system", "content": "System prompt"}
    assert request["messages"][1] == {"role": "user", "content": "Hello"}


# 验证 Anthropic 配置只调用 Messages，并在启用时传递 thinking 参数。
# 最终消息的用量应被统一为 StreamComplete；prompt cache 启用时 user 消息尾部带 cache_control。
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
    # 启用 prompt cache 后，最后一条 user 消息的 content 会被改写为带 cache_control 的 list。
    assert fake.request["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Hello", "cache_control": {"type": "ephemeral"}}
            ],
        }
    ]
    # test-model 不在 adaptive thinking 名单，budget_tokens = max_output_tokens - 1。
    assert fake.request["thinking"] == {"type": "enabled", "budget_tokens": 8191}
    # system prompt 也应该用 list 形式带 cache_control。
    assert fake.request["system"] == [
        {"type": "text", "text": "System prompt", "cache_control": {"type": "ephemeral"}}
    ]


# 验证 adaptive thinking 只对已支持的 4 系列小版本启用。
# 参数化相邻版本，防止宽泛的系列匹配向不支持的模型发送零预算。
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4-1", False),
        ("claude-sonnet-4-5", False),
        ("claude-opus-4-6", True),
        ("claude-sonnet-4-6", True),
        ("claude-opus-5-0", False),
    ],
)
def test_adaptive_thinking_requires_supported_model_version(
    model: str, expected: bool
) -> None:
    assert _supports_adaptive_thinking(model) is expected


# 验证不支持 adaptive thinking 的模型使用明确的正数预算。
# 通过记录 Anthropic 请求，断言 claude-opus-4-1 不携带 budget_tokens=0。
@pytest.mark.asyncio
async def test_anthropic_client_uses_positive_thinking_budget_for_unsupported_version() -> None:
    delta = SimpleNamespace(type="text_delta", text="Hello")
    event = SimpleNamespace(type="content_block_delta", delta=delta)
    final = SimpleNamespace(usage=SimpleNamespace(input_tokens=5, output_tokens=3))
    fake = _FakeAnthropicClient([event], final)
    config = ProviderConfig(
        name="anthropic-profile",
        protocol="anthropic",
        model="claude-opus-4-1",
        base_url="https://api.anthropic.test",
        api_key="test-key",
        thinking=True,
    )
    client = AnthropicClient(config, client=fake)

    await _events(client)

    assert fake.request is not None
    assert fake.request["thinking"] == {"type": "enabled", "budget_tokens": 8191}


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


# ---------------------------------------------------------------------------
# AnthropicClient.fetch_model_context_window
# ---------------------------------------------------------------------------


# 记录 models.retrieve 调用并可返回预设值或抛异常的最小 SDK 客户端。
class _FakeModelsRetrieve:
    def __init__(
        self, info: Any | None = None, error: Exception | None = None
    ) -> None:
        self.info = info
        self.error = error
        self.retrieve_calls: list[tuple[str, dict[str, Any]]] = []

    # 保存调用参数以验证 model 与 timeout 透传；按构造期预设决定返回或抛异常。
    async def retrieve(self, model: str, **kwargs: Any) -> Any:
        self.retrieve_calls.append((model, dict(kwargs)))
        if self.error is not None:
            raise self.error
        return self.info


# 暴露 models 命名空间的最小测试客户端，供 AnthropicClient 注入。
class _FakeAnthropicModelsClient:
    def __init__(
        self, info: Any | None = None, error: Exception | None = None
    ) -> None:
        self.models = _FakeModelsRetrieve(info=info, error=error)


# 创建带 anthropic 协议与可选 context_window 的测试 Provider。
def _anthropic_provider(*, context_window: int = 0) -> ProviderConfig:
    return ProviderConfig(
        name="anthropic-profile",
        protocol="anthropic",
        model="claude-test",
        base_url="https://api.anthropic.test",
        api_key="test-key",
        thinking=True,
        context_window=context_window,
    )


# 验证 fetch_model_context_window 把 SDK 返回的 max_input_tokens 转成正整数。
# 注入返回 max_input_tokens=200000 的假 SDK 客户端，断言返回值与 model 透传。
@pytest.mark.asyncio
async def test_fetch_model_context_window_returns_max_input_tokens() -> None:
    info = SimpleNamespace(max_input_tokens=200_000)
    fake = _FakeAnthropicModelsClient(info=info)
    client = AnthropicClient(_anthropic_provider(), client=fake)

    window = await client.fetch_model_context_window()

    assert window == 200_000
    # model 字段透传给 retrieve。
    assert fake.models.retrieve_calls[0][0] == "claude-test"
    # timeout 透传为模块常量。
    assert fake.models.retrieve_calls[0][1]["timeout"] == ANTHROPIC_MODEL_FETCH_TIMEOUT


# 验证 fetch_model_context_window 在 SDK 异常时返回 None 而不抛出。
# 注入抛 RuntimeError 的假 SDK 客户端，断言返回 None。
@pytest.mark.asyncio
async def test_fetch_model_context_window_returns_none_on_sdk_error() -> None:
    fake = _FakeAnthropicModelsClient(error=RuntimeError("network down"))
    client = AnthropicClient(_anthropic_provider(), client=fake)

    window = await client.fetch_model_context_window()

    assert window is None


# 验证 fetch_model_context_window 在 info 缺失 max_input_tokens 时返回 None。
# 注入返回空 SimpleNamespace 的假 SDK 客户端，断言返回 None。
@pytest.mark.asyncio
async def test_fetch_model_context_window_returns_none_when_field_missing() -> None:
    info = SimpleNamespace()  # 无 max_input_tokens 属性
    fake = _FakeAnthropicModelsClient(info=info)
    client = AnthropicClient(_anthropic_provider(), client=fake)

    window = await client.fetch_model_context_window()

    assert window is None


# 验证 fetch_model_context_window 在 max_input_tokens 非正整数时返回 None。
# 注入返回 max_input_tokens=0 的假 SDK 客户端，断言返回 None。
@pytest.mark.asyncio
async def test_fetch_model_context_window_returns_none_when_non_positive() -> None:
    info = SimpleNamespace(max_input_tokens=0)
    fake = _FakeAnthropicModelsClient(info=info)
    client = AnthropicClient(_anthropic_provider(), client=fake)

    window = await client.fetch_model_context_window()

    assert window is None


# ---------------------------------------------------------------------------
# resolve_context_window
# ---------------------------------------------------------------------------


# 验证 resolve_context_window 在显式 context_window > 0 时跳过网络请求。
# 构造 context_window=100000 的 provider，断言 create_client 不被调用且缓存不变。
@pytest.mark.asyncio
async def test_resolve_context_window_skips_when_explicit_config_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[ProviderConfig] = []

    def _fake_create_client(config: ProviderConfig) -> Any:
        calls.append(config)
        raise AssertionError("create_client should not be called")

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    provider = _anthropic_provider(context_window=100_000)

    await resolve_context_window(provider)

    assert calls == []
    assert provider._fetched_context_window == 0


# 验证 resolve_context_window 在已缓存 _fetched_context_window > 0 时跳过网络请求。
# 先手动 set_fetched_context_window，断言 create_client 不被调用且缓存保留原值。
@pytest.mark.asyncio
async def test_resolve_context_window_skips_when_already_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[ProviderConfig] = []

    def _fake_create_client(config: ProviderConfig) -> Any:
        calls.append(config)
        raise AssertionError("create_client should not be called")

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    provider = _anthropic_provider()
    provider.set_fetched_context_window(150_000)

    await resolve_context_window(provider)

    assert calls == []
    assert provider._fetched_context_window == 150_000


# 验证 resolve_context_window 对非 anthropic 协议直接返回，不发起拉取。
# 构造 openai 协议 provider，断言 create_client 不被调用。
@pytest.mark.asyncio
async def test_resolve_context_window_skips_non_anthropic_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[ProviderConfig] = []

    def _fake_create_client(config: ProviderConfig) -> Any:
        calls.append(config)
        raise AssertionError("create_client should not be called")

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    provider = ProviderConfig(
        name="openai-profile",
        protocol="openai",
        model="gpt-4o",
        base_url="https://api.openai.test",
        api_key="test-key",
    )

    await resolve_context_window(provider)

    assert calls == []
    assert provider._fetched_context_window == 0


# 验证 resolve_context_window 成功时把窗口大小写入 _fetched_context_window。
# 用 monkeypatch 替换 create_client 返回注入了假 SDK 的 AnthropicClient。
@pytest.mark.asyncio
async def test_resolve_context_window_caches_window_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = SimpleNamespace(max_input_tokens=300_000)
    fake_sdk = _FakeAnthropicModelsClient(info=info)
    fake_client = AnthropicClient(_anthropic_provider(), client=fake_sdk)

    def _fake_create_client(config: ProviderConfig) -> Any:
        return fake_client

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    provider = _anthropic_provider()

    await resolve_context_window(provider)

    assert provider._fetched_context_window == 300_000


# 验证 resolve_context_window 在 fetch 抛异常时静默降级、不写缓存。
# 用 monkeypatch 返回 fetch 会抛异常的假客户端，断言 _fetched_context_window 保持 0。
@pytest.mark.asyncio
async def test_resolve_context_window_silently_handles_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sdk = _FakeAnthropicModelsClient(error=RuntimeError("timeout"))
    fake_client = AnthropicClient(_anthropic_provider(), client=fake_sdk)

    def _fake_create_client(config: ProviderConfig) -> Any:
        return fake_client

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    provider = _anthropic_provider()

    await resolve_context_window(provider)

    assert provider._fetched_context_window == 0


# 验证 resolve_context_window 在 create_client 抛异常时静默降级。
# 用 monkeypatch 让 create_client 抛异常，断言不向上传播且缓存不变。
@pytest.mark.asyncio
async def test_resolve_context_window_silently_handles_create_client_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_create_client(config: ProviderConfig) -> Any:
        raise RuntimeError("no api key")

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    provider = _anthropic_provider()

    await resolve_context_window(provider)

    assert provider._fetched_context_window == 0


# 验证 resolve_context_window 在 fetch 返回 None 时不写缓存。
# 用 monkeypatch 返回 fake_sdk（无 max_input_tokens），断言缓存保持 0。
@pytest.mark.asyncio
async def test_resolve_context_window_does_not_cache_when_fetch_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = SimpleNamespace()  # 无 max_input_tokens
    fake_sdk = _FakeAnthropicModelsClient(info=info)
    fake_client = AnthropicClient(_anthropic_provider(), client=fake_sdk)

    def _fake_create_client(config: ProviderConfig) -> Any:
        return fake_client

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    provider = _anthropic_provider()

    await resolve_context_window(provider)

    assert provider._fetched_context_window == 0
