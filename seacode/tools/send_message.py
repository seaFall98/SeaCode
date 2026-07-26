# SendMessage 工具：向团队成员发送消息（text / shutdown_request / shutdown_response）。
"""SendMessage 工具实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from seacode.teams.mailbox import create_message
from seacode.teams.registry import AgentNameRegistry
from seacode.teams.spawn_inprocess import LEAD_NAME
from seacode.tools.base import Tool, ToolCategory, ToolResult

if TYPE_CHECKING:
    from seacode.teams.manager import TeamManager


class SendMessageParams(BaseModel):
    # to 支持具体名称、agent_id 或 "*"（广播）；message 必填；summary 仅 text 类型必填。
    to: str
    message: str
    summary: str = ""
    message_type: str = "text"
    metadata: dict = Field(default_factory=dict)


class SendMessageTool(Tool):
    # 团队消息工具；to="*" 广播排除发送者，非 lead 发送时带 lead。
    name = "SendMessage"
    description = (
        "向团队成员发送消息，可按名字或 agent_id 指定接收人。"
        "to='*' 表示广播给所有队友；text 消息应附带 5-10 词的简短摘要。"
        "支持结构化类型：shutdown_request（请求队友关闭）、shutdown_response（关闭应答）。"
    )
    params_model = SendMessageParams
    category = ToolCategory.COMMAND
    is_concurrency_safe = True

    # 合法 message_type 集合；非法值返回 is_error。
    VALID_MESSAGE_TYPES = {"text", "shutdown_request", "shutdown_response"}

    def __init__(
        self,
        team_manager: TeamManager,
        team_name: str,
        from_agent_id: str,
        from_agent_name: str = "",
    ) -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._from_agent_id = from_agent_id
        self._from_agent_name = from_agent_name

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

        team = self._team_manager.get_team(self._team_name)
        if team is None:
            return ToolResult(
                content=f"团队不存在: {self._team_name}", is_error=True
            )

        mailbox = self._team_manager.get_mailbox(self._team_name)
        if mailbox is None:
            return ToolResult(
                content=f"邮箱不存在: {self._team_name}", is_error=True
            )

        from_agent = self._from_agent_id

        if tool_params.to == "*":
            # 广播：排除发送者；非 lead 发送时带 lead，并按 agent_id 唤醒各收件人 pane。
            recipients = [
                m.agent_id for m in team.members if m.agent_id != from_agent
            ]
            if from_agent != team.lead_agent_id:
                recipients.append(team.lead_agent_id)
            msg = create_message(
                from_agent=self._from_agent_name or from_agent,
                to_agent="*",
                content=tool_params.message,
                summary=tool_params.summary,
                message_type=tool_params.message_type,
                metadata=tool_params.metadata,
            )
            mailbox.broadcast(msg, recipients, exclude=from_agent)
            # 广播路径同样需要唤醒所有收件人 pane；Lead 主进程无 pane_id 时跳过。
            for recipient_id in recipients:
                if recipient_id != from_agent:
                    self._wake_pane(recipient_id)
        else:
            # Lead 是团队角色名，始终解析为当前团队保存的稳定标识。
            agent_id: str | None
            if tool_params.to == LEAD_NAME:
                agent_id = team.lead_agent_id
            else:
                agent_id = AgentNameRegistry.instance().resolve(tool_params.to)
            if agent_id is None:
                return ToolResult(
                    content=f"未知名称: {tool_params.to}", is_error=True
                )
            msg = create_message(
                from_agent=self._from_agent_name or from_agent,
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
            # 按 agent_id 唤醒目标 pane；in-process / Lead 主进程无 pane_id 时跳过。
            self._wake_pane(agent_id)

        return ToolResult(
            content=f"消息已发送给 {tool_params.to}", is_error=False
        )

    # 唤醒 pane 后端的 teammate；in-process 后端或 Lead 主进程无 pane_id 时跳过。
    def _wake_pane(self, agent_id: str) -> None:
        pane_id = self._team_manager.get_pane_id(agent_id)
        if pane_id:
            try:
                from seacode.teams.spawn_tmux import send_keys_to_pane

                send_keys_to_pane(pane_id, "")
            except Exception:
                pass
