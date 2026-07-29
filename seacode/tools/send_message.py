# SendMessage 工具：向团队成员发送消息（text / shutdown_request / shutdown_response）。
"""SendMessage 工具实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from seacode.teams.mailbox import create_message
from seacode.teams.spawn_inprocess import LEAD_NAME
from seacode.tools.base import Tool, ToolCategory, ToolResult

if TYPE_CHECKING:
    from seacode.teams.manager import TeamManager


class SendMessageParams(BaseModel):
    # to 支持具体名称、agent_id 或 "*"（广播）；message 必填；summary 仅 text 类型必填。
    to: str
    message: str
    # Lead 有多个团队时必须显式选择；单团队保持省略参数的兼容调用。
    team_name: str | None = None
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
        team_name: str = "",
        from_agent_id: str = "",
        from_agent_name: str = "",
        parent_agent: Any = None,
    ) -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._from_agent_id = from_agent_id
        self._from_agent_name = from_agent_name
        # Lead 注册表实例在应用装配期创建；每回合由 app 注入当前 Agent。
        # teammate 工具不设置该字段，继续使用创建时绑定的稳定身份。
        self._parent_agent = parent_agent

    def _resolve_context(
        self, requested_team_name: str | None
    ) -> tuple[Any | None, str, str, str | None]:
        # Lead 必须按当前 Agent 身份选团队；teammate 只能留在创建时绑定的团队。
        if self._parent_agent is not None:
            from_agent = getattr(self._parent_agent, "agent_id", "")
            if not isinstance(from_agent, str) or not from_agent:
                return None, "", "", "Lead 身份未初始化，无法发送团队消息"
            from_agent_name = getattr(self._parent_agent, "agent_name", "")
            if not isinstance(from_agent_name, str) or not from_agent_name:
                from_agent_name = LEAD_NAME

            if requested_team_name:
                team = self._team_manager.get_team(requested_team_name)
                if team is None:
                    return None, "", "", f"团队不存在: {requested_team_name}"
                if team.lead_agent_id != from_agent:
                    return (
                        None,
                        "",
                        "",
                        f"当前 Lead 不属于团队: {requested_team_name}",
                    )
                return team, from_agent, from_agent_name, None

            teams = self._team_manager.get_teams_for_lead(from_agent)
            if not teams:
                return None, "", "", "当前 Lead 没有可发送消息的团队"
            if len(teams) > 1:
                return (
                    None,
                    "",
                    "",
                    "当前 Lead 有多个团队，请在 SendMessage 中提供 team_name",
                )
            return teams[0], from_agent, from_agent_name, None

        if requested_team_name and self._team_name and requested_team_name != self._team_name:
            return (
                None,
                "",
                "",
                f"当前成员只能向所属团队发送消息: {self._team_name}",
            )
        team_name = requested_team_name or self._team_name
        if not team_name:
            return None, "", "", "发送者缺少团队上下文"
        team = self._team_manager.get_team(team_name)
        if team is None:
            return None, "", "", f"团队不存在: {team_name}"
        from_agent = self._from_agent_id
        is_member = any(member.agent_id == from_agent for member in team.members)
        if from_agent != team.lead_agent_id and not is_member:
            return None, "", "", f"发送者不属于团队: {team_name}"
        return team, from_agent, self._from_agent_name or from_agent, None

    @staticmethod
    def _resolve_recipient_id(team: Any, recipient: str) -> str | None:
        # 名称和 agent ID 都只在已选团队的成员列表中解析，避免跨团队误投。
        if recipient == LEAD_NAME:
            return team.lead_agent_id
        for member in team.members:
            if recipient == member.name or recipient == member.agent_id:
                return member.agent_id
        return None

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

        team, from_agent, from_agent_name, context_error = self._resolve_context(
            tool_params.team_name
        )
        if context_error is not None:
            return ToolResult(
                content=context_error, is_error=True
            )
        assert team is not None

        mailbox = self._team_manager.get_mailbox(team.name)
        if mailbox is None:
            return ToolResult(
                content=f"邮箱不存在: {team.name}", is_error=True
            )

        if tool_params.to == "*":
            # 广播：排除发送者；非 lead 发送时带 lead，并按 agent_id 唤醒各收件人 pane。
            recipients = [
                m.agent_id for m in team.members if m.agent_id != from_agent
            ]
            if from_agent != team.lead_agent_id:
                recipients.append(team.lead_agent_id)
            msg = create_message(
                from_agent=from_agent_name,
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
            agent_id = self._resolve_recipient_id(team, tool_params.to)
            if agent_id is None:
                return ToolResult(
                    content=f"团队 {team.name} 中不存在收件人: {tool_params.to}",
                    is_error=True,
                )
            msg = create_message(
                from_agent=from_agent_name,
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
