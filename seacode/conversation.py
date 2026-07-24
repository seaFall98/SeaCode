"""纯对话回合的逻辑历史与原子提交。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Message:
    """表示本批次可发送给模型的一条纯文本消息。"""

    role: Literal["user", "assistant"]
    content: str


class ConversationManager:
    """仅把完成的用户与助手回合保存为逻辑历史。"""

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
