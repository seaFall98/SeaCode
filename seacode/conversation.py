"""对话历史的逻辑消息表示与回合管理。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ToolUseBlock:
    """表示一次助手工具调用的结构化块。"""

    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    """表示一次工具执行结果回灌给模型的块。"""

    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ThinkingBlock:
    """表示一段模型思考及其签名，用于回传以保持思考连续性。"""

    thinking: str
    signature: str


@dataclass(frozen=True)
class Message:
    """表示一条可发送给模型的消息，支持纯文本、工具调用与思考块。"""

    role: Literal["user", "assistant"]
    content: str = ""
    tool_uses: list[ToolUseBlock] = field(default_factory=list)
    tool_results: list[ToolResultBlock] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)


class ConversationManager:
    """维护已完成回合的历史并支持单活动回合的暂存与回滚。"""

    # 初始化完成历史和当前暂存回合。
    def __init__(self) -> None:
        self._messages: list[Message] = []
        self._pending_user: Message | None = None

    # 返回已经完成的消息，供测试和界面读取。
    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    # 开始一个尚未确认成功的用户回合。
    def begin_turn(self, content: str) -> None:
        if self._pending_user is not None:
            raise RuntimeError("A conversation turn is already active")
        self._pending_user = Message(role="user", content=content)

    # 返回当前请求可见的完成历史和待发送用户消息。
    def messages_for_request(self) -> tuple[Message, ...]:
        if self._pending_user is None:
            raise RuntimeError("No active conversation turn")
        return (*self._messages, self._pending_user)

    # 只在 Provider 正常结束后原子提交本回合。
    def complete_turn(self, assistant_content: str) -> None:
        if self._pending_user is None:
            raise RuntimeError("No active conversation turn")
        assistant = Message(role="assistant", content=assistant_content)
        self._messages.extend((self._pending_user, assistant))
        self._pending_user = None

    # 在失败或取消时丢弃未完成回合，避免污染后续请求。
    def abandon_turn(self) -> None:
        self._pending_user = None

    # 直接追加一条用户消息，供单轮调度器使用。
    def add_user_message(self, content: str) -> None:
        self._messages.append(Message(role="user", content=content))

    # 追加一条助手消息，可携带工具调用与思考块。
    def add_assistant_message(
        self,
        content: str,
        tool_uses: list[ToolUseBlock] | None = None,
        thinking_blocks: list[ThinkingBlock] | None = None,
    ) -> None:
        self._messages.append(
            Message(
                role="assistant",
                content=content,
                tool_uses=tool_uses or [],
                thinking_blocks=thinking_blocks or [],
            )
        )

    # 追加一条携带工具结果的用户消息，回灌给模型。
    def add_tool_results_message(self, tool_results: list[ToolResultBlock]) -> None:
        self._messages.append(Message(role="user", content="", tool_results=tool_results))

    # 返回当前完整历史，供调度器发起请求使用。
    def get_messages(self) -> list[Message]:
        return list(self._messages)

    # 丢弃末尾消息，用于调度器在失败时回滚不完整回合。
    def drop_last(self) -> None:
        if self._messages:
            self._messages.pop()
