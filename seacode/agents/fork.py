"""Fork 路径：复制父对话历史并注入 Fork 提示词。

Fork 子 Agent 继承父对话全部消息（深拷贝），但被强约束为不可再次 fork、
不可对话、必须直接使用工具、最终报告须以 ``Scope:`` 开头且不超 500 字符。

嵌套拦截采用双层防御：(a) ``AgentTool.query_source == FORK_QUERY_SOURCE`` 时
fork 路径直接拒绝；(b) ``build_forked_messages`` 在父对话历史中检测到
``<fork_boilerplate>`` 标签时抛 ``ForkError``。
"""

from __future__ import annotations

import copy
from typing import Any

# Fork 子 Agent 的强约束提示词；用 <fork_boilerplate> 标签包裹便于嵌套检测。
FORK_BOILERPLATE: str = """<fork_boilerplate>
You are a forked worker. You MUST NOT:
- Call the Agent tool to fork again
- Ask the user questions or request confirmations
- Engage in conversation

You MUST:
- Use tools directly to complete the task
- Report your final result starting with "Scope:" and keep it under 500 characters
</fork_boilerplate>"""

# Fork 子 Agent 的 query_source 标记，用于 AgentTool 双层嵌套拦截。
FORK_QUERY_SOURCE: str = "agent:builtin:fork"


class ForkError(Exception):
    """Fork 路径不合法时抛出，例如检测到嵌套 fork。"""


# 构造 fork 子 Agent 的消息列表：深拷贝父对话历史 + 注入 FORK_BOILERPLATE 与 task。
# 父对话历史含 <fork_boilerplate> 时抛 ForkError，与 AgentTool.query_source 共同构成
# 双层拦截。父对话最后一条 assistant 的 pending tool_uses 会注入 interrupted 占位
# ToolResult，避免 provider 拒绝未完成 tool_use 的请求。
def build_forked_messages(parent_conversation: Any, task: str) -> list[Any]:
    # 嵌套检测：扫描父对话历史文本，若已含 <fork_boilerplate> 视为嵌套 fork。
    for msg in parent_conversation.messages:
        content = getattr(msg, "content", "") or ""
        if "<fork_boilerplate>" in content:
            raise ForkError("nested fork detected")
        # tool_uses / tool_results 也参与扫描。
        for tu in getattr(msg, "tool_uses", []) or []:
            if "<fork_boilerplate>" in str(getattr(tu, "arguments", "")):
                raise ForkError("nested fork detected")

    messages = copy.deepcopy(list(parent_conversation.messages))

    # 为父对话最后一条 assistant message 的 pending tool_uses 注入 interrupted 占位。
    # pending 指助手消息持有 tool_uses 但对话中尚无对应 tool_result。
    if messages:
        last = messages[-1]
        if getattr(last, "role", None) == "assistant" and getattr(
            last, "tool_uses", None
        ):
            _inject_interrupted_tool_results(last)

    # 追加 FORK_BOILERPLATE 与 task 作为新的 user message。
    # 复用 ConversationManager 的 Message 类型以保持类型一致。
    from seacode.conversation import Message

    fork_user = Message(
        role="user", content=f"{FORK_BOILERPLATE}\n\nTask:\n{task}"
    )
    messages.append(fork_user)
    return messages


# 为父对话最后一条 assistant message 的 pending tool_uses 注入 interrupted 占位
# ToolResult，避免 provider 拒绝未完成 tool_use 的请求。
def _inject_interrupted_tool_results(assistant_msg: Any) -> None:
    tool_uses = getattr(assistant_msg, "tool_uses", []) or []
    if not tool_uses:
        return
    # 检查当前消息是否已含 tool_results（多块消息时可能已自带）。
    existing_results = getattr(assistant_msg, "tool_results", []) or []
    existing_ids = {tr.tool_use_id for tr in existing_results}
    # 构造 interrupted 占位 ToolResultBlock。
    from seacode.conversation import ToolResultBlock

    new_results: list[ToolResultBlock] = list(existing_results)
    for tu in tool_uses:
        if tu.tool_use_id in existing_ids:
            continue
        new_results.append(
            ToolResultBlock(
                tool_use_id=tu.tool_use_id,
                content="[interrupted: parent agent cancelled before tool result returned]",
                is_error=True,
            )
        )
    # Message 是 frozen dataclass，无法就地修改；用 object.__setattr__ 绕过冻结。
    try:
        object.__setattr__(assistant_msg, "tool_results", new_results)
    except (AttributeError, TypeError):
        # frozen dataclass 不允许 setattr；忽略，由下游 provider 适配器处理。
        pass
