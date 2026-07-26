"""AskUser 工具：向用户发起一组问题并等待回复。

支持 ``text`` / ``radio`` / ``select`` / ``checkbox`` 四种问题类型。
``execute`` 创建 ``asyncio.Future``、``await asyncio.wait_for(future, timeout=300)``、
5 分钟超时返回错误。``AskUserEvent`` 持 ``questions: list[dict]`` 与 ``future``，
``app.py`` 在 ``ToolResultEvent`` 后检查 ``_pending_event`` 挂起 ``InlineAskUserWidget``。

``should_defer=True`` 让工具调用在 Agent Loop 中延迟到下一轮（避免阻塞当前迭代）。
``AskUserQuestion`` 在 ``ALL_AGENT_DISALLOWED_TOOLS`` 中，定义式子 Agent 默认不能调用；
只有主 Agent 与 fork 子 Agent（用 ``clone_registry_for_fork`` 不过滤）可调用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from seacode.tools.base import Tool, ToolCategory, ToolResult

# 用户回复超时（秒）；超时后返回 is_error 让模型自适应。
_ASK_USER_TIMEOUT_SECONDS: int = 300


@dataclass
class QuestionItem:
    """单个问题项；type 决定渲染方式，options 用于 radio/select/checkbox。"""

    type: str  # text / radio / select / checkbox
    name: str
    message: str
    options: list[str] = field(default_factory=list)


class AskUserParams(BaseModel):
    """AskUser 工具参数；questions 列表至少一项。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    questions: list[QuestionItem] = Field(default_factory=list)


@dataclass
class AskUserEvent:
    """挂起中的提问事件；questions 是 dict 列表，future 由 TUI 回填。"""

    questions: list[dict[str, Any]]
    future: asyncio.Future[dict[str, str]]


class AskUserTool(Tool):
    """向用户发起一组问题的工具；5 分钟超时返回错误。"""

    name = "AskUserQuestion"
    description = (
        "Ask the user one or more questions when you need information "
        "that cannot be determined from code or context alone. "
        "Supports text input, radio (single select), select, and checkbox "
        "(multi select) question types."
    )
    category = ToolCategory.READ
    is_system_tool = True
    should_defer = True
    params_model = AskUserParams

    def __init__(self) -> None:
        # _pending_event 在 execute 设置，TUI 检查后挂起 InlineAskUserWidget。
        self._pending_event: AskUserEvent | None = None

    async def execute(
        self,
        params: BaseModel,
        conversation: Any = None,
        parent_agent: Any = None,
    ) -> ToolResult:
        ask_params: AskUserParams = params  # type: ignore[assignment]
        # 把 QuestionItem 序列化为 dict 供 TUI 渲染。
        questions_data: list[dict[str, Any]] = []
        for q in ask_params.questions:
            questions_data.append(
                {
                    "type": q.type,
                    "name": q.name,
                    "message": q.message,
                    "options": list(q.options),
                }
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str]] = loop.create_future()
        self._pending_event = AskUserEvent(questions=questions_data, future=future)

        try:
            answers = await asyncio.wait_for(
                future, timeout=_ASK_USER_TIMEOUT_SECONDS
            )
            output = "\n".join(f"{k}: {v}" for k, v in answers.items())
            return ToolResult(content=output or "(no answers)")
        except TimeoutError:
            return ToolResult(
                content="User did not respond within 5 minutes",
                is_error=True,
            )
        finally:
            # 无论成功或超时都清空 pending_event，避免后续 TUI 误挂起。
            self._pending_event = None

    # 兼容基类单参签名；内部走三参版本。
    async def _execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
        return await self.execute(params)
