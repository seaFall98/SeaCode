"""SeaCode 第 05 步的紧凑 Textual 对话界面，支持 Agent Loop、工具调用与权限确认。"""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .agent import (
    Agent,
    ErrorEvent,
    LoopComplete,
    PermissionRequest,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from .client import (
    AuthenticationError,
    LLMClient,
    LLMError,
    NetworkError,
    RateLimitError,
    create_client,
)
from .config import ProviderConfig, SandboxAppConfig
from .conversation import ConversationManager
from .permission_dialog import InlinePermissionWidget
from .permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from .sandbox import SandboxConfig, create_sandbox
from .tools import create_default_registry
from .tools.base import ToolResult
from .tools.bash import Bash

# 工具调用详情展示的最大行数，超过则截断并提示剩余行数。
MAX_TRUNCATED_LINES: int = 20

# 可折叠的工具集合：只读工具在多工具回合时折叠为摘要。
COLLAPSIBLE_TOOLS: frozenset[str] = frozenset({"ReadFile", "Glob", "Grep"})

# thinking-done 行使用的动词列表，循环时随机选取一个。
THINKING_VERBS: list[str] = [
    "Accomplishing", "Architecting", "Baking", "Brewing",
    "Calculating", "Cascading", "Cerebrating", "Choreographing",
    "Churning", "Coalescing", "Cogitating", "Composing",
    "Computing", "Concocting", "Considering", "Contemplating",
    "Cooking", "Crafting", "Creating", "Crunching", "Crystallizing",
    "Cultivating", "Deciphering", "Deliberating", "Doodling",
    "Elucidating", "Enchanting", "Envisioning", "Fermenting",
    "Forging", "Generating", "Germinating", "Harmonizing",
    "Hatching", "Ideating", "Imagining", "Improvising", "Incubating",
    "Inferring", "Infusing", "Manifesting", "Marinating",
    "Meandering", "Mulling", "Musing", "Noodling", "Orbiting",
    "Orchestrating", "Percolating", "Pondering", "Pontificating",
    "Puzzling", "Ruminating", "Simmering", "Sketching", "Spinning",
    "Synthesizing", "Thinking", "Tinkering", "Transmuting",
    "Unfurling", "Unravelling", "Wandering", "Whisking", "Working",
    "Wrangling",
]

# Shift+Tab 循环切换的权限模式顺序：default → acceptEdits → plan → YOLO。
_MODE_CYCLE: list[PermissionMode] = [
    PermissionMode.DEFAULT,
    PermissionMode.ACCEPT_EDITS,
    PermissionMode.PLAN,
    PermissionMode.BYPASS,
]

# 各权限模式在状态栏的显示名；BYPASS 显示为 YOLO 以突出风险。
_MODE_DISPLAY: dict[PermissionMode, str] = {
    PermissionMode.DEFAULT: "default",
    PermissionMode.ACCEPT_EDITS: "accept-edits",
    PermissionMode.PLAN: "plan",
    PermissionMode.BYPASS: "YOLO",
}

# 各权限模式在状态栏的颜色；YOLO 用红色警示风险。
_MODE_COLORS: dict[PermissionMode, str] = {
    PermissionMode.DEFAULT: "#aebbc0",
    PermissionMode.ACCEPT_EDITS: "#a3be8c",
    PermissionMode.PLAN: "#d9a441",
    PermissionMode.BYPASS: "#ff9c9c",
}


# 把现在进行时动词转换为过去式，用于 thinking-done 行。
def _to_past_tense(verb: str) -> str:
    if verb.endswith("ing"):
        stem = verb[:-3]
        if stem.endswith("e"):
            return stem + "d"
        return stem + "ed"
    return verb + "ed"


def _tool_title(tool_name: str, arguments: dict[str, Any]) -> str:
    """根据工具名与参数生成简短的展示标题。"""
    if tool_name == "ReadFile":
        path = os.path.basename(arguments.get("file_path", ""))
        return f"Read {path}" if path else "Read"
    if tool_name == "WriteFile":
        path = os.path.basename(arguments.get("file_path", ""))
        content = arguments.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"Write {path} ({lines} lines)" if path else "Write"
    if tool_name == "EditFile":
        path = os.path.basename(arguments.get("file_path", ""))
        return f"Edit {path}" if path else "Edit"
    if tool_name == "Bash":
        cmd = arguments.get("command", "")
        short = cmd[:50] + "…" if len(cmd) > 50 else cmd
        return f"Bash: {short}" if short else "Bash"
    if tool_name == "Glob":
        return f"Glob: {arguments.get('pattern', '')}"
    if tool_name == "Grep":
        return f"Grep: {arguments.get('pattern', '')}"
    return tool_name


def _format_detail(tool_name: str, arguments: dict[str, Any], output: str) -> str:
    """按工具类型格式化展开态的详情文本，含截断与 diff 着色。"""
    parts: list[str] = []

    if tool_name == "Bash":
        parts.append(f"  IN   {arguments.get('command', '')}")
        parts.append("")
        for line in output.splitlines():
            parts.append(f"  OUT  {line}")
    elif tool_name == "EditFile":
        # EditFile 输出是 build_diff 生成的带行号 diff：+ 行绿色、- 行红色、其它 dim。
        # 转义 Rich markup 特殊字符，避免代码里的方括号被当成标签解析。
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            escaped = escape(line)
            if line.startswith("+ "):
                parts.append(f"  [green]{escaped}[/]")
            elif line.startswith("- "):
                parts.append(f"  [red]{escaped}[/]")
            else:
                parts.append(f"  [dim]{escaped}[/]")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  [dim]… ({total - MAX_TRUNCATED_LINES} more lines)[/]")
    elif tool_name in ("ReadFile", "WriteFile"):
        parts.append(f"  {arguments.get('file_path', '')}")
        parts.append("")
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  {line}")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  … ({total - MAX_TRUNCATED_LINES} more lines)")
    else:
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  {line}")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  … ({total - MAX_TRUNCATED_LINES} more lines)")

    return "\n".join(parts)


class ToolCallBlock(Static, can_focus=True):
    """展示单次工具调用 loading/成功/失败状态及可展开详情的块。"""

    def __init__(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tool_name = tool_name
        self._arguments = arguments
        self._title = _tool_title(tool_name, arguments)
        self._full_output = ""
        self._is_error = False
        self._elapsed = 0.0
        self._collapsed = True
        self._loading = True
        self._render_loading()

    # loading 态显示品牌色圆点与标题。
    def _render_loading(self) -> None:
        self.update(f"  ● {self._title} …")
        self.add_class("tool-block-loading")

    # 接收工具结果后切换到成功或失败态，EditFile 成功默认展开。
    def set_result(self, result: ToolResult, elapsed: float) -> None:
        self._full_output = result.content
        self._is_error = result.is_error
        self._elapsed = elapsed
        self._loading = False
        self.remove_class("tool-block-loading")
        if self._is_error:
            self.add_class("tool-block-error")
        # EditFile 的 diff 是最高频需要的信息，成功时默认展开；其它默认折叠避免刷屏。
        if self.tool_name == "EditFile" and not self._is_error:
            self._collapsed = False
            self._render_expanded()
        else:
            self._collapsed = True
            self._render_collapsed()

    # 折叠态只显示状态符号、标题与耗时。
    def _render_collapsed(self) -> None:
        if self._is_error:
            self.update(f"  ✗ {self._title} ({self._elapsed:.1f}s)")
        else:
            self.update(f"  ✓ {self._title} ({self._elapsed:.1f}s)")

    # 展开态在标题下附加格式化详情。
    def _render_expanded(self) -> None:
        if self._is_error:
            header = f"  ✗ {self._title} ({self._elapsed:.1f}s)"
        else:
            header = f"  ✓ {self._title} ({self._elapsed:.1f}s)"
        detail = _format_detail(self.tool_name, self._arguments, self._full_output)
        self.update(f"{header}\n{detail}")

    # 点击切换展开/折叠，loading 态不响应。
    def on_click(self) -> None:
        if self._loading:
            return
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._render_collapsed()
        else:
            self._render_expanded()


class ToolGroupSummary(Static, can_focus=True):
    """多工具调用分组的折叠摘要，显示工具数量与总耗时。"""

    def __init__(self, count: int, total_elapsed: float, **kwargs: Any) -> None:
        label = f"● Done ({count} tool uses · {total_elapsed:.1f}s)  (ctrl+o to expand)"
        super().__init__(label, **kwargs)
        self._count = count
        self._total = total_elapsed
        self._expanded = False

    # 切换展开/折叠态显示。
    def _refresh_display(self) -> None:
        if self._expanded:
            self.update(f"▼ Done ({self._count} tool uses · {self._total:.1f}s)")
        else:
            self.update(
                f"● Done ({self._count} tool uses · {self._total:.1f}s)"
                "  (ctrl+o to expand)"
            )

    # 切换展开/折叠状态。
    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh_display()

    # 返回当前是否处于展开态，供 ctrl+o 同步工具块显示。
    @property
    def is_expanded(self) -> bool:
        return self._expanded

    # 点击切换展开/折叠。
    def on_click(self) -> None:
        self.toggle()


class ChatInput(TextArea):
    """提供 Enter 发送与 Shift+Enter 换行的对话输入框。"""

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True),
        Binding("shift+enter", "newline", "New line", priority=True),
    ]

    class Submitted(TextualMessage):
        """携带已确认发送的非空用户文本。"""

        # 保存本次提交的纯文本内容。
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    # 发送非空输入，避免主要操作依赖鼠标按钮。
    def action_submit(self) -> None:
        if self.disabled:
            return
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(text))
            self.clear()

    # 在多行提示中保留显式换行行为。
    def action_newline(self) -> None:
        self.insert("\n")


class SeaCodeApp(App[None]):
    """管理 Provider 选择、单活动回合和可恢复流式呈现。"""

    CSS_PATH = "styles.tcss"
    TITLE = "SeaCode"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+o", "toggle_tool_blocks", "Toggle tools", priority=True),
        Binding("shift+tab", "cycle_mode", "Cycle mode", priority=True),
    ]

    # 初始化当前配置、客户端、工具注册中心和单回合状态。
    def __init__(
        self,
        providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
        *,
        client_factory: Callable[[ProviderConfig], LLMClient] = create_client,
        max_steps: int = 100,
        sandbox_cfg: SandboxAppConfig | None = None,
    ) -> None:
        super().__init__()
        self._providers = tuple(providers)
        self._client_factory = client_factory
        self._client: LLMClient | None = None
        self._conversation = ConversationManager()
        self._selected_provider: ProviderConfig | None = None
        self._tool_registry = create_default_registry()
        self._streaming = False
        self._agent_task: asyncio.Task[None] | None = None
        self._max_steps = max_steps
        # OS 级沙箱配置；从 .seacode/config.yaml 的 sandbox 段加载，默认全关闭。
        self._sandbox_cfg = sandbox_cfg or SandboxAppConfig()
        # 权限检查器；在 _select_provider 中装配，为 None 时跳过权限检查。
        self._permission_checker: PermissionChecker | None = None
        # 当前权限模式；与 permission_checker.mode 同步，供 TUI 状态栏展示。
        self._permission_mode: PermissionMode | None = None
        # 待回复的权限请求；HITL 弹窗期间持有，回复后清空。
        self._pending_permission: PermissionRequest | None = None

    # 生成三行品牌标题，保留终端对话的既定信息层级。
    @staticmethod
    def _make_banner(work_dir: str = "") -> Text:
        banner = Text()
        banner.append(" /\\___/\\   ", style="bold #d9a441")
        banner.append("SeaCode\n", style="#c7d2d5")
        banner.append("( =o.o= )  ", style="bold #d9a441")
        banner.append(f"{work_dir}\n" if work_dir else "\n", style="#9fb2b6")
        banner.append(" /| ||| |\\ ", style="bold #d9a441")
        return banner

    # 构造标题、选择、聊天、输入和横向状态栏五个既定区域。
    def compose(self) -> ComposeResult:
        yield Static(self._make_banner(), id="title-bar")
        if len(self._providers) > 1:
            with Vertical(id="provider-select"):
                yield Static("Select a model profile", id="select-label")
                yield OptionList(
                    *[
                        Option(f"{provider.name}  [{provider.model}]", id=provider.name)
                        for provider in self._providers
                    ],
                    id="provider-list",
                )
        yield VerticalScroll(id="chat-area")
        with Vertical(id="input-area"):
            yield ChatInput(id="chat-input")
            with Horizontal(id="status-bar"):
                yield Static("Preparing configuration", id="turn-status")
                yield Static("", id="model-label")
                yield Static("", id="mode-label")

    # 根据 Provider 数量进入选择状态或直接准备单一配置。
    def on_mount(self) -> None:
        self.query_one(ChatInput).disabled = True
        if len(self._providers) == 1:
            self._select_provider(self._providers[0])
        else:
            self.query_one("#chat-area").display = False
            self.query_one("#input-area").display = False

    # 接收键盘选择并切换到相应模型配置。
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        selected_name = str(event.option.id)
        provider = next(
            (candidate for candidate in self._providers if candidate.name == selected_name),
            None,
        )
        if provider is not None:
            self._select_provider(provider)

    # 建立客户端并让对话界面进入可发送状态。
    def _select_provider(self, provider: ProviderConfig) -> None:
        try:
            self._client = self._client_factory(provider)
        except LLMError as error:
            self._show_startup_error(error)
            return

        self._selected_provider = provider
        self._assemble_permission_system()
        self.query_one("#chat-area").display = True
        self.query_one("#input-area").display = True
        if len(self._providers) > 1:
            self.query_one("#provider-select").display = False
        self.query_one("#title-bar", Static).update(self._make_banner(os.getcwd()))
        self.query_one("#model-label", Static).update(Text(provider.model))
        self._set_status("Ready")
        self._update_mode_label()
        input_widget = self.query_one(ChatInput)
        input_widget.disabled = False
        input_widget.focus()

    # 装配权限检查器与 OS 级沙箱：三层规则文件 + 危险命令检测 + 路径沙箱 + 模式 + 沙箱挂载。
    def _assemble_permission_system(self) -> None:
        work_dir = os.getcwd()
        home = Path.home()

        # 三层规则文件路径：用户级 > 项目级 > 本地级（可写入）。
        rule_engine = RuleEngine(
            user_rules_path=home / ".seacode" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".seacode" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".seacode" / "permissions.local.yaml",
        )

        # OS 级沙箱：配置启用时尝试创建，Windows 等不支持平台返回 None 优雅降级。
        sandbox_cfg = self._sandbox_cfg
        os_sandbox = create_sandbox() if sandbox_cfg.enabled else None
        checker_sandbox_enabled = False

        if sandbox_cfg.enabled and os_sandbox is not None and os_sandbox.available():
            # 挂载 OS 沙箱到 Bash 工具，实现应用层 + 内核层双重防护。
            bash_tool = self._tool_registry.get("Bash")
            if isinstance(bash_tool, Bash):
                bash_tool.sandbox = os_sandbox
                bash_tool.sandbox_config = SandboxConfig(
                    allow_write=[work_dir, tempfile.gettempdir()],
                    deny_write=[
                        os.path.join(work_dir, ".seacode", "config.yaml"),
                        os.path.join(work_dir, ".seacode", "permissions.local.yaml"),
                    ],
                    network_enabled=sandbox_cfg.network_enabled,
                )
            # 内核兜底存在时，auto_allow 才能触发 Layer 1c 自动放行。
            checker_sandbox_enabled = sandbox_cfg.auto_allow
        elif sandbox_cfg.enabled and os_sandbox is None:
            # 不支持沙箱的平台：提示用户已降级为应用层路径检查。
            self.call_after_refresh(
                self._show_system_message,
                "当前系统不支持 OS 沙箱，已降级为应用层路径检查",
            )

        # 装配五层防御链权限检查器。
        self._permission_checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(project_root=work_dir),
            rule_engine=rule_engine,
            mode=PermissionMode.DEFAULT,
            sandbox_enabled=checker_sandbox_enabled,
        )
        self._permission_mode = PermissionMode.DEFAULT

    # 在客户端创建失败时展示脱敏启动错误。
    def _show_startup_error(self, error: LLMError) -> None:
        self.query_one("#chat-area").display = True
        self.query_one("#input-area").display = True
        self._set_status("Configuration error")
        self.call_after_refresh(self._append_error, self._error_message(error))

    # 接收输入消息并把单个活动回合交给异步任务执行，支持 ESC 取消。
    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        if self._streaming or self._client is None:
            return
        self._streaming = True
        self._agent_task = asyncio.create_task(self._run_turn(event.text))

    # ESC 取消正在进行的回合；权限对话框活动时由对话框处理（拒绝），不取消整个回合。
    def action_cancel(self) -> None:
        if self._pending_permission is not None:
            return
        if self._streaming and self._agent_task is not None:
            self._agent_task.cancel()

    # ctrl+o 切换当前回合 ToolGroupSummary 的展开/折叠，并同步隐藏工具块的显示。
    def action_toggle_tool_blocks(self) -> None:
        for summary in self.query(ToolGroupSummary):
            summary.toggle()
            expanded = summary.is_expanded
            parent = summary.parent
            if parent is None:
                continue
            for block in parent.query(ToolCallBlock):
                if block.tool_name in COLLAPSIBLE_TOOLS:
                    block.display = expanded

    # Shift+Tab 循环切换权限模式：default → acceptEdits → plan → YOLO → default。
    def action_cycle_mode(self) -> None:
        if self._permission_mode is None:
            return
        current_idx = _MODE_CYCLE.index(self._permission_mode)
        next_mode = _MODE_CYCLE[(current_idx + 1) % len(_MODE_CYCLE)]
        self._permission_mode = next_mode
        if self._permission_checker is not None:
            self._permission_checker.mode = next_mode
        self._update_mode_label()

    # 更新状态栏右侧的模式标签，显示当前模式名与对应颜色。
    def _update_mode_label(self) -> None:
        if self._permission_mode is None:
            return
        display = _MODE_DISPLAY.get(self._permission_mode, self._permission_mode.value)
        color = _MODE_COLORS.get(self._permission_mode, "#aebbc0")
        self.query_one("#mode-label", Static).update(Text(f"[{display}]", style=color))

    # HITL 权限确认回复：resolve future 让 Agent 继续，移除对话框，输入框保持禁用直到回合结束。
    async def on_inline_permission_widget_responded(
        self, event: InlinePermissionWidget.Responded
    ) -> None:
        if self._pending_permission is None:
            return
        if not self._pending_permission.future.done():
            self._pending_permission.future.set_result(event.response)
        self._pending_permission = None
        try:
            widget = self.query_one("#perm-inline", InlinePermissionWidget)
            await widget.remove()
        except Exception:
            pass

    # 执行一条完整 Agent Loop 回合，消费 AgentEvent 流并管理 TUI 展示与取消。
    async def _run_turn(self, text: str) -> None:
        client = self._client
        provider = self._selected_provider
        if client is None or provider is None:
            return

        input_widget = self.query_one(ChatInput)
        input_widget.disabled = True

        # 记录回合起点，失败时回滚到此长度，避免不完整回合污染历史。
        turn_start_len = len(self._conversation.messages)
        self._conversation.add_user_message(text)

        user_message = Text()
        user_message.append("❯ ", style="bold #71b8bc")
        user_message.append(text, style="bold #f2f5f5")
        await self._append_static(user_message, "message user-message")

        chat = self.query_one("#chat-area", VerticalScroll)
        ai_row = Vertical(classes="ai-row")
        await chat.mount(ai_row)
        live_answer = Static(Text(""), classes="message assistant-message")
        await ai_row.mount(live_answer)
        started = time.monotonic()
        thinking_verb = random.choice(THINKING_VERBS)
        answer = ""
        thinking_widget: Static | None = None
        thinking = ""
        tool_blocks: dict[str, ToolCallBlock] = {}
        total_input = 0
        total_output = 0

        try:
            agent = Agent(
                client=client,
                registry=self._tool_registry,
                protocol=provider.protocol,
                work_dir=os.getcwd(),
                max_iterations=self._max_steps,
                permission_checker=self._permission_checker,
            )
            async for event in agent.run(self._conversation):
                if isinstance(event, StreamText):
                    answer += event.text
                    live_text = Text()
                    live_text.append("● ", style="bold #d9a441")
                    live_text.append(answer)
                    live_answer.update(live_text)
                    chat.scroll_end(animate=False)
                elif isinstance(event, ThinkingText):
                    thinking += event.text
                    if thinking_widget is None:
                        thinking_widget = Static(
                            Text("Thinking"), classes="message thinking-message"
                        )
                        await chat.mount(thinking_widget)
                    thinking_widget.update(Text(f"Thinking\n{thinking}"))
                    chat.scroll_end(animate=False)
                elif isinstance(event, ToolUseEvent):
                    block = ToolCallBlock(event.tool_name, event.arguments)
                    tool_blocks[event.tool_id] = block
                    await ai_row.mount(block)
                    chat.scroll_end(animate=False)
                elif isinstance(event, ToolResultEvent):
                    result_block = tool_blocks.get(event.tool_id)
                    if result_block is not None:
                        result_block.set_result(
                            ToolResult(content=event.output, is_error=event.is_error),
                            event.elapsed,
                        )
                    chat.scroll_end(animate=False)
                elif isinstance(event, RetryEvent):
                    await self._show_system_message(f"↻ Retrying: {event.reason}")
                elif isinstance(event, UsageEvent):
                    total_input = event.input_tokens
                    total_output = event.output_tokens
                elif isinstance(event, PermissionRequest):
                    # HITL 权限确认：挂载内联对话框并禁用输入框，等待用户回复。
                    self._pending_permission = event
                    widget = InlinePermissionWidget(event.tool_name, event.description)
                    await chat.mount(widget)
                    chat.scroll_end(animate=False)
                    input_widget.disabled = True
                elif isinstance(event, ErrorEvent):
                    await self._append_error(event.message)
                elif isinstance(event, TurnComplete):
                    # 可折叠工具 >=2 个时 mount 摘要并隐藏工具块。
                    collapsible = [
                        (tid, blk) for tid, blk in tool_blocks.items()
                        if blk.tool_name in COLLAPSIBLE_TOOLS and not blk._loading
                    ]
                    if len(collapsible) >= 2:
                        total_elapsed = sum(blk._elapsed for _, blk in collapsible)
                        summary = ToolGroupSummary(len(collapsible), total_elapsed)
                        for _, blk in collapsible:
                            blk.display = False
                        await ai_row.mount(summary)
                    # 重置工具块字典，开新 ai_row 供下一轮工具调用。
                    tool_blocks.clear()
                    ai_row = Vertical(classes="ai-row")
                    await chat.mount(ai_row)
                    live_answer = Static(Text(""), classes="message assistant-message")
                    await ai_row.mount(live_answer)
                    answer = ""
                    chat.scroll_end(animate=False)
                elif isinstance(event, LoopComplete):
                    total_time = time.monotonic() - started
                    done_label = Static(
                        f"✻ {_to_past_tense(thinking_verb)} for {total_time:.1f}s",
                        classes="message thinking-done",
                    )
                    await ai_row.mount(done_label)
                    chat.scroll_end(animate=False)

            # 收尾：渲染剩余的累积文本。
            await live_answer.remove()
            final_answer = answer or "*(The provider completed without text.)*"
            await ai_row.mount(
                Markdown(final_answer, classes="message assistant-markdown")
            )
            elapsed = time.monotonic() - started
            self._set_status(
                f"Ready  {elapsed:.1f}s  in {total_input} / out {total_output}"
            )
        except asyncio.CancelledError:
            # 保留已累积的流式文本并追加 [cancelled] 标记。
            await live_answer.remove()
            if answer:
                await ai_row.mount(
                    Markdown(
                        answer + "\n\n*[cancelled]*",
                        classes="message assistant-markdown",
                    )
                )
            await self._show_system_message("Operation cancelled")
            self._set_status("Ready")
            raise
        except LLMError as error:
            self._rollback_turn(turn_start_len)
            await self._append_error(self._error_message(error))
            self._set_status("Ready")
        except Exception:
            self._rollback_turn(turn_start_len)
            await self._append_error(
                "The request could not be completed. Check the model configuration."
            )
            self._set_status("Ready")
        finally:
            self._streaming = False
            self._agent_task = None
            input_widget.disabled = self._client is None
            if not input_widget.disabled:
                input_widget.focus()

    # 回滚本回合新增的所有消息，避免不完整历史污染后续请求。
    def _rollback_turn(self, turn_start_len: int) -> None:
        while len(self._conversation.messages) > turn_start_len:
            self._conversation.drop_last()

    # 在对话区追加一条安全的静态文本消息。
    async def _append_static(self, content: Text, css_class: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        await chat.mount(Static(content, classes=css_class))
        chat.scroll_end(animate=False)

    # 在对话区追加不包含原始异常内容的错误消息。
    async def _append_error(self, message: str) -> None:
        await self._append_static(Text(f"✖ {message}"), "message error-message")

    # 在对话区追加一条系统提示消息（取消、重试等），用 dim 样式与正文区分。
    async def _show_system_message(self, text: str) -> None:
        await self._append_static(Text(text), "message system-message")

    # 将有限错误类别映射成可行动但不泄露细节的文本。
    def _error_message(self, error: LLMError) -> str:
        if isinstance(error, AuthenticationError):
            return "Authentication failed. Check the selected local model configuration."
        if isinstance(error, RateLimitError):
            return "The provider is rate limiting this request. Try again shortly."
        if isinstance(error, NetworkError):
            return "The provider could not be reached. Check the endpoint and network."
        return "The provider returned an unusable response. You can send another message."

    # 在唯一状态栏位置更新当前回合状态、耗时和用量。
    def _set_status(self, text: str) -> None:
        self.query_one("#turn-status", Static).update(Text(text))
