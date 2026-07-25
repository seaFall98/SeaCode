"""对话历史的逻辑消息表示与回合管理。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ToolUseBlock:
    """表示一次助手工具调用的结构化块。"""

    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ToolResultBlock:
    """表示一次工具执行结果回灌给模型的块。

    非 frozen：上下文治理的 Layer 1 预算需要就地替换 content，
    以保证 prompt cache 前缀稳定。
    """

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


# 锚点之后追加消息的 token 估算使用的字符/token 比率。
# 与上下文治理模块的恢复附件启发值保持一致，全代码库统一使用同一比率。
_CHARS_PER_TOKEN: float = 3.5


def _message_chars(m: Message) -> int:
    """统计一条消息中所有可见文本的字符数，作为 token 估算的输入。"""
    n = len(m.content)
    for tb in m.thinking_blocks:
        n += len(tb.thinking)
    for tu in m.tool_uses:
        n += len(tu.tool_name) + len(json.dumps(tu.arguments, ensure_ascii=False))
    for tr in m.tool_results:
        n += len(tr.content)
    return n


def estimate_tokens(messages: list[Message]) -> int:
    """基于字符数对一组消息做 token 估算。

    刻意做得粗略——它只覆盖那些尚未锚定到真实 API 用量数值的消息，这部分的
    精确度本就无关紧要。统计内容包括消息正文、thinking、工具调用参数以及
    工具结果内容。
    """
    total = sum(_message_chars(m) for m in messages)
    return int(total / _CHARS_PER_TOKEN)


class ConversationManager:
    """维护已完成回合的历史并支持单活动回合的暂存与回滚。"""

    # 初始化完成历史和当前暂存回合；env_injected 标记会话级上下文是否已注入。
    def __init__(self) -> None:
        self._messages: list[Message] = []
        self._pending_user: Message | None = None
        self.env_injected: bool = False
        # 长期记忆（指令 + MEMORY.md 索引 + 当前日期）是否已注入会话头部。
        # 与 env_injected 平行管理，replace_history 后一并重置以支持压缩后重新注入。
        self.ltm_injected: bool = False
        # 真实用量锚点：baseline_tokens 是上一轮 API 计费的完整 prompt+output 大小
        # （input + cache_read + cache_creation + output）；anchor_count 是记录该数值时的
        # 消息数量。两者配合让 current_tokens() 在锚点以内信任 API 数据，只对之后追加
        # 的消息做字符估算。baseline_tokens == 0 表示"尚无锚点"（冷启动或刚压缩清空）。
        self.baseline_tokens: int = 0
        self.anchor_count: int = 0
        # API 报告的每轮真实 prompt 大小，保留用于向后兼容。
        # 现在与 baseline_tokens 一致（input + cache_read + cache_creation + output）。
        self.last_input_tokens: int = 0

    # 暴露底层消息列表引用，供上下文治理就地修改 ToolResultBlock.content。
    # 同时支持赋值替换整个历史（auto_compact 重建对话时使用）。
    @property
    def history(self) -> list[Message]:
        return self._messages

    # 赋值替换整个历史并重置 env_injected 与用量锚点。
    @history.setter
    def history(self, new_messages: list[Message]) -> None:
        self.replace_history(new_messages)

    # 返回已经完成的消息，供测试和界面读取。
    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    # 根据一次 API 响应钉下一个真实用量锚点。
    # baseline = input + cache_read + cache_creation + output；各家服务商返回的
    # input_tokens 已排除命中缓存的 token，所以三个 input 分量相加才是真正的 prompt
    # 大小；再加上 output 是因为 assistant 回复此刻已成为历史的一部分。anchor_count
    # 对齐到当前消息数量，后续新追加的消息就成了唯一需要估算的部分。
    def record_usage_anchor(
        self,
        input_tokens: int,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        self.baseline_tokens = (
            input_tokens + cache_read + cache_creation + output_tokens
        )
        self.anchor_count = len(self._messages)
        self.last_input_tokens = self.baseline_tokens

    # 对当前对话中的 token 数量做出最佳估算。
    # 有锚点时：baseline（真实用量）+ 仅对锚点之后追加的消息做字符估算。
    # 无锚点时（冷启动或刚压缩重置）：对整个历史做字符估算。
    def current_tokens(self) -> int:
        if self.baseline_tokens <= 0:
            return estimate_tokens(self._messages)
        tail = self._messages[self.anchor_count:]
        return self.baseline_tokens + estimate_tokens(tail)

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

    # 追加一条 system-reminder 包裹的 user 消息，用于轮次级提醒（Plan Mode/Hook/Mailbox）。
    def add_system_reminder(self, content: str) -> None:
        self._messages.append(
            Message(
                role="user",
                content=f"<system-reminder>\n{content}\n</system-reminder>",
            )
        )

    # 在历史头部插入会话级环境上下文；env_injected 标记确保只注入一次。
    def inject_environment(self, context: str) -> None:
        if not self.env_injected:
            self._messages.insert(0, Message(role="user", content=context))
            self.env_injected = True

    # 在环境上下文之后插入长期记忆（项目指令 + MEMORY.md 索引 + 当前日期）。
    # ltm_injected 标记确保只注入一次；插入位置在 env 之后（若已注入），
    # 否则在 history[0]。整体用 <system-reminder> 包裹并附"may or may not be relevant"提示，
    # 避免模型对长期记忆过度反应。空 instructions 与 memories 时跳过不注入。
    def inject_long_term_memory(self, instructions: str, memories: str) -> None:
        if self.ltm_injected:
            return
        sections: list[str] = []
        if instructions:
            sections.append(
                "# seacodeMd\n"
                "Codebase and user instructions are shown below. "
                "Be sure to adhere to these instructions. "
                "IMPORTANT: These instructions OVERRIDE any default behavior "
                "and you MUST follow them exactly as written.\n\n" + instructions
            )
        if memories:
            sections.append("# autoMemory\n" + memories)
        if not sections:
            return

        # 当前日期单独成段，让模型在引用"今天"时有权威来源。
        from datetime import date

        sections.append(f"# currentDate\nToday's date is {date.today().isoformat()}.")
        body = "\n\n".join(sections)
        wrapped = (
            "<system-reminder>\n"
            "As you answer the user's questions, you can use the following context:\n"
            + body
            + "\n\n      IMPORTANT: this context may or may not be relevant to your tasks."
            " You should not respond to this context unless it is highly relevant to your task.\n"
            "</system-reminder>"
        )
        # env 已注入时插在 env 之后（pos=1），否则插在头部（pos=0）。
        pos = 1 if self.env_injected else 0
        self._messages.insert(pos, Message(role="user", content=wrapped))
        self.ltm_injected = True

    # 替换整个历史并重置 env_injected 与用量锚点，支持第 07 步压缩后重新注入环境上下文。
    # 旧的锚点描述的是压缩前的对话记录，这里清零使 current_tokens() 退化为字符估算，
    # 直到下次 API 响应基于压缩后的历史重新锚定。
    def replace_history(self, new_messages: list[Message]) -> None:
        self._messages = new_messages
        self.env_injected = False
        self.ltm_injected = False
        self.baseline_tokens = 0
        self.anchor_count = 0
        self.last_input_tokens = 0

    # 返回当前完整历史，供调度器发起请求使用。
    def get_messages(self) -> list[Message]:
        return list(self._messages)

    # 丢弃末尾消息，用于调度器在失败时回滚不完整回合。
    def drop_last(self) -> None:
        if self._messages:
            self._messages.pop()
