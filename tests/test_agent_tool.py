"""AgentTool 单元测试：覆盖定义式路径、Fork 路径、_select_llm 与保留入口分支。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from seacode.agents.fork import FORK_QUERY_SOURCE, ForkError
from seacode.agents.parser import AgentDef
from seacode.tools import ToolRegistry
from seacode.tools.agent_tool import AgentTool, AgentToolParams
from seacode.tools.base import Tool, ToolCategory, ToolResult

# ---------------------------------------------------------------------------
# 测试辅助 fake 类
# ---------------------------------------------------------------------------


# 假 AgentLoader：可配置 get 返回值与 list_agents 返回值。
class _FakeLoader:
    def __init__(
        self,
        agent_def: AgentDef | None = None,
        available: list[tuple[str, str]] | None = None,
    ) -> None:
        self._agent_def = agent_def
        self._available = available or []
        self.get_calls: list[str] = []

    def get(self, name: str) -> AgentDef | None:
        self.get_calls.append(name)
        return self._agent_def

    def list_agents(self) -> list[tuple[str, str]]:
        return list(self._available)


# 假 TaskManager：记录 launch 调用参数并返回固定 task_id。
class _FakeTaskManager:
    def __init__(self, task_id: str = "abc12345") -> None:
        self._task_id = task_id
        self.launch_calls: list[dict[str, Any]] = []

    async def launch(
        self,
        agent: Any,
        task: str,
        name: str,
        fork_conversation: Any = None,
    ) -> str:
        self.launch_calls.append(
            {
                "agent": agent,
                "task": task,
                "name": name,
                "fork_conversation": fork_conversation,
            }
        )
        return self._task_id


# 假 TraceManager：记录 create/update/complete 调用。
class _FakeTraceManager:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.complete_calls: list[str] = []

    def create(self, agent_type: str, parent_id: Any, trace_id: str) -> Any:
        class _FakeNode:
            def __init__(self) -> None:
                self.agent_id = "fake-agent-id"
                self.agent_type = agent_type
                self.parent_id = parent_id
                self.trace_id = trace_id

        self.create_calls.append(
            {
                "agent_type": agent_type,
                "parent_id": parent_id,
                "trace_id": trace_id,
            }
        )
        return _FakeNode()

    def update(self, agent_id: str, **kwargs: Any) -> None:
        self.update_calls.append({"agent_id": agent_id, **kwargs})

    def complete(self, agent_id: str, status: str = "completed") -> None:
        self.complete_calls.append(agent_id)


# 假父 Agent：提供 AgentTool 所需的最小属性集合。
class _FakeParent:
    def __init__(self, *, replacement_state: Any = None) -> None:
        self.agent_id = "parent-id"
        self.trace_id = "trace-id"
        self.max_iterations = 100
        self.protocol = "anthropic"
        self.work_dir = "."
        self.context_window = 200_000
        self.instructions_content = ""
        self.permission_mode = None
        self.permission_checker = None
        self.hook_engine = None
        self.client = object()
        self._full_registry = ToolRegistry()
        self.replacement_state = replacement_state


# 假子 Agent：记录 run_to_completion 调用并返回固定文本。
class _FakeSubAgent:
    def __init__(self, result: str = "done") -> None:
        self._result = result
        self.last_output: str = ""
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.run_calls: list[tuple[Any, ...]] = []
        self.agent_id: str = ""
        self.parent_id: str | None = None
        self.trace_id: str | None = None
        self.replacement_state: Any = None

    async def run_to_completion(self, task: str, conversation: Any = None) -> str:
        self.run_calls.append((task, conversation))
        self.last_output = self._result
        return self._result


# 假工具：用于构造父注册表。
class _FakeTool(Tool):
    name = "FakeTool"
    description = "fake"
    category = ToolCategory.READ
    params_model = BaseModel

    async def execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
        return ToolResult(content="")


# 构造 AgentTool 实例；通过覆写 _create_sub_agent 返回假子 Agent。
def _make_tool(
    *,
    loader: _FakeLoader | None = None,
    task_manager: _FakeTaskManager | None = None,
    trace_manager: _FakeTraceManager | None = None,
    parent: _FakeParent | None = None,
    enable_fork: bool = False,
    provider_config: Any = None,
    sub_agent: _FakeSubAgent | None = None,
) -> tuple[AgentTool, _FakeSubAgent]:
    loader = loader or _FakeLoader()
    task_manager = task_manager or _FakeTaskManager()
    trace_manager = trace_manager or _FakeTraceManager()
    parent = parent or _FakeParent()
    fake_sub = sub_agent or _FakeSubAgent()

    tool = AgentTool(
        agent_loader=loader,
        task_manager=task_manager,
        trace_manager=trace_manager,
        parent_agent=parent,
        enable_fork=enable_fork,
        provider_config=provider_config,
    )
    # 覆写 _create_sub_agent 避免实例化真实 Agent。
    tool._create_sub_agent = lambda **kwargs: fake_sub  # type: ignore[method-assign]
    return tool, fake_sub


# 构造合法 AgentDef；isolation 默认空串。
def _make_def(
    *,
    agent_type: str = "Explore",
    background: bool = False,
    isolation: str = "",
    model: str = "inherit",
    tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
) -> AgentDef:
    return AgentDef(
        agent_type=agent_type,
        when_to_use="探索",
        system_prompt="body",
        tools=tools or [],
        disallowed_tools=disallowed_tools or [],
        model=model,
        background=background,
        isolation=isolation,
        source="project",
    )


# ---------------------------------------------------------------------------
# 定义式路径
# ---------------------------------------------------------------------------


# 验证定义式路径合法 subagent_type 取 AgentDef 并前台同步返回文本。
# fake loader 返回 AgentDef，fake 子 Agent 返回 "done"，断言 result.content == "done"。
async def test_defined_path_returns_sync_text() -> None:
    loader = _FakeLoader(agent_def=_make_def())
    tool, fake_sub = _make_tool(
        loader=loader, sub_agent=_FakeSubAgent(result="done")
    )
    params = AgentToolParams(subagent_type="Explore", prompt="do task")

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is False
    assert result.content == "done"
    assert loader.get_calls == ["Explore"]
    # 前台同步路径调用 run_to_completion(prompt, conversation=None)。
    assert len(fake_sub.run_calls) == 1
    assert fake_sub.run_calls[0][0] == "do task"


# 验证定义式路径未知 subagent_type 返回 is_error 与可用列表。
# fake loader 返回 None 且可用列表含 Explore，断言 is_error=True 且 output 含 "未知" 与 "Explore"。
async def test_defined_path_unknown_type_returns_error_with_available() -> None:
    loader = _FakeLoader(
        agent_def=None, available=[("Explore", "探索"), ("Plan", "规划")]
    )
    tool, _ = _make_tool(loader=loader)
    params = AgentToolParams(subagent_type="Unknown", prompt="do task")

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is True
    assert "未知子 Agent 类型" in result.content
    assert "Explore" in result.content
    assert "Plan" in result.content


# 验证定义式路径 run_in_background=True 走后台并返回 task_id。
# fake task_manager 返回 "abc12345"，断言 output 含 task_id 与 "不要 wait"。
async def test_defined_path_background_returns_task_id() -> None:
    loader = _FakeLoader(agent_def=_make_def())
    task_manager = _FakeTaskManager(task_id="abc12345")
    tool, fake_sub = _make_tool(loader=loader, task_manager=task_manager)
    params = AgentToolParams(
        subagent_type="Explore", prompt="do task", run_in_background=True
    )

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is False
    assert "abc12345" in result.content
    assert "不要 wait" in result.content
    # 后台路径不直接调用 run_to_completion，而是由 task_manager.launch 启动。
    assert len(fake_sub.run_calls) == 0
    assert len(task_manager.launch_calls) == 1
    assert task_manager.launch_calls[0]["task"] == "do task"


# 验证定义式路径 definition.background=True 默认走后台。
# AgentDef.background=True，断言 task_manager.launch 被调用。
async def test_defined_path_definition_background_flag() -> None:
    loader = _FakeLoader(agent_def=_make_def(background=True))
    task_manager = _FakeTaskManager()
    tool, _ = _make_tool(loader=loader, task_manager=task_manager)
    params = AgentToolParams(subagent_type="Explore", prompt="do task")

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is False
    assert len(task_manager.launch_calls) == 1


# 验证定义式路径前台完成后调用 trace_manager.update 与 complete。
# fake trace_manager 记录调用，断言 update 与 complete 都被调用。
async def test_defined_path_foreground_updates_trace() -> None:
    loader = _FakeLoader(agent_def=_make_def())
    trace_manager = _FakeTraceManager()
    tool, _ = _make_tool(loader=loader, trace_manager=trace_manager)
    params = AgentToolParams(subagent_type="Explore", prompt="do task")

    await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert len(trace_manager.create_calls) == 1
    assert trace_manager.create_calls[0]["agent_type"] == "Explore"
    assert len(trace_manager.update_calls) == 1
    assert len(trace_manager.complete_calls) == 1


# ---------------------------------------------------------------------------
# Fork 路径
# ---------------------------------------------------------------------------


# 验证 fork 路径 enable_fork=False 拒绝。
# subagent_type 为空且 enable_fork=False，断言 is_error=True 且 output 含 "fork 未启用"。
async def test_fork_path_disabled_returns_error() -> None:
    tool, _ = _make_tool(enable_fork=False)
    params = AgentToolParams(subagent_type="", prompt="do task")

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is True
    assert "fork 未启用" in result.content


# 验证 fork 路径 query_source=FORK_QUERY_SOURCE 拒绝。
# enable_fork=True 但 query_source 标记为 fork，断言 is_error=True 且 output 含 "不能再次 fork"。
async def test_fork_path_query_source_rejects_refork() -> None:
    tool, _ = _make_tool(enable_fork=True)
    tool.query_source = FORK_QUERY_SOURCE
    params = AgentToolParams(subagent_type="", prompt="do task")

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is True
    assert "不能再次 fork" in result.content


# 验证 fork 路径 build_forked_messages 抛 ForkError 返回错误。
# 用 monkeypatch 让 build_forked_messages 抛 ForkError，断言 is_error=True 且 output 含错误信息。
async def test_fork_path_fork_error_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, _ = _make_tool(enable_fork=True)

    def _raise_fork_error(*args: Any, **kwargs: Any) -> Any:
        raise ForkError("nested fork detected")

    monkeypatch.setattr(
        "seacode.tools.agent_tool.build_forked_messages", _raise_fork_error
    )
    params = AgentToolParams(subagent_type="", prompt="do task")

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is True
    assert "nested fork" in result.content


# 验证 fork 路径默认走后台并返回 task_id。
# enable_fork=True，fake conversation 提供合法 messages，断言 task_manager.launch 被调用。
async def test_fork_path_default_background_returns_task_id() -> None:
    tool, _ = _make_tool(enable_fork=True)
    task_manager = _FakeTaskManager(task_id="fork1234")
    tool.task_manager = task_manager
    # 构造合法 conversation 供 build_forked_messages 使用。
    from seacode.conversation import ConversationManager

    conv = ConversationManager()
    conv.add_user_message("hello")
    conv.add_assistant_message("hi")
    params = AgentToolParams(subagent_type="", prompt="fork task")

    result = await tool.execute(params, conversation=conv, parent_agent=tool.parent_agent)

    assert result.is_error is False
    assert "fork1234" in result.content
    assert len(task_manager.launch_calls) == 1
    # fork 路径 launch 传 task="" 与 fork_conversation。
    assert task_manager.launch_calls[0]["task"] == ""
    assert task_manager.launch_calls[0]["fork_conversation"] is not None


# 验证 fork 子 Agent 继承 replacement_state。
# 父 Agent 持 replacement_state，fork 后断言子 Agent 的 replacement_state 是深拷贝结果。
async def test_fork_path_inherits_replacement_state() -> None:
    parent = _FakeParent(replacement_state={"key": "value"})
    tool, fake_sub = _make_tool(parent=parent, enable_fork=True)
    from seacode.conversation import ConversationManager

    conv = ConversationManager()
    conv.add_user_message("hello")
    conv.add_assistant_message("hi")
    params = AgentToolParams(subagent_type="", prompt="fork task")

    await tool.execute(params, conversation=conv, parent_agent=parent)

    # fork 子 Agent 的 replacement_state 应为父 replacement_state 的深拷贝。
    assert fake_sub.replacement_state == {"key": "value"}
    assert fake_sub.replacement_state is not parent.replacement_state


# ---------------------------------------------------------------------------
# _select_llm
# ---------------------------------------------------------------------------


# 验证 _select_llm model=None 且 definition.model=inherit 回退父 client。
# params.model=None，definition.model="inherit"，断言返回 parent.client。
def test_select_llm_none_model_falls_back_to_parent_client() -> None:
    tool, _ = _make_tool(provider_config=object())
    parent = tool.parent_agent
    params = AgentToolParams(subagent_type="Explore", prompt="task")
    definition = _make_def(model="inherit")

    client = tool._select_llm(params, definition, parent)

    assert client is parent.client


# 验证 _select_llm provider_config=None 时回退父 client。
# params.model="haiku" 但 provider_config=None，断言返回 parent.client。
def test_select_llm_no_provider_config_falls_back_to_parent_client() -> None:
    tool, _ = _make_tool(provider_config=None)
    parent = tool.parent_agent
    params = AgentToolParams(subagent_type="Explore", prompt="task", model="haiku")
    definition = _make_def(model="inherit")

    client = tool._select_llm(params, definition, parent)

    assert client is parent.client


# 验证 _select_llm haiku 别名映射到具体模型 id。
# monkeypatch create_client 记录 model_id，断言映射到 claude-haiku-4-5。
def test_select_llm_haiku_alias_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, _ = _make_tool(provider_config=_make_provider_config())
    parent = tool.parent_agent
    params = AgentToolParams(subagent_type="Explore", prompt="task", model="haiku")
    definition = _make_def(model="inherit")

    captured: list[str] = []

    def _fake_create_client(cfg: Any) -> Any:
        captured.append(cfg.model)
        return object()

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    tool._select_llm(params, definition, parent)

    assert captured == ["claude-haiku-4-5"]


# 验证 _select_llm sonnet 别名映射到具体模型 id。
# monkeypatch create_client 记录 model_id，断言映射到 claude-sonnet-4-5。
def test_select_llm_sonnet_alias_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, _ = _make_tool(provider_config=_make_provider_config())
    parent = tool.parent_agent
    params = AgentToolParams(subagent_type="Explore", prompt="task", model="sonnet")
    definition = _make_def(model="inherit")

    captured: list[str] = []

    def _fake_create_client(cfg: Any) -> Any:
        captured.append(cfg.model)
        return object()

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    tool._select_llm(params, definition, parent)

    assert captured == ["claude-sonnet-4-5"]


# 验证 _select_llm opus 别名映射到具体模型 id。
# monkeypatch create_client 记录 model_id，断言映射到 claude-opus-4-1。
def test_select_llm_opus_alias_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, _ = _make_tool(provider_config=_make_provider_config())
    parent = tool.parent_agent
    params = AgentToolParams(subagent_type="Explore", prompt="task", model="opus")
    definition = _make_def(model="inherit")

    captured: list[str] = []

    def _fake_create_client(cfg: Any) -> Any:
        captured.append(cfg.model)
        return object()

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    tool._select_llm(params, definition, parent)

    assert captured == ["claude-opus-4-1"]


# 验证 _select_llm 非别名模型名直通。
# params.model="claude-sonnet-4"，断言 create_client 收到的 model_id 是原值。
def test_select_llm_non_alias_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, _ = _make_tool(provider_config=_make_provider_config())
    parent = tool.parent_agent
    params = AgentToolParams(
        subagent_type="Explore", prompt="task", model="claude-sonnet-4"
    )
    definition = _make_def(model="inherit")

    captured: list[str] = []

    def _fake_create_client(cfg: Any) -> Any:
        captured.append(cfg.model)
        return object()

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    tool._select_llm(params, definition, parent)

    assert captured == ["claude-sonnet-4"]


# 验证 _select_llm create_client 失败回退父 client。
# monkeypatch create_client 抛异常，断言返回 parent.client。
def test_select_llm_create_client_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, _ = _make_tool(provider_config=_make_provider_config())
    parent = tool.parent_agent
    params = AgentToolParams(subagent_type="Explore", prompt="task", model="haiku")
    definition = _make_def(model="inherit")

    def _raise(cfg: Any) -> Any:
        raise RuntimeError("create failed")

    monkeypatch.setattr("seacode.client.create_client", _raise)
    client = tool._select_llm(params, definition, parent)

    assert client is parent.client


# 验证 _select_llm definition.model 非 inherit 时作为次优覆盖。
# params.model=None，definition.model="haiku"，断言映射到 haiku 对应 id。
def test_select_llm_definition_model_used_when_params_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, _ = _make_tool(provider_config=_make_provider_config())
    parent = tool.parent_agent
    params = AgentToolParams(subagent_type="Explore", prompt="task", model=None)
    definition = _make_def(model="haiku")

    captured: list[str] = []

    def _fake_create_client(cfg: Any) -> Any:
        captured.append(cfg.model)
        return object()

    monkeypatch.setattr("seacode.client.create_client", _fake_create_client)
    tool._select_llm(params, definition, parent)

    assert captured == ["claude-haiku-4-5"]


# ---------------------------------------------------------------------------
# 保留入口分支
# ---------------------------------------------------------------------------


# 验证 team_name 分支在 team_manager 未注入时返回 is_error。
# params.team_name="team1"，未 set_team_manager，断言 is_error=True 且 output 含 "未初始化"。
async def test_team_name_branch_returns_error() -> None:
    tool, _ = _make_tool()
    params = AgentToolParams(
        subagent_type="Explore", prompt="task", team_name="team1"
    )

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is True
    assert "未初始化" in result.content


# 验证 isolation=worktree 分支在 worktree_manager 未注入时返回 is_error。
# fake loader 返回 isolation=worktree 的 AgentDef，未 set_worktree_manager，
# 断言 is_error=True 且 content 含 "未初始化" 提示。
async def test_isolation_worktree_branch_returns_error() -> None:
    loader = _FakeLoader(agent_def=_make_def(isolation="worktree"))
    tool, _ = _make_tool(loader=loader)
    params = AgentToolParams(subagent_type="Explore", prompt="task")

    result = await tool.execute(params, conversation=None, parent_agent=tool.parent_agent)

    assert result.is_error is True
    assert "未初始化" in result.content


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


# 构造最小 ProviderConfig 供 _select_llm 测试使用。
def _make_provider_config() -> Any:
    from seacode.config import ProviderConfig

    return ProviderConfig(
        name="test",
        protocol="anthropic",
        model="default-model",
        base_url="https://api.example.test",
        api_key="test-key",
    )


# 验证 AgentTool 类属性与默认状态。
# 直接断言 name / category / is_system_tool / should_defer 字段值。
def test_agent_tool_class_attributes() -> None:
    assert AgentTool.name == "Agent"
    assert AgentTool.category == ToolCategory.READ
    assert AgentTool.is_system_tool is False
    assert AgentTool.should_defer is False


# 验证 AgentToolParams 默认值。
# 构造空 AgentToolParams，断言 subagent_type 为空串且 run_in_background 为 False。
def test_agent_tool_params_defaults() -> None:
    params = AgentToolParams()
    assert params.subagent_type == ""
    assert params.prompt == ""
    assert params.description == ""
    assert params.run_in_background is False
    assert params.model is None
    assert params.team_name is None


# 验证 AgentTool 构造时 model_aliases 默认含 haiku/sonnet/opus 映射。
# 构造 AgentTool，断言 model_aliases 含三个别名。
def test_agent_tool_default_model_aliases() -> None:
    tool, _ = _make_tool()
    assert tool.model_aliases["haiku"] == "claude-haiku-4-5"
    assert tool.model_aliases["sonnet"] == "claude-sonnet-4-5"
    assert tool.model_aliases["opus"] == "claude-opus-4-1"


# 验证 AgentTool 构造时 query_source 默认 None。
# 构造 AgentTool，断言 query_source 为 None。
def test_agent_tool_default_query_source_none() -> None:
    tool, _ = _make_tool()
    assert tool.query_source is None


# 验证 AgentTool 接受自定义 model_aliases 覆盖默认。
# 构造时传 model_aliases={"haiku": "custom"}，断言覆盖生效。
def test_agent_tool_custom_model_aliases_override() -> None:
    loader = _FakeLoader()
    parent = _FakeParent()
    tool = AgentTool(
        agent_loader=loader,
        task_manager=_FakeTaskManager(),
        trace_manager=_FakeTraceManager(),
        parent_agent=parent,
        model_aliases={"haiku": "custom-haiku"},
    )
    assert tool.model_aliases["haiku"] == "custom-haiku"
    # 默认映射仍保留其它别名。
    assert tool.model_aliases["sonnet"] == "claude-sonnet-4-5"
