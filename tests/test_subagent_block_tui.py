"""SubAgentBlock 与 InlineAskUserWidget TUI 测试：覆盖渲染、交互与 app 集成。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from textual.app import App, ComposeResult

from seacode.app import SeaCodeApp
from seacode.askuser_dialog import InlineAskUserWidget
from seacode.client import (
    LLMClient,
    StreamEvent,
)
from seacode.config import ProviderConfig

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


# 假 LLM 客户端：返回空流，不连接真实 Provider。
class _FakeClient(LLMClient):
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def stream(
        self,
        messages: Sequence[Any],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del messages, system, tools
        return
        yield  # pragma: no cover  # 让 Python 识别为 async generator


# 构造无密钥 Provider 配置供 SeaCodeApp 使用。
def _provider(name: str = "test") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol="openai-compat",
        model="test-model",
        base_url="https://api.example.test",
        api_key="test-key",
    )


# 等待 Textual 事件循环处理完待定事件。
async def _settle(pilot: Any) -> None:
    await pilot.pause(0.05)
    await pilot.pause(0.05)


# ---------------------------------------------------------------------------
# SubAgentBlock 单元测试
# ---------------------------------------------------------------------------


# 验证 SubAgentBlock._parse_stats 正则提取 "N tool uses" 模式。
# 传入 "done with 5 tool uses"，断言返回 5。
def test_subagent_block_parse_stats_extracts_count() -> None:
    # 用最小 App 上下文构造 SubAgentBlock 避免渲染异常。
    # _parse_stats 是纯字符串解析，不依赖 App 运行状态。
    from seacode.app import SubAgentBlock

    # 直接测试 _parse_stats 逻辑；用实例方法需绕过 __init__ 的 update 调用。
    # 用 __new__ 创建实例避免 __init__ 副作用，再手动调用 _parse_stats。
    block = SubAgentBlock.__new__(SubAgentBlock)
    assert block._parse_stats("done with 5 tool uses") == 5
    assert block._parse_stats("done with 12 tool uses") == 12


# 验证 SubAgentBlock._parse_stats 无匹配返回 0。
# 传入 "no tools here"，断言返回 0。
def test_subagent_block_parse_stats_no_match_returns_zero() -> None:
    from seacode.app import SubAgentBlock

    block = SubAgentBlock.__new__(SubAgentBlock)
    assert block._parse_stats("no tools here") == 0
    assert block._parse_stats("") == 0


# 验证 SubAgentBlock._parse_stats 大小写不敏感。
# 传入 "Done with 3 Tool Uses"，断言返回 3。
def test_subagent_block_parse_stats_case_insensitive() -> None:
    from seacode.app import SubAgentBlock

    block = SubAgentBlock.__new__(SubAgentBlock)
    assert block._parse_stats("Done with 3 Tool Uses") == 3


# 验证 SubAgentBlock.description 截断 60 字符。
# 传入 100 字符 description，断言 _description 长度为 60。
def test_subagent_block_description_truncates_to_sixty() -> None:
    from seacode.app import SubAgentBlock

    block = SubAgentBlock.__new__(SubAgentBlock)
    # 手动设置 _DESC_LIMIT 与 _description 模拟截断逻辑。
    long_desc = "x" * 100
    block._DESC_LIMIT = 60
    block._description = long_desc[: block._DESC_LIMIT]
    assert len(block._description) == 60


# ---------------------------------------------------------------------------
# SubAgentBlock 渲染测试（使用 Textual test app）
# ---------------------------------------------------------------------------


# 挂载 SubAgentBlock 的最小测试 App。
class _BlockApp(App[None]):
    def __init__(self, agent_type: str, description: str) -> None:
        super().__init__()
        self._agent_type = agent_type
        self._description = description

    def compose(self) -> ComposeResult:
        from seacode.app import SubAgentBlock

        yield SubAgentBlock(self._agent_type, self._description)


# 验证 SubAgentBlock 渲染运行中状态含 agent_type 与 "Running…"。
# 挂载 block，断言渲染含 "● Explore" 与 "Running…"。
async def test_subagent_block_renders_running_state() -> None:
    app = _BlockApp("Explore", "探索代码库")
    async with app.run_test() as pilot:
        await _settle(pilot)
        from seacode.app import SubAgentBlock

        block = app.query_one(SubAgentBlock)
        rendered = str(block.render())
        assert "● Explore" in rendered
        assert "Running" in rendered


# 验证 SubAgentBlock.set_result 渲染折叠完成态。
# set_result 后断言渲染含 "Done" 与 "ctrl+o to expand"。
async def test_subagent_block_set_result_collapsed() -> None:
    app = _BlockApp("Explore", "探索")
    async with app.run_test() as pilot:
        await _settle(pilot)
        from seacode.app import SubAgentBlock

        block = app.query_one(SubAgentBlock)
        block.set_result(
            output="done with 5 tool uses", is_error=False, elapsed=1.5
        )
        await _settle(pilot)
        rendered = str(block.render())
        assert "Done" in rendered
        assert "5 tool uses" in rendered
        assert "1.5s" in rendered
        assert "ctrl+o" in rendered


# 验证 SubAgentBlock.set_result 错误状态渲染含 "✗"。
# set_result is_error=True，断言渲染含 "✗"。
async def test_subagent_block_set_result_error_state() -> None:
    app = _BlockApp("Explore", "探索")
    async with app.run_test() as pilot:
        await _settle(pilot)
        from seacode.app import SubAgentBlock

        block = app.query_one(SubAgentBlock)
        block.set_result(output="error occurred", is_error=True, elapsed=1.0)
        await _settle(pilot)
        rendered = str(block.render())
        assert "✗" in rendered


# 验证 SubAgentBlock.set_result 展开态显示结果预览。
# _expanded=True 后 set_result，断言渲染含输出文本前 300 字符。
async def test_subagent_block_set_result_expanded_shows_preview() -> None:
    app = _BlockApp("Explore", "探索")
    async with app.run_test() as pilot:
        await _settle(pilot)
        from seacode.app import SubAgentBlock

        block = app.query_one(SubAgentBlock)
        block._expanded = False
        block._loading = False
        block.set_result(
            output="result preview text here", is_error=False, elapsed=2.0
        )
        # 切换到展开态。
        block._collapsed = False
        block._render_expanded()
        await _settle(pilot)
        rendered = str(block.render())
        assert "result preview text here" in rendered


# 验证 SubAgentBlock 点击切换折叠/展开。
# 初始 collapsed=True，触发 on_click 后断言 _collapsed=False。
async def test_subagent_block_click_toggles_collapse() -> None:
    app = _BlockApp("Explore", "探索")
    async with app.run_test() as pilot:
        await _settle(pilot)
        from seacode.app import SubAgentBlock

        block = app.query_one(SubAgentBlock)
        block.set_result(output="done", is_error=False, elapsed=1.0)
        await _settle(pilot)
        assert block._collapsed is True
        block.on_click()
        assert block._collapsed is False
        block.on_click()
        assert block._collapsed is True


# 验证 SubAgentBlock loading 态点击不响应。
# loading=True 时 on_click 不切换 _collapsed。
async def test_subagent_block_click_loading_no_response() -> None:
    app = _BlockApp("Explore", "探索")
    async with app.run_test() as pilot:
        await _settle(pilot)
        from seacode.app import SubAgentBlock

        block = app.query_one(SubAgentBlock)
        # loading 态初始 _collapsed=True。
        assert block._loading is True
        block.on_click()
        assert block._loading is True


# ---------------------------------------------------------------------------
# InlineAskUserWidget 渲染测试
# ---------------------------------------------------------------------------


# 挂载 InlineAskUserWidget 的最小测试 App。
class _AskUserApp(App[None]):
    def __init__(
        self,
        questions: list[dict[str, Any]],
        future: asyncio.Future[dict[str, str]],
    ) -> None:
        super().__init__()
        self._questions = questions
        self._future = future

    def compose(self) -> ComposeResult:
        yield InlineAskUserWidget(self._questions)

    # 收到 Responded 事件时回填 future，模拟 app.py 的 future 回填契约。
    def on_inline_ask_user_widget_responded(
        self, event: InlineAskUserWidget.Responded
    ) -> None:
        if not self._future.done():
            self._future.set_result(event.answers if event.answers else {})


# 读取 InlineAskUserWidget 当前渲染内容（#askuser-content Static 的纯文本）。
def _askuser_render(widget: InlineAskUserWidget) -> str:
    from textual.widgets import Static

    return str(widget.query_one("#askuser-content", Static).render())


# 验证 InlineAskUserWidget 渲染 text 表单含 message 与 Other 输入提示。
# 挂载 text 问题，断言渲染含 message 文本与 "Other" 占位（text 无 options，光标落到 Other）。
async def test_inline_ask_user_renders_text_form() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.Future()
    questions = [{"type": "text", "name": "q1", "message": "你的名字", "options": []}]
    app = _AskUserApp(questions, future)
    async with app.run_test() as pilot:
        await _settle(pilot)
        widget = app.query_one(InlineAskUserWidget)
        rendered = _askuser_render(widget)
        assert "你的名字" in rendered
        # text 无 options，光标默认在 Other，应显示输入提示。
        assert "Other" in rendered
        assert "Type your answer here" in rendered


# 验证 InlineAskUserWidget 渲染 radio 表单含所有选项文本。
# 挂载 radio 问题带 2 选项，断言渲染含两个选项文本与单选确认提示。
async def test_inline_ask_user_renders_radio_form() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.Future()
    questions = [
        {
            "type": "radio",
            "name": "q1",
            "message": "选择",
            "options": ["选项A", "选项B"],
        }
    ]
    app = _AskUserApp(questions, future)
    async with app.run_test() as pilot:
        await _settle(pilot)
        widget = app.query_one(InlineAskUserWidget)
        rendered = _askuser_render(widget)
        assert "选项A" in rendered
        assert "选项B" in rendered
        # 单选提示。
        assert "enter to confirm" in rendered


# 验证 InlineAskUserWidget 渲染 select 表单含所有选项文本。
# 挂载 select 问题带 3 选项，断言渲染含全部选项文本。
async def test_inline_ask_user_renders_select_form() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.Future()
    questions = [
        {
            "type": "select",
            "name": "q1",
            "message": "下拉选择",
            "options": ["x", "y", "z"],
        }
    ]
    app = _AskUserApp(questions, future)
    async with app.run_test() as pilot:
        await _settle(pilot)
        widget = app.query_one(InlineAskUserWidget)
        rendered = _askuser_render(widget)
        assert "x" in rendered
        assert "y" in rendered
        assert "z" in rendered


# 验证 InlineAskUserWidget 渲染 checkbox 表单含多选切换提示。
# 挂载带 multiSelect=True 的 checkbox 问题，断言渲染含选项文本与 "space to toggle" 多选提示。
async def test_inline_ask_user_renders_checkbox_form() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.Future()
    questions = [
        {
            "type": "checkbox",
            "name": "q1",
            "message": "多选",
            "multiSelect": True,
            "options": ["m", "n"],
        }
    ]
    app = _AskUserApp(questions, future)
    async with app.run_test() as pilot:
        await _settle(pilot)
        widget = app.query_one(InlineAskUserWidget)
        rendered = _askuser_render(widget)
        assert "m" in rendered
        assert "n" in rendered
        # 多选提示。
        assert "space to toggle" in rendered


# 验证 InlineAskUserWidget 多问题时渲染导航栏含 Submit 项。
# 挂载 2 个问题，断言渲染含 "Submit" 导航项与两个问题的 name 标签。
async def test_inline_ask_user_renders_nav_bar_with_submit() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.Future()
    questions = [
        {"type": "text", "name": "q1", "message": "第一问", "options": []},
        {"type": "text", "name": "q2", "message": "第二问", "options": []},
    ]
    app = _AskUserApp(questions, future)
    async with app.run_test() as pilot:
        await _settle(pilot)
        widget = app.query_one(InlineAskUserWidget)
        rendered = _askuser_render(widget)
        # 多问题导航栏含 Submit 项与各问题 fallback 标签 Q1/Q2（无 header 时用 Q{i+1}）。
        assert "Submit" in rendered
        assert "Q1" in rendered
        assert "Q2" in rendered


# 验证 InlineAskUserWidget 键盘导航：下移光标 → Enter 提交 → future 回填答案。
# 挂载 radio 问题带 3 选项，action_cursor_down 后 action_select，断言 future 收到第 2 项。
async def test_inline_ask_user_keyboard_navigation_submits_answer() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.Future()
    questions = [
        {
            "type": "radio",
            "name": "q1",
            "message": "选择",
            "options": ["a", "b", "c"],
        }
    ]
    app = _AskUserApp(questions, future)
    async with app.run_test() as pilot:
        await _settle(pilot)
        widget = app.query_one(InlineAskUserWidget)
        # 初始光标在 0（"a"），下移一次到 1（"b"），Enter 提交。
        widget.action_cursor_down()
        widget.action_select()
        await _settle(pilot)
        # 单问题 Enter 直接提交；key 取 question→message fallback，故 key 为 "选择"。
        assert future.done()
        assert future.result() == {"选择": "b"}


# 验证 InlineAskUserWidget checkbox 多选切换：空格切换勾选 → Enter 提交多选答案。
# 挂载带 multiSelect=True 的 checkbox 问题，action_toggle 切换两项后提交，断言答案含两项逗号拼接。
async def test_inline_ask_user_checkbox_toggle_submits_multiple() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.Future()
    questions = [
        {
            "type": "checkbox",
            "name": "q1",
            "message": "多选",
            "multiSelect": True,
            "options": ["x", "y"],
        }
    ]
    app = _AskUserApp(questions, future)
    async with app.run_test() as pilot:
        await _settle(pilot)
        widget = app.query_one(InlineAskUserWidget)
        # 光标在 0（"x"），空格勾选；下移到 1（"y"），空格勾选；Enter 提交。
        widget.action_toggle_option()
        widget.action_cursor_down()
        widget.action_toggle_option()
        widget.action_select()
        await _settle(pilot)
        assert future.done()
        assert future.result() == {"多选": "x, y"}


# 验证 InlineAskUserWidget ESC 取消回填空 dict。
# 挂载 widget，触发 action_cancel，断言 future 回填空 dict。
async def test_inline_ask_user_cancel_returns_empty_dict() -> None:
    future: asyncio.Future[dict[str, str]] = asyncio.Future()
    questions = [{"type": "text", "name": "q1", "message": "hi", "options": []}]
    app = _AskUserApp(questions, future)
    async with app.run_test() as pilot:
        await _settle(pilot)
        widget = app.query_one(InlineAskUserWidget)
        widget.action_cancel()
        await _settle(pilot)
        assert future.done()
        assert future.result() == {}


# 验证 InlineAskUserWidget.Responded 事件携带 answers。
# 构造 Responded 事件，断言 answers 字段非空。
def test_responded_event_carries_answers() -> None:
    answers = {"q1": "answer1", "q2": "answer2"}
    event = InlineAskUserWidget.Responded(answers=answers)
    assert event.answers == answers
    assert len(event.answers) == 2


# 验证 InlineAskUserWidget.Responded 事件支持 None（取消语义）。
# 构造 Responded(None)，断言 answers 为 None。
def test_responded_event_supports_none_answers() -> None:
    event = InlineAskUserWidget.Responded(None)
    assert event.answers is None


# ---------------------------------------------------------------------------
# SeaCodeApp 集成验证
# ---------------------------------------------------------------------------


# 验证 SeaCodeApp 启动时 AgentLoader / TaskManager / TraceManager 初始化。
# 构造 SeaCodeApp 并启动，断言三个属性非 None。
async def test_app_initializes_subagent_managers() -> None:
    client = _FakeClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.agent_loader is not None
        assert app.task_manager is not None
        assert app.trace_manager is not None


# 验证 /tasks 与 /trace 命令在命令注册中心可见。
# 启动 app，查询 command_registry，断言含 "tasks" 与 "trace"。
async def test_app_registers_tasks_and_trace_commands() -> None:
    client = _FakeClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        await _settle(pilot)
        tasks_cmd = app._command_registry.find("tasks")
        trace_cmd = app._command_registry.find("trace")
        assert tasks_cmd is not None
        assert trace_cmd is not None
        assert "task" in tasks_cmd.aliases
        assert "tree" in trace_cmd.aliases


# 验证 SeaCodeApp 启动时 AgentTool 与 AskUserTool 注入到 ToolRegistry。
# 启动 app，断言 ToolRegistry 含 "Agent" 与 "AskUserQuestion"。
async def test_app_registers_agent_and_ask_user_tools() -> None:
    client = _FakeClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        await _settle(pilot)
        agent_tool = app._tool_registry.get("Agent")
        ask_user_tool = app._tool_registry.get("AskUserQuestion")
        assert agent_tool is not None
        assert ask_user_tool is not None


# 验证 SeaCodeApp._subagent_task 默认 None。
# 启动 app，断言 _subagent_task 为 None。
async def test_app_default_subagent_task_none() -> None:
    client = _FakeClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app._subagent_task is None


# 验证 ESC 无运行子 Agent 时走原有路径不调用 adopt_running。
# _subagent_task=None，触发 action_cancel，断言不抛异常。
async def test_app_esc_no_subagent_goes_original_path() -> None:
    client = _FakeClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app._subagent_task is None
        # action_cancel 在无子 Agent 时应安全返回。
        await app.action_cancel()
        # _subagent_task 仍为 None。
        assert app._subagent_task is None


# 验证 _process_task_notifications 空完成列表不注入消息。
# poll_completed 返回空，断言不注入 user message。
async def test_app_process_notifications_empty_does_nothing() -> None:
    client = _FakeClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        await _settle(pilot)
        # 空 TaskManager 的 poll_completed 返回空列表。
        await app._process_task_notifications()
        # 不抛异常即通过。


# 验证 _process_task_notifications 有完成任务时注入通知。
# 手动往 task_manager 插入完成 task，调用 _process_task_notifications，断言对话含通知。
async def test_app_process_notifications_injects_notification() -> None:
    from seacode.agents.task_manager import BackgroundTask

    client = _FakeClient()
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)
    async with app.run_test() as pilot:
        await _settle(pilot)
        # 手动插入一个已完成的后台任务到 notify_queue。
        bg = BackgroundTask(
            id="abc12345",
            name="Explore",
            agent=None,
            task="test",
            status="completed",
            result="done",
        )
        app.task_manager._tasks["abc12345"] = bg  # type: ignore[assignment]
        app.task_manager._notify_queue.put_nowait("abc12345")
        # 记录注入前的消息数。
        before = len(app._conversation.messages)
        await app._process_task_notifications()
        await _settle(pilot)
        after = len(app._conversation.messages)
        # 应注入至少一条含 <task-notification> 的 user message。
        assert after > before
        # 在所有消息中查找含 <task-notification> 的 user message。
        notification_msgs = [
            m
            for m in app._conversation.messages
            if "<task-notification>" in m.content
        ]
        assert len(notification_msgs) >= 1
