"""Skill 执行器：inline 注入主对话、fork 创建子 Agent 隔离执行。"""

from __future__ import annotations

from typing import Any

from seacode.skills.parser import SkillDef, substitute_arguments


class SkillExecutor:
    """执行 Skill：inline 激活后注入主对话；fork 创建独立子 Agent 隔离执行。

    不负责 Skill 解析、加载、命令管理或通用 SubAgent 任务委派（第 12 步）。
    fork 创建的子 Agent 复用主 Agent 的 client/registry/protocol 等运行时依赖，
    但 permission_checker=None（权限由第 05 步既有机制在工具调用时统一生效）。
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    # inline 执行：替换参数 → 记录激活状态 → 返回 prompt 供 handler 发送。
    # prompt 作为用户消息注入主对话历史；recovery_state 记录用于压缩恢复。
    async def execute_inline(self, skill: SkillDef, args: str = "") -> str:
        prompt = substitute_arguments(skill.prompt_body, args)
        self._agent.activate_skill(skill.name, prompt)
        recovery_state = getattr(self._agent, "recovery_state", None)
        if recovery_state is not None:
            recovery_state.record_skill_invocation(skill.name, prompt)
        return prompt

    # fork 执行：替换参数 → 构建上下文 → 创建子 Agent → 收集流式文本。
    # 子 Agent 不持有主对话引用，结果作为系统消息返回主对话，不污染主对话历史。
    async def execute_fork(self, skill: SkillDef, args: str = "") -> str:
        # 延迟导入避免与 agent.py 形成循环依赖。
        from seacode.agent import Agent, ErrorEvent, StreamText
        from seacode.conversation import ConversationManager

        prompt = substitute_arguments(skill.prompt_body, args)
        fork_messages = self._build_fork_context(skill.context)

        fork_conv = ConversationManager()
        for msg in fork_messages:
            fork_conv._messages.append(msg)
        fork_conv.add_user_message(prompt)

        # 复用主 Agent 的运行时依赖；permission_checker=None 让子 Agent 工具调用不走 HITL。
        fork_agent = Agent(
            client=self._agent.client,
            registry=self._agent.registry,
            protocol=self._agent.protocol,
            work_dir=self._agent.work_dir,
            max_iterations=getattr(self._agent, "max_iterations", 100),
            context_window=getattr(self._agent, "context_window", 200_000),
            permission_checker=None,
        )

        result_parts: list[str] = []
        async for event in fork_agent.run(fork_conv):
            if isinstance(event, StreamText):
                result_parts.append(event.text)
            elif isinstance(event, ErrorEvent):
                result_parts.append(f"[error] {event.message}")
        return "".join(result_parts)

    # 按 context 字段构建 fork 上下文消息列表。
    # none 空；recent 最近 5 条内容消息（过滤空内容与工具结果）；full 200 字摘要单条 user 消息。
    def _build_fork_context(self, context: str) -> list[Any]:
        from seacode.conversation import Message

        conversation = getattr(self._agent, "conversation", None)
        if conversation is None:
            return []

        messages = conversation.get_messages()

        if context == "none":
            return []

        if context == "recent":
            filtered = [
                m
                for m in messages
                if getattr(m, "content", "")
                and not getattr(m, "tool_results", None)
            ]
            return filtered[-5:]

        # full（默认与 fallback）：每条消息截断到 200 字符拼成摘要。
        summaries: list[str] = []
        for m in messages:
            content = getattr(m, "content", "") or ""
            if not content:
                continue
            summaries.append(f"- {content[:200]}")
        if not summaries:
            return []
        summary = "\n".join(summaries)
        return [
            Message(
                role="user",
                content=f"## Previous conversation summary\n\n{summary}",
            )
        ]
