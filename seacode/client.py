"""模型协议适配与统一流事件边界。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from .config import ProviderConfig
from .conversation import Message
from .serialization import (
    build_anthropic_messages,
    build_chat_completion_messages,
    build_openai_input,
)


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
class ThinkingComplete:
    """表示一段思考块结束，携带完整思考文本与签名。"""

    thinking: str
    signature: str


@dataclass(frozen=True)
class ToolCallStart:
    """表示一个工具调用块开始。"""

    tool_name: str
    tool_id: str


@dataclass(frozen=True)
class ToolCallDelta:
    """表示工具调用参数 JSON 的一段增量。"""

    text: str


@dataclass(frozen=True)
class ToolCallComplete:
    """表示一个工具调用块结束，携带解析后的参数字典。"""

    tool_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class StreamComplete:
    """表示一个 Provider 已正常结束流式回复。"""

    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"
    cache_read: int = 0
    cache_creation: int = 0


type StreamEvent = (
    TextDelta
    | ThinkingDelta
    | ThinkingComplete
    | ToolCallStart
    | ToolCallDelta
    | ToolCallComplete
    | StreamComplete
)


class LLMClient(ABC):
    """为上层提供统一的三家协议流式接口。"""

    # 声明为同步方法返回 AsyncIterator；子类以 async generator 实现，调用方式不变。
    @abstractmethod
    def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    # 更新后续请求的最大输出 token 数；默认无操作，子类按需覆盖。
    def set_max_output_tokens(self, n: int) -> None:
        """更新后续流式请求的最大输出 token 上限。"""


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


# 识别支持 adaptive thinking 的 Anthropic 模型。
# 仅确认的 4 系列小版本允许 budget_tokens=0，其余模型保留明确的正数预算。
def _supports_adaptive_thinking(model: str) -> bool:
    normalized_model = model.lower()
    for family in ("claude-opus-4-", "claude-sonnet-4-"):
        if not normalized_model.startswith(family):
            continue
        version_suffix = normalized_model[len(family) :]
        if version_suffix and version_suffix[0].isdigit() and int(version_suffix[0]) >= 6:
            return True
    return False


# 给 messages 列表中最后一条 user 消息尾部追加 cache_control 标记，
# 启用 Anthropic prompt cache 的前缀缓存（多轮对话每轮命中前缀缓存，节省成本）。
# 原地修改并返回 messages；content 既支持 str 也支持 list[dict]。
def _mark_last_user_tail_for_cache(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not messages:
        return messages
    # 从末尾向前找最后一条 user 消息。
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") != "user":
            continue
        msg = messages[idx]
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
        elif isinstance(content, list) and content:
            # 仅给最后一个 block 加标记；已存在 cache_control 时跳过避免重复。
            last_block = content[-1]
            if isinstance(last_block, dict) and "cache_control" not in last_block:
                last_block["cache_control"] = {"type": "ephemeral"}
        break
    return messages


# 给 tools 列表中最后一个工具 schema 追加 cache_control 标记，
# 让工具定义也命中前缀缓存（工具集稳定时收益显著）。
def _mark_last_tool_for_cache(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tools:
        return tools
    last = tools[-1]
    if isinstance(last, dict) and "cache_control" not in last:
        last["cache_control"] = {"type": "ephemeral"}
    return tools


# Anthropic /v1/models 拉取上下文窗口的超时秒数；超时不抛异常，降级到下一层。
ANTHROPIC_MODEL_FETCH_TIMEOUT: float = 3.0


class AnthropicClient(LLMClient):
    """通过 Anthropic Messages 协议流式传输文本、思考与工具调用。"""

    # 保存配置并允许测试替换实际 SDK 客户端；max_output_tokens 从配置读取。
    def __init__(self, config: ProviderConfig, client: Any | None = None) -> None:
        self._config = config
        self._max_output_tokens = config.get_max_output_tokens()
        self._client = client or AsyncAnthropic(
            api_key=_api_key(config),
            base_url=config.base_url,
        )

    # 更新后续请求的最大输出 token 数。
    def set_max_output_tokens(self, n: int) -> None:
        self._max_output_tokens = n

    # 向 Anthropic 兼容的 /v1/models/{model} 端点查询模型的 max_input_tokens
    # （context window 解析的第 2 层）。尽力而为：遇到任何错误都返回 None 而非抛异常，
    # 阻塞时间不超过 ANTHROPIC_MODEL_FETCH_TIMEOUT，让调用方安全降级。
    async def fetch_model_context_window(self) -> int | None:
        try:
            info = await self._client.models.retrieve(
                self._config.model, timeout=ANTHROPIC_MODEL_FETCH_TIMEOUT
            )
            window = getattr(info, "max_input_tokens", None)
            if isinstance(window, int) and window > 0:
                return window
            return None
        except Exception:
            return None

    # 调用 Messages API 并归一化文本、思考、工具调用与完成事件。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # 构造请求：system / tools / messages 都启用 prompt cache，让多轮对话前缀命中缓存。
        request_messages = build_anthropic_messages(list(messages))
        _mark_last_user_tail_for_cache(request_messages)
        request: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._max_output_tokens,
            "messages": request_messages,
        }
        if system:
            # system prompt 用 list 形式携带 cache_control，命中前缀缓存。
            request["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if self._config.thinking:
            # adaptive thinking 模型用 budget_tokens=0 表示由模型自决定思考预算；
            # 旧模型用 max_output_tokens - 1 保证思考与正文有足够空间（至少 1024）。
            if _supports_adaptive_thinking(self._config.model):
                request["thinking"] = {"type": "enabled", "budget_tokens": 0}
            else:
                budget = max(self._max_output_tokens - 1, 1024)
                request["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if tools:
            # tools 列表尾部加 cache_control，让工具定义也命中前缀缓存。
            _mark_last_tool_for_cache(tools)
            request["tools"] = tools

        # 流式工具调用与思考块的累积状态。
        current_tool_name = ""
        current_tool_id = ""
        json_accum = ""
        in_thinking = False
        thinking_accum = ""
        thinking_signature = ""

        # MiniMax 等兼容 provider 在 message_delta 中上报用量，暂存作为降级值。
        delta_input_tokens = 0
        delta_cache_read = 0
        delta_cache_creation = 0

        try:
            async with self._client.messages.stream(**request) as response_stream:
                async for raw_event in response_stream:
                    event: Any = raw_event
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            in_thinking = True
                            thinking_accum = ""
                            thinking_signature = ""
                        elif block.type == "tool_use":
                            current_tool_name = block.name
                            current_tool_id = block.id
                            json_accum = ""
                            yield ToolCallStart(
                                tool_name=current_tool_name,
                                tool_id=current_tool_id,
                            )
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta" and getattr(delta, "text", ""):
                            yield TextDelta(delta.text)
                        elif delta.type == "thinking_delta" and getattr(delta, "thinking", ""):
                            thinking_accum += delta.thinking
                            yield ThinkingDelta(delta.thinking)
                        elif delta.type == "signature_delta":
                            thinking_signature = delta.signature
                        elif delta.type == "input_json_delta":
                            partial = getattr(delta, "partial_json", "")
                            if partial:
                                json_accum += partial
                                yield ToolCallDelta(text=partial)
                    elif event.type == "content_block_stop":
                        if in_thinking:
                            yield ThinkingComplete(
                                thinking=thinking_accum,
                                signature=thinking_signature,
                            )
                            in_thinking = False
                        if current_tool_name:
                            # 损坏 JSON 优雅降级为空字典。
                            try:
                                args = json.loads(json_accum) if json_accum else {}
                            except json.JSONDecodeError:
                                args = {}
                            yield ToolCallComplete(
                                tool_id=current_tool_id,
                                tool_name=current_tool_name,
                                arguments=args,
                            )
                            current_tool_name = ""
                            current_tool_id = ""
                            json_accum = ""
                    elif event.type == "message_delta":
                        delta_usage = getattr(event, "usage", None)
                        if delta_usage:
                            v = getattr(delta_usage, "input_tokens", 0) or 0
                            if v:
                                delta_input_tokens = v
                            v = getattr(delta_usage, "cache_read_input_tokens", 0) or 0
                            if v:
                                delta_cache_read = v
                            v = getattr(delta_usage, "cache_creation_input_tokens", 0) or 0
                            if v:
                                delta_cache_creation = v
                final = await response_stream.get_final_message()
        except Exception as error:
            raise _normalize_error(error) from error

        usage = getattr(final, "usage", None)
        input_tokens = _usage_value(usage, "input_tokens")
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0 if usage else 0
        cache_creation = (
            getattr(usage, "cache_creation_input_tokens", 0) or 0 if usage else 0
        )
        # 当 message_start 报告 input_tokens=0 时（兼容 provider），降级使用 delta 值。
        if not input_tokens and delta_input_tokens:
            input_tokens = delta_input_tokens
        if not cache_read and delta_cache_read:
            cache_read = delta_cache_read
        if not cache_creation and delta_cache_creation:
            cache_creation = delta_cache_creation
        stop_reason = getattr(final, "stop_reason", None) or "end_turn"

        yield StreamComplete(
            input_tokens=input_tokens,
            output_tokens=_usage_value(usage, "output_tokens"),
            stop_reason=stop_reason,
            cache_read=cache_read if isinstance(cache_read, int) else 0,
            cache_creation=cache_creation if isinstance(cache_creation, int) else 0,
        )


class OpenAIClient(LLMClient):
    """通过 OpenAI Responses 协议流式传输文本、思考与工具调用。"""

    # 保存配置并允许测试替换实际 SDK 客户端。
    def __init__(self, config: ProviderConfig, client: Any | None = None) -> None:
        self._config = config
        self._max_output_tokens = 0
        self._client = client or AsyncOpenAI(
            api_key=_api_key(config),
            base_url=config.base_url,
        )

    # 更新后续请求的最大输出 token 数；0 表示不设置上限。
    def set_max_output_tokens(self, n: int) -> None:
        self._max_output_tokens = n

    # 调用 Responses API 并归一化公开文本、思考与工具调用事件。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        request: dict[str, Any] = {
            "model": self._config.model,
            "input": build_openai_input(list(messages)),
            "stream": True,
        }
        if self._max_output_tokens > 0:
            request["max_output_tokens"] = self._max_output_tokens
        if system:
            request["instructions"] = system
        if tools:
            request["tools"] = tools

        current_tool_name = ""
        current_call_id = ""
        json_accum = ""
        reasoning_id = ""
        reasoning_text = ""
        completed = False

        try:
            response_stream = await self._client.responses.create(**request)
            async for event in response_stream:
                if event.type == "response.output_text.delta" and getattr(event, "delta", ""):
                    yield TextDelta(event.delta)
                elif event.type == "response.reasoning_summary_text.delta":
                    reasoning_text += event.delta
                    yield ThinkingDelta(event.delta)
                elif event.type == "response.reasoning_summary_text.done":
                    yield ThinkingComplete(thinking=reasoning_text, signature=reasoning_id)
                elif event.type == "response.function_call_arguments.delta":
                    if not current_tool_name:
                        current_tool_name = getattr(event, "name", "") or ""
                        current_call_id = getattr(event, "call_id", "") or ""
                        if current_tool_name:
                            yield ToolCallStart(
                                tool_name=current_tool_name,
                                tool_id=current_call_id,
                            )
                    json_accum += event.delta
                    yield ToolCallDelta(text=event.delta)
                elif event.type == "response.function_call_arguments.done":
                    if not current_tool_name:
                        current_tool_name = getattr(event, "name", "") or ""
                        current_call_id = getattr(event, "call_id", "") or ""
                    try:
                        args = json.loads(json_accum) if json_accum else {}
                    except json.JSONDecodeError:
                        args = {}
                    yield ToolCallComplete(
                        tool_id=current_call_id,
                        tool_name=current_tool_name,
                        arguments=args,
                    )
                    current_tool_name = ""
                    current_call_id = ""
                    json_accum = ""
                elif event.type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        current_tool_name = getattr(item, "name", "")
                        current_call_id = getattr(item, "call_id", "")
                        json_accum = ""
                        yield ToolCallStart(
                            tool_name=current_tool_name,
                            tool_id=current_call_id,
                        )
                    elif item and getattr(item, "type", "") == "reasoning":
                        reasoning_id = getattr(item, "id", "")
                        reasoning_text = ""
                elif event.type == "response.completed":
                    resp = getattr(event, "response", None)
                    usage = getattr(resp, "usage", None) if resp else None
                    # input_tokens 包含缓存 token，减去 cache_read 保持可加性。
                    details = getattr(usage, "input_tokens_details", None) if usage else None
                    cache_read = getattr(details, "cached_tokens", 0) or 0 if details else 0
                    input_tokens = getattr(usage, "input_tokens", 0) or 0 if usage else 0
                    completed = True
                    yield StreamComplete(
                        stop_reason="end_turn",
                        input_tokens=max(input_tokens - cache_read, 0),
                        output_tokens=getattr(usage, "output_tokens", 0) or 0 if usage else 0,
                        cache_read=cache_read,
                        cache_creation=0,
                    )
        except Exception as error:
            raise _normalize_error(error) from error

        if not completed:
            raise ProtocolError("The Responses stream ended without a completion event.")


class OpenAICompatClient(LLMClient):
    """通过 OpenAI-compatible Chat Completions 协议流式传输文本、思考与工具调用。"""

    # 保存配置并允许测试替换实际 SDK 客户端；max_output_tokens 从配置读取。
    def __init__(self, config: ProviderConfig, client: Any | None = None) -> None:
        self._config = config
        self._max_output_tokens = config.get_max_output_tokens()
        self._client = client or AsyncOpenAI(
            api_key=_api_key(config),
            base_url=config.base_url,
        )

    # 更新后续请求的最大输出 token 数。
    def set_max_output_tokens(self, n: int) -> None:
        self._max_output_tokens = n

    # 把 Responses 风格的 tool schema 转成 Chat Completions 的嵌套 function 格式。
    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for t in tools:
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", t.get("input_schema", {})),
                    },
                }
            )
        return converted

    # 调用兼容 Chat Completions API 并接受 reasoning_content 与 tool_calls 增量。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        request_messages = build_chat_completion_messages(list(messages))
        if system:
            request_messages.insert(0, {"role": "system", "content": system})
        request: dict[str, Any] = {
            "model": self._config.model,
            "messages": request_messages,
            "max_tokens": self._max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request["tools"] = self._convert_tools(tools)

        # 按索引累积 streaming tool call 的状态。
        active_calls: dict[int, dict[str, str]] = {}
        reasoning_accum = ""
        completed = False
        # finish_reason="tool_calls" 标记：表示模型回合以工具调用结束，
        # 后续 usage chunk 到达时发 StreamComplete；无 usage chunk 时在循环外兜底。
        tool_calls_finished = False

        try:
            response_stream = await self._client.chat.completions.create(**request)
            async for chunk in response_stream:
                if not chunk.choices:
                    # 最后一个 chunk 只包含 usage 数据。
                    if chunk.usage and not completed:
                        details = getattr(chunk.usage, "prompt_tokens_details", None)
                        cache_read = getattr(details, "cached_tokens", 0) or 0 if details else 0
                        prompt_tokens = chunk.usage.prompt_tokens or 0
                        completed = True
                        # stop_reason 取决于上一阶段是否以 tool_calls 结束。
                        yield StreamComplete(
                            stop_reason="tool_calls" if tool_calls_finished else "end_turn",
                            input_tokens=max(prompt_tokens - cache_read, 0),
                            output_tokens=chunk.usage.completion_tokens or 0,
                            cache_read=cache_read,
                            cache_creation=0,
                        )
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # 文本内容。
                if delta and delta.content:
                    yield TextDelta(text=delta.content)

                # reasoning_content（兼容 provider 的非标准字段）。
                if delta:
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        reasoning_accum += rc
                        yield ThinkingDelta(text=rc)

                # tool call 增量，按 index 累积。
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in active_calls:
                            active_calls[idx] = {"id": "", "name": "", "args": ""}
                        call = active_calls[idx]

                        if tc.id:
                            call["id"] = tc.id
                        if tc.function and tc.function.name:
                            call["name"] = tc.function.name
                            yield ToolCallStart(
                                tool_name=call["name"],
                                tool_id=call["id"],
                            )
                        if tc.function and tc.function.arguments:
                            call["args"] += tc.function.arguments
                            yield ToolCallDelta(text=tc.function.arguments)

                # 结束原因。
                if choice.finish_reason in ("tool_calls", "stop"):
                    if reasoning_accum:
                        yield ThinkingComplete(thinking=reasoning_accum, signature="")
                        reasoning_accum = ""
                    if choice.finish_reason == "tool_calls":
                        # 按 index 排序完成每个 call，保持顺序一致。
                        for _idx, call in sorted(active_calls.items()):
                            try:
                                args = json.loads(call["args"]) if call["args"] else {}
                            except json.JSONDecodeError:
                                args = {}
                            yield ToolCallComplete(
                                tool_id=call["id"],
                                tool_name=call["name"],
                                arguments=args,
                            )
                        active_calls.clear()
                        # 标记工具调用回合结束；StreamComplete 留给 usage chunk 或循环外兜底，
                        # 这样能保留 usage chunk 中的 token 计数。
                        tool_calls_finished = True
                    if choice.finish_reason == "stop" and not completed:
                        completed = True
                        # finish_reason=stop 但无 usage chunk 时，发一个空完成事件。
                        yield StreamComplete(stop_reason="end_turn")
                    # 兼容 provider（如 DeepSeek）把 usage 与 finish_reason 放在同一 chunk：
                    # 标准OpenAI 分两个 chunk 发，DeepSeek 合并发送。此处统一在 finish_reason
                    # chunk 中提取 usage，避免 stream 结束后无 StreamComplete 事件。
                    if not completed and getattr(chunk, "usage", None):
                        usage = chunk.usage
                        details = getattr(usage, "prompt_tokens_details", None)
                        cache_read = getattr(details, "cached_tokens", 0) or 0 if details else 0
                        prompt_tokens = usage.prompt_tokens or 0
                        completed = True
                        yield StreamComplete(
                            stop_reason="tool_calls" if tool_calls_finished else "end_turn",
                            input_tokens=max(prompt_tokens - cache_read, 0),
                            output_tokens=usage.completion_tokens or 0,
                            cache_read=cache_read,
                            cache_creation=0,
                        )
        except Exception as error:
            raise _normalize_error(error) from error

        # 兜底：finish_reason="tool_calls" 后若无 usage chunk（部分兼容 provider 不发），
        # 补发一个零 token 的 StreamComplete 让 Agent Loop 正常推进到工具执行阶段。
        if not completed and tool_calls_finished:
            yield StreamComplete(stop_reason="tool_calls")

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


# context window 解析的第 2 层：对 anthropic 协议的 provider 从 /v1/models/{model}
# 自动拉取一次 max_input_tokens，并通过 set_fetched_context_window 缓存到 config 上。
# 完全尽力而为，绝不抛异常：非 anthropic provider、客户端构造失败、拉取失败或超时，
# 都让缓存保持不变，由 get_context_window() 降级到内置映射表 / 默认值。在启动时调用安全。
async def resolve_context_window(config: ProviderConfig) -> None:
    # 显式配置或已缓存的值优先级更高，跳过网络请求。
    if config.context_window > 0 or config._fetched_context_window > 0:
        return
    if config.protocol != "anthropic":
        return

    try:
        client = create_client(config)
    except Exception:
        return
    fetch = getattr(client, "fetch_model_context_window", None)
    if fetch is None:
        return

    try:
        window = await fetch()
    except Exception:
        window = None
    if window:
        config.set_fetched_context_window(window)
