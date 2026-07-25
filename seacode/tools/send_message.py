# SendMessage 工具：向团队成员发送消息（text / shutdown_request / shutdown_response）。
"""SendMessage 工具实现。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from seacode.teams.mailbox import create_message
from seacode.teams.manager import TeamManager
from seacode.teams.registry import AgentNameRegistry
from seacode.tools.base import Tool, ToolCategory, ToolResult


class SendMessageParams(BaseModel):
    # to 支持具体名称或 "*"（广播）；message 必填；summary 仅 text 类型必填。
    to: str
    message: str
    summary: str = ""
    message_type: str = "text"
    metadata: dict = Field(default_factory=dict)


class SendMessageTool(Tool):
    # 团队消息工具；to="*" 广播排除发送者，非 lead 发送时带 lead。
    name = "SendMessage"
    description = "向团队成员发送消息（text / shutdown_request / shutdown_response）"
    params_model = SendMessageParams
    category = ToolCategory.COMMAND
    is_concurrency_safe = True

    # 合法 message_type 集合；非法值返回 is_error。
    VALID_MESSAGE_TYPES = {"text", "shutdown_request", "shutdown_response"}

    def __init__(self, parent_agent: Any, team_manager: TeamManager) -> None:
        self._parent_agent = parent_agent
        self._team_manager = team_manager

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: SendMessageParams = params  # type: ignore[assignment]

        if tool_params.message_type not in self.VALID_MESSAGE_TYPES:
            return ToolResult(
                content=(
                    f"非法 message_type: {tool_params.message_type}，"
                    f"必须是 {sorted(self.VALID_MESSAGE_TYPES)} 之一"
                ),
                is_error=True,
            )
        if tool_params.message_type == "text" and not tool_params.summary:
            return ToolResult(
                content="text 消息必须提供 summary", is_error=True
            )

        # 优先用 parent_agent.team_name，回退到 team_manager 反查。
        team_name = (
            getattr(self._parent_agent, "team_name", None)
            or self._team_manager.get_team_for_teammate(self._parent_agent.agent_id)
            or ""
        )
        if not team_name:
            return ToolResult(content="未找到当前团队", is_error=True)

        mailbox = self._team_manager.get_mailbox(team_name)
        from_agent = self._parent_agent.agent_id

        if tool_params.to == "*":
            # 广播：排除发送者；非 lead 发送时带 lead。
            team = self._team_manager.get_team(team_name)
            if team is None:
                return ToolResult(content="团队不存在", is_error=True)
            recipients = [
                m.agent_id for m in team.members if m.agent_id != from_agent
            ]
            if from_agent != team.lead_agent_id:
                recipients.append(team.lead_agent_id)
            msg = create_message(
                from_agent=from_agent,
                to_agent="*",
                content=tool_params.message,
                summary=tool_params.summary,
                message_type=tool_params.message_type,
                metadata=tool_params.metadata,
            )
            mailbox.broadcast(msg, recipients, exclude=from_agent)
        else:
            # 单发：通过 AgentNameRegistry 解析名称到 agent_id。
            agent_id = AgentNameRegistry.instance().resolve(tool_params.to)
            if agent_id is None:
                return ToolResult(
                    content=f"未知名称: {tool_params.to}", is_error=True
                )
            msg = create_message(
                from_agent=from_agent,
                to_agent=agent_id,
                content=tool_params.message,
                summary=tool_params.summary,
                message_type=tool_params.message_type,
                metadata=tool_params.metadata,
            )
            try:
                mailbox.write(agent_id, msg)
            except OSError as e:
                return ToolResult(
                    content=f"写入邮箱失败: {e}", is_error=True
                )
            # pane 后端唤醒：in-process 无需唤醒。
            self._wake_pane(team_name, tool_params.to)

        return ToolResult(
            content=f"消息已发送给 {tool_params.to}", is_error=False
        )

    # 唤醒 pane 后端的 teammate；in-process 后端无 pane_id 时跳过。
    def _wake_pane(self, team_name: str, member_name: str) -> None:
        pane_id = self._team_manager.get_pane_id(team_name, member_name)
        if pane_id:
            try:
                from seacode.teams.spawn_tmux import send_keys_to_pane

                send_keys_to_pane(pane_id, "")
            except Exception:
                pass
