"""模型协议适配与统一流事件边界。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from .config import ProviderConfig
from .conversation import Message


class LLMError(Exception):
    """表示可安全展示给用户的模型请求失败。"""


class AuthenticationError(LLMError):
    """表示模型端点拒绝凭据。"""


class RateLimitError(LLMError):
    """表示模型端点暂时限制请求。"""


class NetworkError(LLMError):
    """表示模型端点连接或传输失败。"""


class ProtocolError(LLMError):
    """表示端点返回了无法完成回合的响应。"""


@dataclass(frozen=True)
class TextDelta:
    """表示助手正文的一段增量文本。"""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """表示模型可公开呈现的一段思考增量。"""

    text: str


@dataclass(frozen=True)
class StreamComplete:
    """表示一个 Provider 已正常结束流式回复。"""

    input_tokens: int = 0
    output_tokens: int = 0


type StreamEvent = TextDelta | ThinkingDelta | StreamComplete


class LLMClient(ABC):
    """为 TUI 提供统一的纯文本流接口。"""

    @abstractmethod
    # 根据完成历史和稳定系统提示词产生归一化事件。
    def stream(self, messages: Sequence[Message], system: str) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError


# 将逻辑消息转换为三个协议共用的纯文本角色表示。
def _message_dicts(messages: Sequence[Message]) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in messages]


# 从 SDK 用量对象读取整数值，并安全处理兼容端点的缺失字段。
def _usage_value(usage: Any, field_name: str) -> int:
    value = getattr(usage, field_name, 0) or 0
    return value if isinstance(value, int) else 0


# 将 SDK 异常转换成不包含请求内容或凭据的有限错误类别。
def _normalize_error(error: Exception) -> LLMError:
    if isinstance(error, LLMError):
        return error
    name = type(error).__name__.lower()
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403} or "authentication" in name or "permission" in name:
        return AuthenticationError("The model provider rejected the configured credentials.")
    if status_code == 429 or "ratelimit" in name or "rate_limit" in name:
        return RateLimitError(
            "The model provider is rate limiting this request. Try again shortly."
        )
    if "connection" in name or "timeout" in name or "network" in name:
        return NetworkError("Unable to reach the model provider. Check the network and endpoint.")
    return ProtocolError("The model provider returned an unusable response.")


# 确保构造 SDK 客户端前已有 YAML 密钥或兼容环境变量。
def _api_key(config: ProviderConfig) -> str:
    api_key = config.resolve_api_key()
    if not api_key:
        raise AuthenticationError("No API key is available for the selected provider.")
    return api_key


class AnthropicClient(LLMClient):
    """通过 Anthropic Messages 协议流式传输文本。"""

    # 保存配置并允许测试替换实际 SDK 客户端。
    def __init__(self, config: ProviderConfig, client: Any | None = None) -> None:
        self._config = config
        self._client = client or AsyncAnthropic(
            api_key=_api_key(config),
            base_url=config.base_url,
        )

    # 调用 Messages API 并归一化文本、思考与完成事件。
    async def stream(self, messages: Sequence[Message], system: str) -> AsyncIterator[StreamEvent]:
        request: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": 8192,
            "messages": _message_dicts(messages),
        }
        if system:
            request["system"] = system
        if self._config.thinking:
            request["thinking"] = {"type": "enabled", "budget_tokens": 4096}

        try:
            async with self._client.messages.stream(**request) as response_stream:
                async for raw_event in response_stream:
                    event: Any = raw_event
                    if event.type != "content_block_delta":
                        continue
                    delta = event.delta
                    if delta.type == "text_delta" and getattr(delta, "text", ""):
                        yield TextDelta(delta.text)
                    elif delta.type == "thinking_delta" and getattr(delta, "thinking", ""):
                        yield ThinkingDelta(delta.thinking)
                final = await response_stream.get_final_message()
        except Exception as error:
            raise _normalize_error(error) from error

        usage = getattr(final, "usage", None)
        yield StreamComplete(
            input_tokens=_usage_value(usage, "input_tokens"),
            output_tokens=_usage_value(usage, "output_tokens"),
        )


class OpenAIClient(LLMClient):
    """通过 OpenAI Responses 协议流式传输文本。"""

    # 保存配置并允许测试替换实际 SDK 客户端。
    def __init__(self, config: ProviderConfig, client: Any | None = None) -> None:
        self._config = config
        self._client = client or AsyncOpenAI(
            api_key=_api_key(config),
            base_url=config.base_url,
        )

    # 调用 Responses API 并归一化公开文本与完成信息。
    async def stream(self, messages: Sequence[Message], system: str) -> AsyncIterator[StreamEvent]:
        request: dict[str, Any] = {
            "model": self._config.model,
            "input": _message_dicts(messages),
            "stream": True,
        }
        if system:
            request["instructions"] = system

        completed = False
        try:
            response_stream = await self._client.responses.create(**request)
            async for event in response_stream:
                if event.type == "response.output_text.delta" and getattr(event, "delta", ""):
                    yield TextDelta(event.delta)
                elif event.type == "response.reasoning_summary_text.delta" and getattr(
                    event, "delta", ""
                ):
                    yield ThinkingDelta(event.delta)
                elif event.type == "response.completed":
                    usage = getattr(getattr(event, "response", None), "usage", None)
                    completed = True
                    yield StreamComplete(
                        input_tokens=_usage_value(usage, "input_tokens"),
                        output_tokens=_usage_value(usage, "output_tokens"),
                    )
        except Exception as error:
            raise _normalize_error(error) from error

        if not completed:
            raise ProtocolError("The Responses stream ended without a completion event.")


class OpenAICompatClient(LLMClient):
    """通过 OpenAI-compatible Chat Completions 协议流式传输文本。"""

    # 保存配置并允许测试替换实际 SDK 客户端。
    def __init__(self, config: ProviderConfig, client: Any | None = None) -> None:
        self._config = config
        self._client = client or AsyncOpenAI(
            api_key=_api_key(config),
            base_url=config.base_url,
        )

    # 调用兼容 Chat Completions API 并接受常见 reasoning_content 增量。
    async def stream(self, messages: Sequence[Message], system: str) -> AsyncIterator[StreamEvent]:
        request_messages = _message_dicts(messages)
        if system:
            request_messages.insert(0, {"role": "system", "content": system})
        request: dict[str, Any] = {
            "model": self._config.model,
            "messages": request_messages,
            "max_tokens": 8192,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        completed = False
        try:
            response_stream = await self._client.chat.completions.create(**request)
            async for chunk in response_stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if getattr(delta, "content", None):
                    yield TextDelta(delta.content)
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield ThinkingDelta(reasoning)
                if choice.finish_reason and not completed:
                    completed = True
                    usage = getattr(chunk, "usage", None)
                    yield StreamComplete(
                        input_tokens=_usage_value(usage, "prompt_tokens"),
                        output_tokens=_usage_value(usage, "completion_tokens"),
                    )
        except Exception as error:
            raise _normalize_error(error) from error

        if not completed:
            raise ProtocolError("The compatible chat stream ended without a completion event.")


# 按声明协议创建适配器，避免依据端点地址猜测请求格式。
def create_client(config: ProviderConfig) -> LLMClient:
    if config.protocol == "anthropic":
        return AnthropicClient(config)
    if config.protocol == "openai":
        return OpenAIClient(config)
    if config.protocol == "openai-compat":
        return OpenAICompatClient(config)
    raise ProtocolError("The selected provider protocol is unsupported.")
