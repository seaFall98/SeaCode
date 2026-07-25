"""SeaCode 第 05 步的紧凑 Textual 对话界面，支持 Agent Loop、工具调用与权限确认。"""

from __future__ import annotations

import asyncio
import os
import random
import re
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
    CompactNotification,
    ErrorEvent,
    LoopComplete,
    MCPConnectEvent,
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
from .commands import (
    CommandContext,
    CommandRegistry,
    CompletionPopup,
    Selected,
    complete,
    parse_command,
)
from .commands.handlers import register_all_commands
from .config import MCPServerConfig, ProviderConfig, SandboxAppConfig
from .conversation import ConversationManager, Message
from .mcp import MCPManager
from .memory import (
    MemoryManager,
    Session,
    SessionManager,
    generate_session_summary,
    load_instructions,
    make_compact_boundary,
)
from .permission_dialog import InlinePermissionWidget
from .permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from .sandbox import SandboxConfig, create_sandbox
from .session_dialog import InlineResumeWidget
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

# batch09：@ 文件引用展开相关常量与函数。
# 单个 @path 文件内容块的最大字节，超过则截断，避免大文件撑爆上下文。
MAX_AT_REF_BYTES: int = 100_000

# @ 文件引用正则：匹配 @ 后跟非空白字符序列。
_AT_REF_RE = re.compile(r"@(\S+)")

# scan_files_for_at 跳过的目录名，避免扫描运行时产物与依赖目录。
_AT_SKIP_DIRS: frozenset[str] = frozenset({
    ".seacode", ".git", "__pycache__", ".venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
})


# 把文本中的 @path 引用替换为文件内容块，便于 LLM 直接读取文件内容。
# 文件不存在、读取异常或路径逃逸时保留原文，不崩溃。
def expand_at_refs(text: str, work_dir: Path) -> str:
    if "@" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        # 解析路径并限制在工作目录可达范围，避免 ../../etc/passwd 逃逸。
        target = (work_dir / raw).resolve()
        try:
            target.relative_to(work_dir.resolve())
        except (ValueError, OSError):
            return match.group(0)
        if not target.is_file():
            return match.group(0)
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return match.group(0)
        if len(content.encode("utf-8")) > MAX_AT_REF_BYTES:
            content = content.encode("utf-8")[:MAX_AT_REF_BYTES].decode(
                "utf-8", errors="replace"
            )
        return f"[File: {raw}]\n```\n{content}\n```"

    return _AT_REF_RE.sub(_replace, text)


# 扫描工作目录下文件名前缀匹配的候选，供 @ 文件引用补全使用。
# 跳过运行时产物与依赖目录；返回相对工作目录的路径，最多 limit 条。
def scan_files_for_at(prefix: str, work_dir: Path, limit: int = 8) -> list[str]:
    result: list[str] = []
    prefix_lower = prefix.lower()
    for path in work_dir.rglob("*"):
        if not path.is_file():
            continue
        # 跳过 .seacode / .git / __pycache__ / .venv / node_modules 等目录下的文件。
        if any(part in _AT_SKIP_DIRS for part in path.parts):
            continue
        name = path.name
        if name.lower().startswith(prefix_lower):
            try:
                rel = path.relative_to(work_dir)
            except ValueError:
                continue
            result.append(str(rel).replace("\\", "/"))
            if len(result) >= limit:
                break
    return result


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
    """提供 Enter 发送、Shift+Enter 换行、Tab 补全、方向键历史导航的对话输入框。"""

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True),
        Binding("shift+enter", "newline", "New line", priority=True),
        Binding("tab", "complete", "Complete", priority=True),
        Binding("escape", "dismiss_popup", "Close popup", priority=True),
        Binding("up", "nav_up", "Prev", priority=True),
        Binding("down", "nav_down", "Next", priority=True),
    ]

    # 携带已确认发送的非空用户文本。
    class Submitted(TextualMessage):
        # 保存本次提交的纯文本内容。
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    # 请求 Tab 补全：父组件查注册中心，单匹配填入、多匹配弹窗。
    class TabComplete(TextualMessage):
        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix

    # 实时刷新斜杠命令补全弹窗；prefix 为 None 时关闭弹窗。
    class SlashMenuUpdate(TextualMessage):
        def __init__(self, prefix: str | None) -> None:
            super().__init__()
            self.prefix = prefix

    # 请求 @ 文件引用补全：父组件扫描工作目录返回候选。
    class AtFileRequest(TextualMessage):
        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix

    # 初始化命令历史导航状态；history_file 在 SeaCodeApp.on_mount 后通过 load_history 注入。
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int = -1
        self._history_draft: str = ""
        self._history_file: Path | None = None

    # 从 <work_dir>/.seacode/history 加载非空历史行；文件不存在不抛异常。
    def load_history(self, work_dir: Path) -> None:
        self._history_file = work_dir / ".seacode" / "history"
        self._history = []
        if not self._history_file.exists():
            return
        try:
            for line in self._history_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._history.append(line)
        except OSError:
            self._history = []

    # 追加一行到历史文件；空文本不写入；异常静默避免污染主流程。
    def _persist_entry(self, text: str) -> None:
        if not text.strip() or self._history_file is None:
            return
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with self._history_file.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass

    # 弹窗可见时选择当前项作为提交文本发出；不可见时走 Batch 01 既定提交逻辑。
    # 提交非空文本后持久化到历史文件。
    def action_submit(self) -> None:
        if self.disabled:
            return
        app = self.app
        popup = _get_completion_popup(app)
        if popup is not None and popup.is_visible:
            selected = popup.get_selected()
            if selected is not None:
                popup.hide()
                self._persist_entry(selected)
                self.post_message(self.Submitted(selected))
                self.clear()
            return
        text = self.text.strip()
        if text:
            self._persist_entry(text)
            self.post_message(self.Submitted(text))
            self.clear()
            self._history_index = -1
            self._history_draft = ""

    # 在多行提示中保留显式换行行为。
    def action_newline(self) -> None:
        self.insert("\n")

    # Tab 补全：弹窗可见时选择当前项填入；否则 / 开头发 TabComplete，其它插入制表符。
    def action_complete(self) -> None:
        app = self.app
        popup = _get_completion_popup(app)
        if popup is not None and popup.is_visible:
            selected = popup.get_selected()
            if selected is not None:
                self.text = selected
                self.cursor_location = (0, len(selected))
                popup.hide()
            return
        if self.text.startswith("/"):
            self.post_message(self.TabComplete(self.text))
        else:
            self.insert("\t")

    # ESC 关闭补全弹窗；弹窗不可见时不响应（避免与 SeaCodeApp 的取消回合冲突）。
    def action_dismiss_popup(self) -> None:
        popup = _get_completion_popup(self.app)
        if popup is not None and popup.is_visible:
            popup.hide()

    # 上键：弹窗可见时移动光标，否则遍历命令历史。
    def action_nav_up(self) -> None:
        popup = _get_completion_popup(self.app)
        if popup is not None and popup.is_visible:
            popup.move_up()
            return
        if not self._history:
            return
        if self._history_index == -1:
            self._history_draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self.text = self._history[self._history_index]
        self.cursor_location = (0, len(self.text))

    # 下键：弹窗可见时移动光标，否则遍历命令历史，到末尾恢复草稿。
    def action_nav_down(self) -> None:
        popup = _get_completion_popup(self.app)
        if popup is not None and popup.is_visible:
            popup.move_down()
            return
        if not self._history or self._history_index == -1:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.text = self._history[self._history_index]
        else:
            self._history_index = -1
            self.text = self._history_draft
        self.cursor_location = (0, len(self.text))

    # 文本变化时检测 / 与 @ 前缀，发出 SlashMenuUpdate 或 AtFileRequest 让父组件刷新弹窗。
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        del event
        text = self.text
        if text.startswith("/") and " " not in text and "\n" not in text:
            self.post_message(self.SlashMenuUpdate(text))
        else:
            self.post_message(self.SlashMenuUpdate(None))
        # @ 文件引用：取最后一个 @ 后的前缀。
        at_idx = text.rfind("@")
        if at_idx != -1:
            tail = text[at_idx + 1:]
            if tail and " " not in tail and "\n" not in tail:
                self.post_message(self.AtFileRequest(tail))


# 安全获取 SeaCodeApp 的 CompletionPopup；非 SeaCodeApp 环境返回 None。
def _get_completion_popup(app: Any) -> CompletionPopup | None:
    if not isinstance(app, SeaCodeApp):
        return None
    return app._completion_popup


class SeaCodeApp(App[None]):
    """管理 Provider 选择、单活动回合和可恢复流式呈现。"""

    CSS_PATH = "styles.tcss"
    TITLE = "SeaCode"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+o", "toggle_tool_blocks", "Toggle tools", priority=True),
        Binding("shift+tab", "cycle_mode", "Cycle mode", priority=True),
        # Ctrl+R 打开会话恢复视图；Ctrl+M 展示当前自动记忆索引。
        # 流式回合期间禁用，避免与活动 Agent Loop 竞争对话历史。
        Binding("ctrl+r", "open_resume", "Resume session", priority=True),
        Binding("ctrl+m", "show_memory", "Show memory", priority=True),
    ]

    # 初始化当前配置、客户端、工具注册中心和单回合状态。
    def __init__(
        self,
        providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
        *,
        client_factory: Callable[[ProviderConfig], LLMClient] = create_client,
        max_steps: int = 100,
        sandbox_cfg: SandboxAppConfig | None = None,
        mcp_servers: tuple[MCPServerConfig, ...] | list[MCPServerConfig] | None = None,
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
        # MCP 管理器；加载配置后在 Agent.run 首轮批量连接，无配置时为 None。
        self._mcp_manager: MCPManager | None = None
        if mcp_servers:
            self._mcp_manager = MCPManager()
            self._mcp_manager.load_configs(list(mcp_servers))
        # MCP 连接摘要；首轮 Agent.run 通过 MCPConnectEvent 回填，供状态栏展示。
        self._mcp_summary: str = ""
        # 跨会话记忆与持久化：在 _select_provider 中按工作目录装配。
        # session_manager 创建/列举/恢复/删除 .seacode/sessions 下的 JSONL；
        # memory_manager 提供指令加载与 MEMORY.md 索引；session 是当前活跃句柄。
        # _instructions_content 缓存 load_instructions 的拼接结果，供 Agent 注入。
        self._session_manager: SessionManager | None = None
        self._session: Session | None = None
        self._memory_manager: MemoryManager | None = None
        self._instructions_content: str = ""
        # batch09：本地命令框架。registry 集中注册 11 条内置命令；
        # CompletionPopup 在 compose 中挂载到 input-area，由 ChatInput 触发显示/隐藏。
        # _agent 保存当前回合 Agent 引用，供命令路径访问（首次回合前为 None）。
        self._command_registry = CommandRegistry()
        register_all_commands(self._command_registry)
        self._completion_popup = CompletionPopup()
        self._agent: Agent | None = None

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
            yield self._completion_popup
            with Horizontal(id="status-bar"):
                yield Static("Preparing configuration", id="turn-status")
                yield Static("", id="model-label")
                yield Static("", id="mode-label")
                yield Static("", id="mcp-label")

    # 根据 Provider 数量进入选择状态或直接准备单一配置。
    def on_mount(self) -> None:
        self.query_one(ChatInput).disabled = True
        # 加载命令历史；无文件不抛异常。
        try:
            self.query_one(ChatInput).load_history(Path(os.getcwd()))
        except Exception:
            pass
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
        # 装配跨会话能力：load_instructions 拼接项目/用户级 SEACODE.md 与 AGENTS.md；
        # MemoryManager 提供双目录记忆索引；SessionManager 创建新 JSONL 并清理过期会话。
        # 失败均不阻断启动——记忆/会话功能降级为不可用，但 Provider 仍可正常对话。
        work_dir = os.getcwd()
        try:
            self._instructions_content = load_instructions(work_dir)
            self._memory_manager = MemoryManager(work_dir)
            self._session_manager = SessionManager(work_dir)
            self._session_manager.cleanup()
            self._session = self._session_manager.create()
        except Exception:
            self._instructions_content = ""
            self._memory_manager = None
            self._session_manager = None
            self._session = None
        self.query_one("#chat-area").display = True
        self.query_one("#input-area").display = True
        if len(self._providers) > 1:
            self.query_one("#provider-select").display = False
        self.query_one("#title-bar", Static).update(self._make_banner(work_dir))
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

    # 接收输入消息：先展开 @ 文件引用，再判断 / 开头走命令分发，否则进入 Agent Loop。
    # 命令路径不进入 Agent Loop，避免本地操作消耗 token；流式期间命令仍可执行（如 /clear）。
    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        if self._client is None:
            return
        text = event.text
        if "@" in text:
            text = expand_at_refs(text, Path(os.getcwd()))
        if text.startswith("/"):
            asyncio.create_task(self._dispatch_command(text))
            return
        if self._streaming:
            return
        self._streaming = True
        self._agent_task = asyncio.create_task(self._run_turn(text))

    # 分发斜杠命令：解析 → 空命令名列出全部 → 查找 → 未找到提示 → 缺参数提示 →
    # 执行 handler → 异常显示错误。返回是否走命令路径（始终 True，因为只在 / 开头时调用）。
    async def _dispatch_command(self, text: str) -> bool:
        name, args, is_command = parse_command(text)
        if not is_command:
            return False
        if not name:
            cmds = self._command_registry.list_commands()
            lines = ["可用命令："]
            for c in cmds:
                lines.append(f"  /{c.name:<12} - {c.description}")
            await self._show_system_message("\n".join(lines))
            return True
        cmd = self._command_registry.find(name)
        if cmd is None:
            await self._show_system_message(
                f"未知命令：{name}，输入 /help 查看可用命令"
            )
            return True
        if cmd.arg_prompt and not args.strip():
            await self._show_system_message(
                f"参数不足：{cmd.arg_prompt}\n用法：{cmd.usage}"
            )
            return True
        ctx = self._build_command_context(args)
        try:
            await cmd.handler(ctx)
        except Exception as exc:
            await self._show_system_message(f"命令执行失败：{exc}")
        return True

    # 构造命令执行上下文：注入业务对象与 UI 状态回调。
    def _build_command_context(self, args: str) -> CommandContext:
        return CommandContext(
            args=args,
            agent=self._agent,
            conversation=self._conversation,
            session=self._session,
            session_manager=self._session_manager,
            memory_manager=self._memory_manager,
            ui=self,  # SeaCodeApp 实现 UIController Protocol
            config={
                "registry": self._command_registry,
                "set_session": self._set_session,
                "set_conversation": self._set_conversation,
                "clear_chat": self._clear_chat,
                "render_restored": lambda msgs: asyncio.create_task(
                    self._render_restored_messages(msgs)
                ),
            },
        )

    # UIController: 在对话区追加系统消息（同步入口，内部异步调度）。
    def add_system_message(self, text: str) -> None:
        self.call_after_refresh(self._show_system_message, text)

    # UIController: 把构造好的提示词当作用户消息发给 LLM，触发 Agent Loop。
    def send_user_message(self, text: str) -> None:
        if self._streaming or self._client is None:
            return
        self._streaming = True
        self._agent_task = asyncio.create_task(self._run_turn(text))

    # UIController: 切换 Plan 模式；同步 permission_checker.mode 保持一致。
    def set_plan_mode(self, enabled: bool) -> None:
        new_mode = PermissionMode.PLAN if enabled else PermissionMode.DEFAULT
        self._permission_mode = new_mode
        if self._permission_checker is not None:
            self._permission_checker.mode = new_mode
        self._update_mode_label()

    # UIController: 返回当前 token 用量与上限，供 /status 与 /compact 使用。
    def get_token_count(self) -> tuple[int, int]:
        used = getattr(self._conversation, "estimated_tokens", 0)
        limit = 0
        if self._selected_provider is not None:
            limit = self._selected_provider.get_context_window()
        return (used, limit)

    # UIController: 刷新状态栏。
    def refresh_status(self) -> None:
        self._update_mode_label()
        self._refresh_mcp_status()

    # 会话状态回调：供 /clear 与 /session new/resume 切换当前会话句柄。
    def _set_session(self, session: Session) -> None:
        self._session = session

    # 会话状态回调：供 /session resume 替换当前对话历史。
    def _set_conversation(self, conversation: ConversationManager) -> None:
        self._conversation = conversation

    # 会话状态回调：清空对话区与对话历史，供 /clear 与 /session new 使用。
    def _clear_chat(self) -> None:
        self._conversation = ConversationManager()
        try:
            chat = self.query_one("#chat-area", VerticalScroll)
            for child in list(chat.children):
                child.remove()
        except Exception:
            pass

    # ChatInput.TabComplete 消息处理：单匹配填入输入框，多匹配弹窗，无匹配不响应。
    def on_chat_input_tab_complete(self, event: ChatInput.TabComplete) -> None:
        pairs = complete(self._command_registry, event.prefix)
        if not pairs:
            return
        if len(pairs) == 1:
            value = pairs[0][1]
            input_widget = self.query_one(ChatInput)
            input_widget.text = value
            input_widget.cursor_location = (0, len(value))
            self._completion_popup.hide()
            return
        self._completion_popup.show_pairs(pairs)

    # ChatInput.SlashMenuUpdate 消息处理：prefix 为 None 或无匹配时关闭弹窗，否则刷新。
    def on_chat_input_slash_menu_update(self, event: ChatInput.SlashMenuUpdate) -> None:
        if event.prefix is None:
            self._completion_popup.hide()
            return
        pairs = complete(self._command_registry, event.prefix)
        if not pairs:
            self._completion_popup.hide()
            return
        self._completion_popup.show_pairs(pairs)

    # ChatInput.AtFileRequest 消息处理：扫描工作目录，有候选显示弹窗，无候选关闭。
    def on_chat_input_at_file_request(self, event: ChatInput.AtFileRequest) -> None:
        candidates = scan_files_for_at(event.prefix, Path(os.getcwd()))
        if not candidates:
            self._completion_popup.hide()
            return
        # @ 文件引用补全的 value 带 @ 前缀，便于直接填入输入框。
        pairs = [(f"@{c}", f"@{c} ") for c in candidates]
        self._completion_popup.show_pairs(pairs)

    # CompletionPopup.Selected 消息处理：把选中值填入输入框并关闭弹窗。
    def on_completion_popup_selected(self, event: Selected) -> None:
        input_widget = self.query_one(ChatInput)
        input_widget.text = event.value
        input_widget.cursor_location = (0, len(event.value))
        self._completion_popup.hide()
        input_widget.focus()

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
                mcp_manager=self._mcp_manager,
                context_window=provider.get_context_window(),
                instructions_content=self._instructions_content,
                memory_manager=self._memory_manager,
            )
            # 保存当前回合 Agent 引用，供命令路径（/status /compact /plan 等）访问。
            self._agent = agent
            # 同步当前会话 ID 给 Agent，仅用于压缩摘要里的 transcript_path 提示。
            if self._session is not None:
                agent.session_id = self._session.session_id
            # 持久化游标：记录已写入 JSONL 的历史末尾位置。
            # TurnComplete 增量追加新消息；CompactNotification 先写 boundary 再推进游标。
            history_cursor = len(self._conversation.messages)
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
                elif isinstance(event, MCPConnectEvent):
                    # MCP 批量连接完成：刷新状态栏摘要，连接错误以系统消息展示。
                    self._mcp_summary = self._format_mcp_summary(event)
                    self._refresh_mcp_status()
                    if event.errors:
                        for err in event.errors:
                            await self._show_system_message(f"MCP: {err}")
                elif isinstance(event, CompactNotification):
                    # Layer 2 压缩完成：以系统消息呈现压缩前 token 数，不重排既有界面结构。
                    await self._show_system_message(f"⚙ {event.message}")
                    chat.scroll_end(animate=False)
                    # 持久化 compact_boundary：将摘要 + 原样保留的尾部内联成一条记录，
                    # resume 时只需这一条即可重建压缩后状态。然后推进游标到重建后的
                    # 历史末尾，避免 TurnComplete/LoopComplete 把已压缩前缀重复写入。
                    self._persist_compact_boundary(event)
                    history_cursor = len(self._conversation.messages)
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
                    # 增量持久化：把本回合新增的消息（user + assistant
                    # + tool_results）追加到 JSONL。
                    if self._session is not None:
                        for msg in self._conversation.messages[history_cursor:]:
                            self._session.append(msg)
                        history_cursor = len(self._conversation.messages)
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
                    # 收尾持久化：把最后一轮 assistant 回复追加到 JSONL，
                    # 并更新 meta.total_tokens 与 summary（后台异步生成）。
                    if self._session is not None:
                        for msg in self._conversation.messages[history_cursor:]:
                            self._session.append(msg)
                        history_cursor = len(self._conversation.messages)
                        self._session.meta.total_tokens = (
                            agent.total_input_tokens + agent.total_output_tokens
                        )
                        asyncio.ensure_future(self._update_session_summary())

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

    # 把 MCPConnectEvent 转为状态栏可展示的简短摘要文本。
    # 连接成功时显示服务器数与工具数；有错误时附加错误数；无配置时返回空串。
    def _format_mcp_summary(self, event: MCPConnectEvent) -> str:
        parts: list[str] = []
        if event.server_count:
            parts.append(f"{event.server_count} MCP server(s)")
        if event.tool_count:
            parts.append(f"{event.tool_count} tool(s)")
        if event.errors:
            parts.append(f"{len(event.errors)} error(s)")
        return " · ".join(parts)

    # 把当前 MCP 摘要刷新到状态栏 mcp-label；无摘要时清空。
    def _refresh_mcp_status(self) -> None:
        try:
            label = self.query_one("#mcp-label", Static)
        except Exception:
            return
        if self._mcp_summary:
            label.update(Text(f"⟂ {self._mcp_summary}", style="#a3be8c"))
        else:
            label.update(Text(""))

    # -----------------------------------------------------------------
    # batch08：会话持久化、记忆索引与 session_dialog 集成
    # -----------------------------------------------------------------

    # 将 CompactNotification 携带的 boundary 持久化为一条 COMPACT_BOUNDARY 记录。
    # boundary 内联了摘要 + 原样保留的尾部，resume 时只需这一条即可重建压缩后状态。
    # 没有活跃 session 或 compact 未产出 boundary 时直接跳过。
    def _persist_compact_boundary(self, notification: CompactNotification) -> None:
        if self._session is None or notification.boundary is None:
            return
        record = make_compact_boundary(
            summary=notification.boundary.summary,
            keep=notification.boundary.keep,
        )
        self._session.append_record(record)

    # 后台异步生成会话摘要并写入 .meta；失败静默不影响主循环。
    # 摘要取最近 10 条消息裸 LLM 调用一句话总结，用于 session_dialog 列表展示。
    async def _update_session_summary(self) -> None:
        if self._session is None or self._client is None or self._selected_provider is None:
            return
        try:
            summary = await generate_session_summary(
                self._client, self._conversation, self._selected_provider.protocol
            )
            if summary and self._session is not None:
                self._session.meta.summary = summary
                self._session.meta.save(
                    self._session._sessions_dir / f"{self._session.session_id}.meta"
                )
        except Exception:
            pass

    # Ctrl+R：打开内联会话恢复视图。流式回合期间禁用避免与活动 Agent Loop 竞争历史。
    # 视图挂载到 chat-area 顶部并获取焦点；空列表时直接关闭。
    async def action_open_resume(self) -> None:
        if self._streaming or self._session_manager is None:
            return
        sessions = self._session_manager.list()
        widget = InlineResumeWidget(sessions, project_name=os.getcwd())
        chat = self.query_one("#chat-area", VerticalScroll)
        await chat.mount(widget)
        widget.focus()

    # Ctrl+M：展示当前自动记忆索引与目录路径。无 memory_manager 时给出降级提示。
    async def action_show_memory(self) -> None:
        if self._memory_manager is None:
            await self._show_system_message("记忆系统未启用（工作目录不可写）")
            return
        text = self._memory_manager.get_display_text()
        await self._show_system_message(text)

    # 接收 InlineResumeWidget 的选择结果：None 表示取消，非空则恢复该会话。
    # 恢复时关闭旧 session 句柄、替换对话历史、重新渲染已有消息，并重置游标。
    async def on_inline_resume_widget_selected(
        self, event: InlineResumeWidget.Selected
    ) -> None:
        # 先移除 widget，无论是否真的恢复会话。
        try:
            widget = self.query_one("#resume-inline", InlineResumeWidget)
            await widget.remove()
        except Exception:
            pass

        if event.session_id is None or self._session_manager is None:
            return

        # 流式回合期间不允许恢复，避免与活动 Agent Loop 竞争历史。
        if self._streaming:
            await self._show_system_message("回合进行中，无法恢复会话")
            return

        result = self._session_manager.resume(event.session_id)
        if result is None:
            await self._show_system_message("会话恢复失败：文件已损坏或被删除")
            return

        # 关闭旧 session 句柄，替换为恢复的句柄；重置对话历史并渲染已恢复的消息。
        if self._session is not None:
            self._session.close()
        self._session = result.session
        new_conv = ConversationManager()
        for msg in result.messages:
            new_conv.history.append(msg)
        self._conversation = new_conv
        await self._render_restored_messages(result.messages)
        await self._show_system_message(
            f"已恢复会话 {result.session.session_id}（{len(result.messages)} 条消息）"
        )

    # 把恢复的消息渲染到 chat-area；先清空旧内容再按角色挂载用户/助手行。
    # tool_results 与空内容消息跳过，避免渲染工具调用回灌噪音。
    async def _render_restored_messages(self, messages: list[Message]) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        await chat.remove_children()
        for msg in messages:
            if msg.tool_results or not msg.content:
                continue
            if msg.role == "user":
                user_message = Text()
                user_message.append("❯ ", style="bold #71b8bc")
                user_message.append(msg.content, style="bold #f2f5f5")
                await chat.mount(Static(user_message, classes="message user-message"))
            elif msg.role == "assistant":
                await chat.mount(
                    Markdown(msg.content, classes="message assistant-markdown")
                )
        chat.scroll_end(animate=False)

    # 应用退出时关闭当前 session 文件句柄，避免句柄泄露。
    def on_unmount(self) -> None:
        if self._session is not None:
            self._session.close()
