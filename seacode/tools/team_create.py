# TeamCreate 工具：创建长期团队并按配置激活 Coordinator 模式。
"""TeamCreate 工具实现。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from seacode.agents.tool_filter import apply_coordinator_filter
from seacode.teams.coordinator import is_coordinator_mode
from seacode.teams.manager import TeamManager
from seacode.tools.base import Tool, ToolCategory, ToolResult


class TeamCreateParams(BaseModel):
    # team_name 由 Lead 提供；description 可选。
    team_name: str
    description: str = ""


class TeamCreateTool(Tool):
    # 创建团队工具；enable_coordinator_mode=True 时同步收敛 Lead 工具集。
    name = "TeamCreate"
    description = (
        "创建一个长期团队用于协调多个 Agent 协作。\n\n"
        "## 何时使用\n\n"
        "在以下情况主动使用此工具：\n"
        "- 用户明确要求使用团队、集群或多 Agent 协作\n"
        "- 用户希望多个 Agent 一起工作、协调或合作\n"
        "- 任务需要多个 Agent 之间的顺序或并行协作\n\n"
        "## 团队工作流\n\n"
        "1. 用 TeamCreate **创建团队**\n"
        "2. 用 Agent 工具传入 team_name 与 name **spawn 镟友** —— 这是创建长驻团队成员的必经路径\n"
        "3. 队友独立工作，通过 **SendMessage** 互相通信\n"
        "4. 队友完成一轮后会向 \"lead\" 发送结果，然后进入 idle 状态\n"
        "5. Lead 收集并整合所有队友的结果\n\n"
        "## 关键：spawn 队友\n\n"
        "要向团队添加成员，必须同时传入 team_name 与 name：\n"
        "```\nAgent({\n"
        '  "team_name": "<步骤 1 的团队名>",\n'
        '  "name": "<成员名，例如 reviewer>",\n'
        '  "prompt": "...",\n'
        '  "description": "..."\n'
        "})\n```\n"
        "不传 team_name 时 Agent 走一次性子 Agent 路径，"
        "会阻塞当前回合并直接返回 —— 它不会成为团队成员。\n\n"
        "## 队友 idle 状态\n\n"
        "队友每完成一轮就进入 idle，这是正常行为；向 idle 队友发消息会唤醒他们继续工作。\n\n"
        "## 通信\n\n"
        "- 用 SendMessage 按名字或 agent_id 与队友通信\n"
        "- 队友发来的消息会在每轮开始时作为 system-reminder 自动注入\n"
        "- 消息自动送达，不需要手动检查收件箱"
    )
    params_model = TeamCreateParams
    category = ToolCategory.COMMAND
    is_concurrency_safe = False

    def __init__(
        self,
        parent_agent: Any,
        team_manager: TeamManager,
        config: Any,
    ) -> None:
        self._parent_agent = parent_agent
        self._team_manager = team_manager
        self._config = config

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: TeamCreateParams = params  # type: ignore[assignment]
        teammate_mode = getattr(self._config, "teammate_mode", "")
        is_interactive = True

        # 先检测后端；fail-fast，避免团队已创建但 backend 信息缺失的中间态。
        try:
            backend = self._team_manager.detect_backend(teammate_mode, is_interactive)
        except Exception as e:
            return ToolResult(
                content=f"后端检测失败: {e}", is_error=True
            )

        try:
            team = await self._team_manager.create_team(
                tool_params.team_name,
                self._parent_agent.agent_id,
                tool_params.description,
                teammate_mode,
                is_interactive,
            )
        except Exception as e:
            return ToolResult(content=f"创建团队失败: {e}", is_error=True)

        # 把 team_manager 挂到 Lead agent 上，供后续 SendMessage 等工具按需取用。
        self._parent_agent._team_manager = self._team_manager

        coordinator_note = ""
        if is_coordinator_mode(
            getattr(self._config, "enable_coordinator_mode", False)
        ) and not getattr(self._parent_agent, "coordinator_mode", False):
            # 保存全量注册表快照，收敛当前 registry 为调度-only 白名单。
            self._parent_agent._full_registry = self._parent_agent.registry
            self._parent_agent.registry = apply_coordinator_filter(
                self._parent_agent.registry
            )
            self._parent_agent.coordinator_mode = True
            coordinator_note = "\n(coordinator mode 已激活，工具集收敛为调度-only)"

        return ToolResult(
            content=(
                f"团队 {team.name} 已创建\n"
                f"backend: {backend.value}\n"
                f"config: {team.config_path}{coordinator_note}"
            ),
            is_error=False,
        )
