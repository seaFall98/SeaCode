"""LoadSkill 系统工具：按需加载并激活 Skill 完整 SOP。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from seacode.tools.base import Tool, ToolCategory, ToolResult


class _LoadSkillParams(BaseModel):
    """LoadSkill 工具参数：仅 name 一个必填字段。"""

    name: str = Field(..., description="要加载的 Skill 名称")


class LoadSkill(Tool):
    """按需加载并激活 Skill 完整 SOP 的系统工具。

    通过 set_loader/set_agent 注入依赖；execute 按 name 获取 Skill 后
    调 agent.activate_skill 激活，返回 # Skill: {name}\\n\\n{body} 作为 tool result。
    未知 name 返回 is_error=True 与可用 Skill 列表。
    """

    name: str = "LoadSkill"
    description: str = (
        "按名称加载并激活与当前任务匹配的 Skill。"
        "当用户请求与可用 Skill 相符时调用此工具。"
        "工具会返回完整 SOP 正文，必须在后续执行中遵循其中的指令。"
    )
    params_model: type[BaseModel] = _LoadSkillParams
    category: ToolCategory = ToolCategory.READ
    is_system_tool: bool = True

    def __init__(self) -> None:
        self._loader: Any = None
        self._agent: Any = None

    # 注入 SkillLoader 引用。
    def set_loader(self, loader: Any) -> None:
        self._loader = loader

    # 注入主 Agent 引用，用于 activate_skill 激活。
    def set_agent(self, agent: Any) -> None:
        self._agent = agent

    # 按 name 加载 Skill 并激活；未初始化或未知 name 返回 is_error=True。
    async def execute(self, params: BaseModel) -> ToolResult:
        if self._loader is None or self._agent is None:
            return ToolResult(
                content="LoadSkill 未初始化：缺少 loader 或 agent", is_error=True
            )
        name = params.name  # type: ignore[attr-defined]
        skill = self._loader.get(name)
        if skill is None:
            catalog = "\n".join(
                f"- {n}: {d}" for n, d in self._loader.get_catalog()
            )
            return ToolResult(
                content=f"未知 Skill：{name}，可用：\n{catalog}",
                is_error=True,
            )
        # 激活时存入 active_skills 的也是原始 prompt_body，不替换 $ARGUMENTS；
        # 参数由后续用户消息提供，环境上下文按原样注入。
        self._agent.activate_skill(skill.name, skill.prompt_body)
        recovery_state = getattr(self._agent, "recovery_state", None)
        if recovery_state is not None:
            recovery_state.record_skill_invocation(skill.name, skill.prompt_body)
        return ToolResult(
            content=f"# Skill: {skill.name}\n\n{skill.prompt_body}",
            is_error=False,
        )
