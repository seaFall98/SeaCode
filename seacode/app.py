"""SeaCode 第 05 步的紧凑 Textual 对话界面，支持 Agent Loop、工具调用与权限确认。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.timer import Timer
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
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
from .agents import (
    AgentLoader,
    TaskManager,
    TraceManager,
    inject_task_notifications,
)
from .askuser_dialog import InlineAskUserWidget
from .client import (
    AuthenticationError,
    LLMClient,
    LLMError,
    NetworkError,
    RateLimitError,
    TextDelta,
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
from .commands.handlers.skill import SKILL_COMMAND
from .commands.handlers.skill_register import (
    make_skill_register_callback,
    register_skill_commands,
)
from .commands.handlers.tasks import create_tasks_command
from .commands.handlers.trace import create_trace_command
from .commands.handlers.worktree import create_worktree_command
from .config import MCPServerConfig, ProviderConfig, SandboxAppConfig, WorktreeConfig
from .context import CompactCircuitBreaker, RecoveryState, create_replacement_state
from .conversation import ConversationManager, Message
from .filehistory.history import FileHistory
from .hooks import HookContext, HookEngine
from .mcp import MCPManager
from .memory import (
    MemoryManager,
    Session,
    SessionManager,
    find_relevant_memories,
    generate_session_summary,
    load_instructions,
    make_compact_boundary,
    render_reminder,
)
from .permission_dialog import InlinePermissionWidget
from .permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from .plan_dialog import InlinePlanWidget, PlanChoice
from .prompts import build_plan_mode_exit_reminder
from .sandbox import SandboxConfig, create_sandbox
from .session_dialog import InlineResumeWidget
from .skills import SkillExecutor, SkillLoader
from .teammate_tree import TeammateTree
from .teams.manager import TeamManager
from .tools import create_default_registry
from .tools.agent_tool import AgentTool
from .tools.ask_user import AskUserTool
from .tools.base import ToolResult
from .tools.bash import Bash
from .tools.enter_worktree import EnterWorktreeTool
from .tools.exit_plan_mode import ExitPlanModeTool
from .tools.exit_worktree import ExitWorktreeTool
from .tools.install_skill import InstallSkill
from .tools.load_skill import LoadSkill

# batch14：团队协调工具；TeamCreate/TeamDelete/SendMessage 在装配阶段注册到 Lead 工具集。
from .tools.send_message import SendMessageTool
from .tools.team_create import TeamCreateTool
from .tools.team_delete import TeamDeleteTool
from .worktree.cleanup import start_stale_cleanup_task
from .worktree.manager import WorktreeManager

log = logging.getLogger(__name__)

# 工具调用详情展示的最大行数，超过则截断并提示剩余行数。
MAX_TRUNCATED_LINES: int = 20

# 可折叠的工具集合：只读工具在多工具回合时折叠为摘要。
COLLAPSIBLE_TOOLS: frozenset[str] = frozenset({"ReadFile", "Glob", "Grep"})

# braille spinner 动画帧，每帧 80ms 切换；thinking 期间持续旋转。
SPINNER_FRAMES: str = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

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


# batch12：子 Agent 调用展示块；与 ToolCallBlock 同层级但呈现风格不同。
# 运行中显示品牌色圆点 + agent_type + description；完成后折叠为 "⎿  Done (N tool uses · X.Xs)"。
# 点击或 ctrl+o 切换展开/折叠；展开态显示前 300 字符结果预览。
class SubAgentBlock(Static, can_focus=True):
    """子 Agent 调用的运行中/完成态展示块。"""

    # description 截断上限；超过 60 字符截断保留前 60。
    _DESC_LIMIT: int = 60
    # 完成态展开时显示的结果预览字符数。
    _PREVIEW_LIMIT: int = 300

    def __init__(
        self, agent_type: str, description: str, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._agent_type = agent_type
        # description 截断到 60 字符，避免状态行过长。
        self._description = description[: self._DESC_LIMIT]
        self._output = ""
        self._is_error = False
        self._elapsed = 0.0
        self._tool_count = 0
        self._collapsed = True
        self._loading = True
        self._render_running()

    # 运行中态：品牌色圆点 + agent_type + description + "Running…"。
    def _render_running(self) -> None:
        line = f"  ● {self._agent_type}"
        if self._description:
            line += f" — {self._description}"
        line += "  Running…"
        self.update(line)
        self.add_class("tool-block-loading")

    # 接收子 Agent 执行结果；解析工具数并切换到完成态。
    def set_result(self, output: str, is_error: bool, elapsed: float) -> None:
        self._output = output
        self._is_error = is_error
        self._elapsed = elapsed
        self._tool_count = self._parse_stats(output)
        self._loading = False
        self.remove_class("tool-block-loading")
        if self._is_error:
            self.add_class("tool-block-error")
        self._collapsed = True
        self._render_collapsed()

    # 从子 Agent 输出文本中解析工具调用次数；匹配 "N tool uses" 模式。
    def _parse_stats(self, output: str) -> int:
        import re

        match = re.search(r"(\d+)\s+tool\s+uses?", output, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return 0
        return 0

    # 折叠态：状态符号 + Done + 工具数 + 耗时 + 展开提示。
    def _render_collapsed(self) -> None:
        if self._is_error:
            symbol = "✗"
        else:
            symbol = "⎿"
        parts: list[str] = []
        if self._tool_count > 0:
            parts.append(f"{self._tool_count} tool uses")
        parts.append(f"{self._elapsed:.1f}s")
        stats = " · ".join(parts)
        line = f"  {symbol}  Done ({stats})  (ctrl+o to expand)"
        self.update(line)

    # 展开态：状态符号 + Done + 工具数 + 耗时 + 前 300 字符结果预览。
    def _render_expanded(self) -> None:
        if self._is_error:
            symbol = "✗"
        else:
            symbol = "⎿"
        parts: list[str] = []
        if self._tool_count > 0:
            parts.append(f"{self._tool_count} tool uses")
        parts.append(f"{self._elapsed:.1f}s")
        stats = " · ".join(parts)
        header = f"  {symbol}  Done ({stats})"
        preview = self._output[: self._PREVIEW_LIMIT]
        if len(self._output) > self._PREVIEW_LIMIT:
            preview += "…"
        self.update(f"{header}\n  {preview}")

    # 点击切换展开/折叠；loading 态不响应。
    def on_click(self) -> None:
        if self._loading:
            return
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._render_collapsed()
        else:
            self._render_expanded()


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
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        sandbox_cfg: SandboxAppConfig | None = None,
        mcp_servers: tuple[MCPServerConfig, ...] | list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        # batch12：子 Agent 系统；enable_fork 开启 Fork 路径，
        # enable_verification_agent 加载内置 Verification 子 Agent。
        enable_fork: bool = False,
        enable_verification_agent: bool = False,
        # batch13：Worktree 隔离工作区配置；从 .seacode/config.yaml 的 worktree 段加载。
        worktree_cfg: WorktreeConfig | None = None,
        # batch14：团队协调配置；teammate_mode 指定 spawn 后端，enable_coordinator_mode
        # 开启 Lead 工具收敛与协调者提示词。两层字段由 __main__ 从 AppConfig 透传。
        teammate_mode: str = "",
        enable_coordinator_mode: bool = False,
    ) -> None:
        super().__init__()
        self._providers = tuple(providers)
        self._client_factory = client_factory
        self._client: LLMClient | None = None
        self._conversation = ConversationManager()
        self._selected_provider: ProviderConfig | None = None
        self._tool_registry = create_default_registry()
        # 压缩恢复状态属于应用会话，跨每条用户消息新建的 Agent 保持连续。
        self._recovery_state = RecoveryState()
        self._compact_breaker = CompactCircuitBreaker()
        self._replacement_state = create_replacement_state()
        self._active_skills: dict[str, str] = {}
        self._plan_exit_requested = False
        self._plan_approval_active = False
        self._pre_plan_mode = PermissionMode.DEFAULT
        # 记录本次会话是否曾退出过 Plan Mode，供 /plan 重入时注入 reentry reminder。
        self._has_exited_plan_mode: bool = False
        self._streaming = False
        # spinner 动画状态：thinking 期间持续旋转，显示 ⠋ verb… (Ns)。
        self._thinking_start: float = 0.0
        self._thinking_verb: str = ""
        self._spinner_idx: int = 0
        self._spinner_timer: Timer | None = None
        self._spinner_label: Static | None = None
        self._agent_task: asyncio.Task[None] | None = None
        self._max_steps = max_steps
        # 启动模式由 CLI 的显式参数或配置文件决定，装配检查器时一次性应用。
        self._initial_permission_mode = permission_mode
        # OS 级沙箱配置；从 .seacode/config.yaml 的 sandbox 段加载，默认全关闭。
        self._sandbox_cfg = sandbox_cfg or SandboxAppConfig()
        # batch13：Worktree 隔离工作区配置与运行时管理器。
        # worktree_cfg 在 _select_provider 中按配置初始化 WorktreeManager；
        # worktree_manager / file_history 在装配阶段填充，None 时关闭相关路径。
        self._worktree_cfg = worktree_cfg or WorktreeConfig()
        self.worktree_manager: WorktreeManager | None = None
        self.file_history: FileHistory | None = None
        self._stale_cleanup_task: asyncio.Task[None] | None = None
        # restore_session 是 async，但 _assemble_worktree_system 是 sync；
        # 故调度为 task，在 _run_turn 创建 Agent 前 await 并据结果切换 work_dir。
        self._restore_session_task: asyncio.Task[Any] | None = None
        # batch11：生命周期 Hook 引擎；为 None 时所有注入点零开销。
        # 由 __main__.py 通过 load_hooks(config.raw_hooks) 构造后注入；
        # on_mount 时触发 startup 事件，on_unmount 时触发 shutdown 事件。
        self._hook_engine: HookEngine | None = hook_engine
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
        # batch12：子 Agent 系统。AgentLoader 三级搜索 + 热重载；
        # TaskManager 后台任务状态机 + 通知队列；TraceManager 调用链追踪。
        # _subagent_task 跟踪前台运行中的子 Agent（用于 ESC adopt_running）；
        # _notification_polling_task 每 2 秒轮询完成的后台任务并注入通知。
        # _ask_user_tool 持有 AskUserTool 引用供 _pending_event 检查；
        # _enable_fork / _enable_verification_agent 由构造参数注入。
        self._enable_fork = enable_fork
        self._enable_verification_agent = enable_verification_agent
        self.agent_loader: AgentLoader | None = None
        self.task_manager: TaskManager | None = None
        self.trace_manager: TraceManager | None = None
        self._subagent_task: asyncio.Task[Any] | None = None
        self._notification_polling_task: asyncio.Task[None] | None = None
        self._ask_user_tool: AskUserTool | None = None
        self._agent_tool: AgentTool | None = None
        # batch09：本地命令框架。registry 集中注册 11 条内置命令；
        # CompletionPopup 在 compose 中挂载到 input-area，由 ChatInput 触发显示/隐藏。
        # _agent 保存当前回合 Agent 引用，供命令路径访问（首次回合前为 None）。
        self._command_registry = CommandRegistry()
        register_all_commands(self._command_registry)
        self._completion_popup = CompletionPopup()
        self._agent: Agent | None = None
        # batch10：Skill 系统。loader 两级搜索项目级与用户级 skills 目录；
        # executor 持有当前回合 Agent 引用（_run_turn 中刷新），inline/fork 执行 Skill。
        # load_skill/install_skill 工具注入到 ToolRegistry，/skill 命令注册到 CommandRegistry。
        # 每个 Skill 自动注册为 PROMPT 斜杠命令；reload 时通过回调重注册。
        self._skill_loader = SkillLoader(
            project_dir=Path(os.getcwd()) / ".seacode" / "skills",
            user_dir=Path.home() / ".seacode" / "skills",
        )
        self._skill_loader.load_all()
        # executor 初始 agent=None，_run_turn 中刷新为当前回合 Agent。
        self._skill_executor = SkillExecutor(agent=None)
        self._load_skill_tool = LoadSkill()
        self._load_skill_tool.set_loader(self._skill_loader)
        self._install_skill_tool = InstallSkill()
        self._install_skill_tool.set_loader(self._skill_loader)
        self._install_skill_tool.set_on_installed(self._make_skill_register_callback())
        self._tool_registry.register(self._load_skill_tool)
        self._tool_registry.register(self._install_skill_tool)
        self._command_registry.register_sync(SKILL_COMMAND)
        register_skill_commands(
            self._command_registry, self._skill_loader, self._skill_executor
        )
        self._skill_loader.register_reload_callback(self._make_skill_register_callback())
        # batch14：团队协调。team_manager 在 _select_provider 装配阶段初始化，
        # 依赖 worktree_manager 与 trace_manager；为 None 时关闭团队路径。
        # teammate_tree 在 compose 中挂载，周期刷新 task 每秒拉取 progress。
        # _teams_config 持 teammate_mode / enable_coordinator_mode 供 TeamCreateTool 读取。
        self._teammate_mode = teammate_mode
        self._enable_coordinator_mode = enable_coordinator_mode
        self._teams_config = SimpleNamespace(
            teammate_mode=teammate_mode,
            enable_coordinator_mode=enable_coordinator_mode,
        )
        self.team_manager: TeamManager | None = None
        self.teammate_tree: TeammateTree | None = None
        self._teammate_refresh_task: asyncio.Task[None] | None = None

    # 生成三行品牌标题，保留终端对话的既定信息层级。
    @staticmethod
    def _make_banner(work_dir: str = "") -> Text:
        banner = Text()
        banner.append("    ╱─╲    ", style="bold #5eb6c8")
        banner.append("SeaCode\n", style="#c7d2d5")
        banner.append("   ║ █ ║   ", style="bold #5eb6c8")
        banner.append(f"{work_dir}\n" if work_dir else "\n", style="#9fb2b6")
        banner.append("  ╱╲ ", style="bold #5eb6c8")
        banner.append("▄", style="bold #d9a441")
        banner.append(" ╱╲  ", style="bold #5eb6c8")
        return banner

    # 构造标题、选择、聊天、输入和横向状态栏五个既定区域。
    # batch14：在 chat-area 之前挂载 TeammateTree；初始 display=False 避免占用空间，
    # 周期刷新 task 在检测到 teammates 时切换 display=True。
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
        # batch14：TeammateTree 占位；空 teammates 时渲染空 Text 不影响既有布局。
        self.teammate_tree = TeammateTree(id="teammate-tree")
        yield self.teammate_tree
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
        # batch14：TeammateTree 初始隐藏；周期刷新 task 在检测到 teammates 时显示。
        if self.teammate_tree is not None:
            self.teammate_tree.display = False
        # 加载命令历史；无文件不抛异常。
        try:
            self.query_one(ChatInput).load_history(Path(os.getcwd()))
        except Exception:
            pass
        # batch11：触发 startup 事件；ensure_future 后台执行避免阻塞 TUI 启动。
        # Hook 抛异常由 _run_single 兜底捕获记 warning，不影响 TUI 主流程。
        if self._hook_engine is not None:
            asyncio.ensure_future(self._trigger_startup_hooks())
        if len(self._providers) == 1:
            self._select_provider(self._providers[0])
        else:
            self.query_one("#chat-area").display = False
            self.query_one("#input-area").display = False

    # batch11：触发 startup Hook 并把累积通知渲染到对话区作为状态行。
    async def _trigger_startup_hooks(self) -> None:
        if self._hook_engine is None:
            return
        try:
            await self._hook_engine.run_hooks(
                "startup", HookContext(event_name="startup")
            )
            for n in self._hook_engine.drain_notifications():
                await self._render_hook_notification(n)
        except Exception:
            # 启动 Hook 异常不阻断 TUI；记静默即可。
            pass

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
        self._tool_registry.register(
            ExitPlanModeTool(
                is_plan_mode=lambda: self._permission_mode == PermissionMode.PLAN,
                plan_exists=lambda: bool(
                    self._permission_checker
                    and self._permission_checker.plan_file_path
                    and Path(self._permission_checker.plan_file_path).exists()
                ),
            )
        )
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
        # batch12：装配子 Agent 系统。AgentLoader 三级搜索 + 热重载；
        # TaskManager 后台任务状态机；TraceManager 调用链追踪。
        # AgentTool / AskUserTool 注入到 ToolRegistry；/tasks /trace 注册到命令注册中心。
        # 失败不阻断启动——子 Agent 能力降级为不可用，但 Provider 仍可正常对话。
        try:
            self.agent_loader = AgentLoader(
                Path(work_dir),
                enable_verification=self._enable_verification_agent,
            )
            self.task_manager = TaskManager()
            self.trace_manager = TraceManager()
            # AgentTool 的 parent_agent 在 _run_turn 中刷新为当前回合 Agent；
            # 此处传 None 仅作占位，execute 调用时由 agent 传入真实 parent_agent。
            self._agent_tool = AgentTool(
                agent_loader=self.agent_loader,
                task_manager=self.task_manager,
                trace_manager=self.trace_manager,
                parent_agent=None,
                enable_fork=self._enable_fork,
                provider_config=provider,
            )
            self._ask_user_tool = AskUserTool()
            self._tool_registry.register(self._agent_tool)
            self._tool_registry.register(self._ask_user_tool)
            # /tasks /trace 命令注册；lead_agent_id 在 _run_turn 中刷新。
            self._command_registry.register_sync(
                create_tasks_command(self.task_manager)
            )
            self._command_registry.register_sync(
                create_trace_command(self.trace_manager, lead_agent_id=None)
            )
        except Exception:
            self.agent_loader = None
            self.task_manager = None
            self.trace_manager = None
            self._agent_tool = None
            self._ask_user_tool = None
        # batch13：装配 Worktree 隔离工作区与文件历史。
        # WorktreeManager 按配置的 symlink_directories 初始化；restore_session 在
        # 启动时尝试恢复中断的 worktree session。FileHistory 按 session_id 隔离快照，
        # 注入到 write_file/edit_file 工具与 Agent。EnterWorktree/ExitWorktree 工具
        # 与 /worktree /rewind 命令注册到对应注册中心。后台清理 task 按 interval/cutoff
        # 自动回收 stale worktree。失败不阻断启动——worktree 能力降级为不可用。
        self._assemble_worktree_system(work_dir)
        # batch14：装配团队协调系统。TeamManager 依赖 worktree_manager 与 trace_manager，
        # 任一为 None 时仍可工作（路径降级）。TeamCreate/TeamDelete/SendMessage 工具注册到
        # Lead 工具集；Lead Agent 的 _team_manager 与 notification_fn 在 _run_turn 中注入。
        # 周期刷新 task 每秒拉取 teammates progress 并刷新 TeammateTree。失败不阻断启动。
        self._assemble_teams_system()
        # 启动后台任务通知轮询；on_unmount 时取消。
        if self.task_manager is not None and self._notification_polling_task is None:
            try:
                self._notification_polling_task = asyncio.create_task(
                    self._start_notification_polling()
                )
            except RuntimeError:
                # 事件循环尚未启动（如测试环境）；on_mount 会再次尝试。
                self._notification_polling_task = None
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

    # batch13：装配 WorktreeManager、FileHistory、工具与命令注册、后台清理 task。
    # 任一步失败静默降级，不阻断 Provider 选择主流程；worktree_manager 为 None 时
    # AgentTool 的 _execute_with_worktree 路径返回错误，EnterWorktree/ExitWorktree
    # 工具不注册，/worktree /rewind 命令不注册。
    def _assemble_worktree_system(self, work_dir: str) -> None:
        try:
            wt_cfg = self._worktree_cfg
            self.worktree_manager = WorktreeManager(
                repo_root=work_dir,
                symlink_directories=list(wt_cfg.symlink_directories),
            )
            # 注入到 AgentTool 让 _execute_with_worktree 路径可用。
            if self._agent_tool is not None:
                self._agent_tool.set_worktree_manager(self.worktree_manager)
            # 注册 EnterWorktree / ExitWorktree 工具。
            self._tool_registry.register(
                EnterWorktreeTool(self.worktree_manager)
            )
            self._tool_registry.register(
                ExitWorktreeTool(self.worktree_manager)
            )
            # 注册 /worktree 命令；/rewind 已在 register_all_commands 中无条件注册。
            self._command_registry.register_sync(
                create_worktree_command(self.worktree_manager)
            )
            # 调度 restore_session；_run_turn 创建 Agent 前 await 结果，
            # 据恢复的 session 切换 agent.work_dir 到 worktree 路径。
            if self._restore_session_task is None:
                try:
                    self._restore_session_task = asyncio.create_task(
                        self.worktree_manager.restore_session()
                    )
                except RuntimeError:
                    # 事件循环尚未启动（如测试环境）；_run_turn 会再次尝试。
                    self._restore_session_task = None
            # 启动后台 stale worktree 清理 task；on_unmount 时取消。
            if self._stale_cleanup_task is None:
                try:
                    self._stale_cleanup_task = asyncio.create_task(
                        start_stale_cleanup_task(
                            self.worktree_manager,
                            wt_cfg.stale_cleanup_interval,
                            wt_cfg.stale_cutoff_hours,
                        )
                    )
                except RuntimeError:
                    # 事件循环尚未启动（如测试环境）；跳过，下次 _select_provider 重试。
                    self._stale_cleanup_task = None
        except Exception:
            # 装配失败时降级为不可用，主流程继续。
            self.worktree_manager = None
        # FileHistory 装配：按 session_id 隔离快照，注入到 Agent 与写文件工具。
        # 失败不阻断启动；file_history 为 None 时 Agent.run 跳过 make_snapshot。
        try:
            session_id = ""
            if self._session is not None:
                session_id = self._session.session_id
            if session_id:
                self.file_history = FileHistory(work_dir, session_id)
                # 注入到 write_file/edit_file 等持有 file_history 属性的工具。
                for tool in self._tool_registry.list_tools():
                    if hasattr(tool, "file_history"):
                        try:
                            tool.file_history = self.file_history
                        except Exception:
                            continue
        except Exception:
            self.file_history = None

    # batch14：装配 TeamManager 与团队协调工具，并启动 TeammateTree 周期刷新 task。
    # 任一步失败静默降级，不阻断 Provider 选择主流程；team_manager 为 None 时
    # TeamCreate/TeamDelete/SendMessage 工具不注册，Lead 不会进入 Coordinator 模式。
    # Lead Agent 的 _team_manager / notification_fn 在 _run_turn 创建 Agent 后注入，
    # 因为 agent 在每个回合重建，且 notification_fn 需绑定当回合 agent_id 作为 lead_agent_id。
    def _assemble_teams_system(self) -> None:
        try:
            team_manager = TeamManager(
                worktree_manager=self.worktree_manager,
                trace_manager=self.trace_manager,
            )
        except Exception:
            team_manager = None
        if team_manager is None:
            self.team_manager = None
            return
        self.team_manager = team_manager
        try:
            # 注入 TeamManager 到 AgentTool 让 _execute_as_teammate 路径可用。
            if self._agent_tool is not None:
                self._agent_tool.set_team_manager(team_manager)
            # 注册 TeamCreate / TeamDelete / SendMessage 工具。
            # TeamCreate/TeamDelete 的 parent_agent 在 _run_turn 中刷新为当前回合 Agent；
            # SendMessage 占位传空字符串，teammate 由 build_teammate_tools 重新实例化。
            self._tool_registry.register(
                TeamCreateTool(None, team_manager, self._teams_config)
            )
            self._tool_registry.register(
                TeamDeleteTool(None, team_manager)
            )
            self._tool_registry.register(
                SendMessageTool(team_manager, "", "", "")
            )
        except Exception:
            # 工具注册失败不撤销 team_manager；Lead 仍可消费邮箱，只是无法新建团队。
            pass
        # 启动 TeammateTree 周期刷新 task；on_unmount 时取消。
        if self._teammate_refresh_task is None:
            try:
                self._teammate_refresh_task = asyncio.create_task(
                    self._refresh_teammate_tree()
                )
            except RuntimeError:
                # 事件循环尚未启动（如测试环境）；on_mount 会再次尝试。
                self._teammate_refresh_task = None

    # batch14：每秒拉取所有团队成员 progress 并刷新 TeammateTree。
    # team_manager / teammate_tree 为 None 时静默跳过；异常只记 warning 不中断循环。
    # 检测到非空 teammates 时显示 widget，空时隐藏，避免占用 TUI 空间。
    async def _refresh_teammate_tree(self) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                if self.team_manager is None or self.teammate_tree is None:
                    continue
                try:
                    progress = self.team_manager.get_all_teammate_progress()
                    self.teammate_tree.teammates = progress
                    # lead token 累计取当前回合 Agent；_agent 为 None 时用 0。
                    lead_tokens = 0
                    if self._agent is not None:
                        lead_tokens = (
                            getattr(self._agent, "total_input_tokens", 0)
                            + getattr(self._agent, "total_output_tokens", 0)
                        )
                    self.teammate_tree.leader_tokens = lead_tokens
                    # 有 teammates 时显示 widget，空时隐藏。
                    self.teammate_tree.display = bool(progress)
                except Exception as e:
                    log.warning("refresh teammate tree failed: %s", e)
        except asyncio.CancelledError:
            # on_unmount 取消时静默退出。
            return

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
            mode=self._initial_permission_mode,
            sandbox_enabled=checker_sandbox_enabled,
        )
        self._permission_mode = self._initial_permission_mode

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
            permission_checker=self._permission_checker,
            mcp_manager=self._mcp_manager,
            config={
                "registry": self._command_registry,
                "set_session": self._set_session,
                "set_conversation": self._set_conversation,
                "clear_chat": self._clear_chat,
                "render_restored": lambda msgs: asyncio.create_task(
                    self._render_restored_messages(msgs)
                ),
                # batch10：/skill reload 通过这两个回调触发斜杠命令重注册与 catalog 刷新。
                "register_skill_commands": self._make_skill_register_callback(),
                "build_skill_catalog": self._build_skill_catalog_text,
            },
        )

    # batch10：拼装 Skill 目录摘要文本，注入环境上下文让模型感知可用 Skill。
    # 空 catalog 返回空字符串，避免注入空段落。
    def _build_skill_catalog_text(self) -> str:
        catalog = self._skill_loader.get_catalog()
        if not catalog:
            return ""
        lines = ["You can use the following Skills:"]
        for name, desc in sorted(catalog):
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    # batch12：拼装子 Agent 目录摘要文本，注入环境上下文让模型感知可用子 Agent。
    # 含 ## Available Sub-Agent Types 标题、可用列表与"不要 wait/sleep/poll"提醒；
    # enable_fork 时追加 fork 子 Agent 条目；空 loader 返回空字符串。
    def _build_agent_catalog_text(self) -> str:
        if self.agent_loader is None:
            return ""
        agents = self.agent_loader.list_agents()
        if not agents and not self._enable_fork:
            return ""
        lines = ["## Available Sub-Agent Types"]
        for agent_type, when_to_use in agents:
            lines.append(f"- {agent_type}: {when_to_use}")
        if self._enable_fork:
            lines.append(
                "- fork: 继承父对话历史的 fork 子 Agent（默认后台执行）"
            )
        lines.append("")
        lines.append(
            "注意：后台任务完成后会自动通知主对话，不要 wait/sleep/poll。"
        )
        return "\n".join(lines)

    # batch10：返回 register_skill_commands 的闭包回调。
    # InstallSkill 安装成功与 /skill reload 时调用，重新注册所有 Skill 为斜杠命令。
    def _make_skill_register_callback(self) -> Callable[[], None]:
        return make_skill_register_callback(
            self._command_registry, self._skill_loader, self._skill_executor
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

    # UIController: 切换 Plan 模式；同步权限状态保持一致。
    def set_plan_mode(self, enabled: bool) -> None:
        new_mode = PermissionMode.PLAN if enabled else self._pre_plan_mode
        self.set_permission_mode(new_mode)

    # UIController: 统一切换权限模式，保持状态栏、检查器与当前 Agent 一致。
    def set_permission_mode(self, mode: PermissionMode) -> None:
        if mode == PermissionMode.PLAN and self._permission_mode != PermissionMode.PLAN:
            self._pre_plan_mode = self._permission_mode or PermissionMode.DEFAULT
        self._permission_mode = mode
        if self._permission_checker is not None:
            self._permission_checker.mode = mode
        if self._agent is not None:
            self._agent.set_permission_mode(mode)
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
    # batch12：若有前台运行中的子 Agent 任务，先尝试 adopt_running 切换为后台；
    # 否则走原有 Agent Loop 取消路径。
    async def action_cancel(self) -> None:
        if self._pending_permission is not None:
            return
        # 前台子 Agent 任务切换为后台；adopt_running 返回 task_id 后清空引用。
        if (
            self._subagent_task is not None
            and not self._subagent_task.done()
            and self.task_manager is not None
        ):
            try:
                # adopt_running 接收 agent 引用；_subagent_task 这里持 asyncio.Task，
                # 取其内部 agent（通过 getattr 兼容包装）。
                sub_agent = getattr(self._subagent_task, "_sub_agent", None)
                if sub_agent is not None:
                    task_id = await self.task_manager.adopt_running(
                        sub_agent,
                        task_description="background task",
                        partial_result=getattr(sub_agent, "last_output", "") or "",
                        name="foreground-to-background",
                    )
                    await self._show_system_message(
                        f"Task moved to background (id: {task_id})"
                    )
                    self._subagent_task = None
                    return
            except Exception:
                # adopt_running 失败时回退到原有取消路径。
                pass
        if self._streaming and self._agent_task is not None:
            self._agent_task.cancel()

    # batch12：判断工具名是否为子 Agent 调度工具（Agent）。
    # 用于 ToolUseEvent 分发到 SubAgentBlock 渲染路径。
    def _is_subagent_tool(self, tool_name: str) -> bool:
        return tool_name == "Agent"

    # ctrl+o 切换当前回合 ToolGroupSummary 的展开/折叠，并同步隐藏工具块的显示。
    # batch12：同时切换 SubAgentBlock 的展开/折叠。
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
        # SubAgentBlock 的展开/折叠独立切换；loading 态不响应。
        for sub_block in self.query(SubAgentBlock):
            if not sub_block._loading:
                sub_block.on_click()

    # Shift+Tab 循环切换权限模式：default → acceptEdits → plan → YOLO → default。
    def action_cycle_mode(self) -> None:
        if self._permission_mode is None:
            return
        current_idx = _MODE_CYCLE.index(self._permission_mode)
        next_mode = _MODE_CYCLE[(current_idx + 1) % len(_MODE_CYCLE)]
        self.set_permission_mode(next_mode)

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

    # batch12：AskUser 表单提交回复：回填 future 让 AskUserTool.execute 继续，
    # 移除表单组件并恢复 #chat-input 焦点。
    async def on_inline_ask_user_widget_responded(
        self, event: InlineAskUserWidget.Responded
    ) -> None:
        # 回填 future 让 AskUserTool.execute 继续；answers 为 None 时回填空 dict。
        if (
            self._ask_user_tool is not None
            and self._ask_user_tool._pending_event is not None
            and not self._ask_user_tool._pending_event.future.done()
        ):
            self._ask_user_tool._pending_event.future.set_result(
                event.answers if event.answers else {}
            )
        try:
            widget = self.query_one("#askuser-content")
            if widget is not None:
                # 找到父 InlineAskUserWidget 并移除。
                parent = widget.parent
                if parent is not None:
                    await parent.remove()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self.query_one(ChatInput).focus()
        except Exception:
            pass

    # 在计划完成后挂载审批组件，并阻止输入与审批选择竞争焦点。
    async def _show_plan_approval(self) -> None:
        self._plan_approval_active = True
        chat = self.query_one("#chat-area", VerticalScroll)
        await chat.mount(InlinePlanWidget())
        self.call_after_refresh(chat.scroll_end, animate=False)
        try:
            self.query_one(ChatInput).disabled = True
        except Exception:
            pass

    # 处理计划审批选择：恢复权限、保留反馈上下文或进入自动确认执行。
    async def on_inline_plan_widget_responded(
        self, event: InlinePlanWidget.Responded
    ) -> None:
        self._plan_approval_active = False
        try:
            widget = self.query_one("#plan-inline", InlinePlanWidget)
            await widget.remove()
        except Exception:
            pass
        try:
            input_widget = self.query_one(ChatInput)
            input_widget.disabled = self._client is None
            if not input_widget.disabled:
                input_widget.focus()
        except Exception:
            pass

        agent = self._agent
        if agent is None:
            return

        plan_path = agent._get_plan_path()
        plan_exists = plan_path.exists()
        plan_content = ""
        if plan_exists:
            try:
                plan_content = plan_path.read_text(encoding="utf-8")
            except Exception:
                pass

        if event.choice == PlanChoice.FEEDBACK:
            if event.feedback:
                self.send_user_message(event.feedback)
            else:
                await self._show_system_message("Type your feedback and send.")
            return

        if event.choice == PlanChoice.YOLO:
            self.set_permission_mode(PermissionMode.BYPASS)
        else:
            self.set_permission_mode(self._pre_plan_mode)

        # 标记已退出 Plan Mode，供下次 /plan 重入时注入 reentry reminder。
        self._has_exited_plan_mode = True
        execute_text = (
            build_plan_mode_exit_reminder(str(plan_path), plan_exists)
            + "\n\nUser has approved your plan. You can now start coding."
        )
        if plan_content:
            execute_text += "\n\nApproved Plan:\n" + plan_content
        self.send_user_message(execute_text)

    # 为当前回合预取相关记忆；使用独立客户端避免与主流式请求竞争。
    async def _prefetch_relevant_memories(self, query: str) -> str:
        if self._memory_manager is None or self._selected_provider is None:
            return ""

        try:
            side_client = self._client_factory(self._selected_provider)
            # 工厂若复用主客户端则跳过本次召回，避免并发流抢占主对话。
            if side_client is self._client:
                return ""

            async def selector(system_prompt: str, user_message: str) -> str:
                messages = [Message(role="user", content=user_message)]
                collected = ""
                async for event in side_client.stream(messages, system=system_prompt):
                    if isinstance(event, TextDelta):
                        collected += event.text
                return collected

            memories = await asyncio.wait_for(
                find_relevant_memories(
                    query=query,
                    user_mem_dir=self._memory_manager.user_mem_dir,
                    project_mem_dir=self._memory_manager.project_mem_dir,
                    recent_tools=None,
                    already_surfaced=None,
                    selector=selector,
                ),
                timeout=8.0,
            )
            return render_reminder(memories)
        except Exception:
            return ""

    # 执行一条完整 Agent Loop 回合，消费 AgentEvent 流并管理 TUI 展示与取消。
    async def _run_turn(self, text: str) -> None:
        client = self._client
        provider = self._selected_provider
        if client is None or provider is None:
            return

        self._plan_exit_requested = False
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
        live_answer: Static | None = Static("", classes="message assistant-message")
        # 初始 live_answer 一定非 None，mount 后才允许后续置 None。
        assert live_answer is not None
        await ai_row.mount(live_answer)
        started = time.monotonic()
        self._thinking_start = started
        self._thinking_verb = random.choice(THINKING_VERBS)
        answer = ""
        # batch12：tool_blocks 同时容纳 ToolCallBlock 与 SubAgentBlock；
        # SubAgentBlock 用于 "Agent" 工具调用，ToolCallBlock 用于其它工具。
        tool_blocks: dict[str, ToolCallBlock | SubAgentBlock] = {}
        total_input = 0
        total_output = 0

        try:
            # batch13：在创建 Agent 前 await restore_session 结果。
            # 若恢复了中断的 worktree session，把 Agent 的 work_dir 切换到 worktree 路径，
            # 让工具调用直接在隔离工作区执行；未恢复时使用当前工作目录。
            restored_work_dir: str | None = None
            if self._restore_session_task is not None:
                try:
                    restored = await self._restore_session_task
                    if restored is not None:
                        restored_work_dir = restored.worktree_path
                except Exception:
                    # restore_session 失败不阻断主循环；按当前工作目录继续。
                    pass
                self._restore_session_task = None
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
                # batch11：注入 HookEngine；Agent.run 在 8 个注入点触发对应生命周期事件。
                hook_engine=self._hook_engine,
            )
            # Agent 每回合重建，恢复快照和压缩状态必须复用当前应用会话对象。
            agent.recovery_state = self._recovery_state
            agent.compact_breaker = self._compact_breaker
            agent.replacement_state = self._replacement_state
            agent.active_skills = self._active_skills
            if (
                self._permission_checker is not None
                and self._permission_checker.plan_file_path
            ):
                agent._plan_path_cache = Path(self._permission_checker.plan_file_path)
            # 保存当前回合 Agent 引用，供命令路径（/status /compact /plan 等）访问。
            self._agent = agent
            # batch13：注入 FileHistory 与恢复的 worktree work_dir。
            # file_history 为 None 时 Agent.run 跳过 make_snapshot（向后兼容）。
            if self.file_history is not None:
                agent.file_history = self.file_history
            if restored_work_dir is not None:
                agent.work_dir = restored_work_dir
            # batch10：刷新 Skill 系统对当前回合 Agent 的引用。
            # executor 持有 agent 供 inline/fork 执行；load_skill 调 agent.activate_skill。
            # skill_catalog 注入环境上下文，让模型知道可用 Skill。
            self._skill_executor._agent = agent
            self._load_skill_tool.set_agent(agent)
            agent.set_skill_loader(self._skill_loader)
            agent.set_skill_catalog(self._build_skill_catalog_text())
            # batch12：注入子 Agent 目录摘要与完整工具注册表引用。
            # set_agent_catalog 让 agent.run 把 ## Available Sub-Agent Types 段落注入环境上下文；
            # set_full_registry 让 AgentTool 在 fork/定义式路径克隆或过滤父 Agent 工具集。
            # AgentTool.parent_agent 也在此刷新为当前回合 Agent（execute 时仍由 agent 传入覆盖）。
            if self.agent_loader is not None:
                agent.set_agent_catalog(
                    self._build_agent_catalog_text(),
                    self.agent_loader.list_agents(),
                )
            agent.set_full_registry(self._tool_registry)
            if self._agent_tool is not None:
                self._agent_tool.parent_agent = agent
            # batch14：注入 TeamManager 与 notification_fn 到当前回合 Lead Agent。
            # notification_fn 按团队保存的 Lead 标识消费未读消息，
            # 每轮拼成 <team-notification> XML 注入对话历史。
            # team_manager 为 None 时（装配失败或未启用）静默跳过，向后兼容 batch01-13。
            if self.team_manager is not None:
                agent._team_manager = self.team_manager
                agent.notification_fn = (
                    lambda: self.team_manager.drain_lead_mailbox()
                    if self.team_manager is not None
                    else []
                )
                # 同步刷新 TeamCreate/TeamDelete/SendMessage 工具的 parent_agent 引用。
                # 每回合重建 Agent，故需刷新引用避免访问过期 Agent 的 agent_id。
                for tool in self._tool_registry.list_tools():
                    if hasattr(tool, "_parent_agent"):
                        tool._parent_agent = agent
            # 同步当前会话 ID 给 Agent，仅用于压缩摘要里的 transcript_path 提示。
            if self._session is not None:
                agent.session_id = self._session.session_id
            if text:
                agent.memory_recall_task = asyncio.create_task(
                    self._prefetch_relevant_memories(text)
                )
                agent._memory_recall_consumed = False
            # 持久化游标：记录已写入 JSONL 的历史末尾位置。
            # TurnComplete 增量追加新消息；CompactNotification 先写 boundary 再推进游标。
            history_cursor = len(self._conversation.messages)
            # 在聊天区底部 mount 持续旋转的 spinner，thinking 期间显示 ⠋ verb… (Ns)。
            self._spinner_idx = 0
            self._spinner_label = Static(
                f"  {SPINNER_FRAMES[0]} {self._thinking_verb}…",
                id="spinner-live",
            )
            await chat.mount(self._spinner_label)
            self._start_spinner()
            self.call_after_refresh(chat.scroll_end, animate=False)
            await asyncio.sleep(0)
            async for event in agent.run(self._conversation):
                if isinstance(event, StreamText):
                    # 首个 StreamText 到达时重建 live_answer，确保样式干净。
                    if live_answer is not None and not answer:
                        await live_answer.remove()
                        live_answer = Static("", classes="message assistant-message")
                        await ai_row.mount(live_answer)
                    answer += event.text
                    live_text = Text()
                    live_text.append("● ", style="bold #d9a441")
                    live_text.append(answer)
                    if live_answer is not None:
                        live_answer.update(live_text)
                    chat.scroll_end(animate=False)
                elif isinstance(event, ThinkingText):
                    # thinking 内容不在 TUI 显示，只滚动到底部让 spinner 保持可见。
                    self.call_after_refresh(chat.scroll_end, animate=False)
                elif isinstance(event, ToolUseEvent):
                    # 工具调用前把已累积的流式文本转为 Markdown 持久化到当前 ai_row，
                    # 避免 live_answer 残留旧文本导致后续回合答案串接或丢失。
                    if answer:
                        if live_answer is not None:
                            await live_answer.remove()
                        prefix = Static(Text("●  ", style="bold #d9a441"), classes="message")
                        await ai_row.mount(prefix)
                        await ai_row.mount(
                            Markdown(answer, classes="message assistant-markdown")
                        )
                        live_answer = None
                        answer = ""
                    elif live_answer is not None:
                        await live_answer.remove()
                        live_answer = None
                    # batch12：Agent 工具调用用 SubAgentBlock 呈现；其它工具用 ToolCallBlock。
                    if self._is_subagent_tool(event.tool_name):
                        agent_type = event.arguments.get("subagent_type", "") or "agent"
                        description = event.arguments.get("description", "")
                        block: ToolCallBlock | SubAgentBlock = SubAgentBlock(
                            agent_type=agent_type, description=description
                        )
                    else:
                        block = ToolCallBlock(event.tool_name, event.arguments)
                    tool_blocks[event.tool_id] = block
                    await ai_row.mount(block)
                    chat.scroll_end(animate=False)
                elif isinstance(event, ToolResultEvent):
                    if event.tool_name == "ExitPlanMode" and not event.is_error:
                        self._plan_exit_requested = True
                    result_block = tool_blocks.get(event.tool_id)
                    if result_block is not None:
                        # batch12：SubAgentBlock 与 ToolCallBlock 接口不同，按类型分发。
                        if isinstance(result_block, SubAgentBlock):
                            result_block.set_result(
                                output=event.output,
                                is_error=event.is_error,
                                elapsed=event.elapsed,
                            )
                        else:
                            result_block.set_result(
                                ToolResult(
                                    content=event.output, is_error=event.is_error
                                ),
                                event.elapsed,
                            )
                    chat.scroll_end(animate=False)
                    # batch12：AskUserTool 执行后检查 _pending_event 挂起 InlineAskUserWidget。
                    if (
                        self._ask_user_tool is not None
                        and self._ask_user_tool._pending_event is not None
                    ):
                        widget: InlineAskUserWidget | InlinePermissionWidget = InlineAskUserWidget(
                            self._ask_user_tool._pending_event.questions,
                        )
                        await chat.mount(widget)
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
                    # 确认组件高度需在本次刷新后才能计算，随后再滚到底部。
                    self.call_after_refresh(chat.scroll_end, animate=False)
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
                elif isinstance(event, HookEvent):
                    # batch11：Hook 执行结果在对话区呈现状态行，与 CompactNotification 同层级。
                    status = "OK" if event.success else "FAIL"
                    output = event.output
                    if len(output) > 200:
                        output = output[:200] + "…"
                    await self._show_system_message(
                        f"Hook [{event.hook_id}] {status} {output}"
                    )
                    chat.scroll_end(animate=False)
                elif isinstance(event, ErrorEvent):
                    await self._append_error(event.message)
                elif isinstance(event, TurnComplete):
                    # 可折叠工具 >=2 个时 mount 摘要并隐藏工具块。
                    # batch12：只对 ToolCallBlock 应用折叠逻辑；SubAgentBlock 不参与。
                    collapsible = [
                        (tid, blk)
                        for tid, blk in tool_blocks.items()
                        if isinstance(blk, ToolCallBlock)
                        and blk.tool_name in COLLAPSIBLE_TOOLS
                        and not blk._loading
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
                    live_answer = Static("", classes="message assistant-message")
                    await ai_row.mount(live_answer)
                    answer = ""
                    chat.scroll_end(animate=False)
                elif isinstance(event, LoopComplete):
                    total_time = time.monotonic() - started
                    done_label = Static(
                        f"✻ {_to_past_tense(self._thinking_verb)} for {total_time:.1f}s",
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
                    if self._plan_exit_requested:
                        self._plan_exit_requested = False
                        await self._show_plan_approval()

            # 收尾：把剩余的累积文本转为 Markdown 持久化到当前 ai_row。
            # live_answer 为 None 时（工具调用后已转 Markdown）不再处理。
            if answer and live_answer is not None:
                await live_answer.remove()
                await ai_row.mount(
                    Markdown(answer, classes="message assistant-markdown")
                )
            elif live_answer is not None:
                await live_answer.remove()
            elapsed = time.monotonic() - started
            self._set_status(
                f"Ready  {elapsed:.1f}s  in {total_input} / out {total_output}"
            )
        except asyncio.CancelledError:
            # 保留已累积的流式文本并追加 [cancelled] 标记。
            if answer:
                if live_answer is not None:
                    await live_answer.remove()
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
            # 停止 spinner 动画并移除 label，无论回合成功/失败/取消。
            self._stop_spinner()
            if self._spinner_label is not None:
                try:
                    self._spinner_label.remove()
                except Exception:
                    pass
                self._spinner_label = None
            self._streaming = False
            self._agent_task = None
            input_widget.disabled = self._client is None or self._plan_approval_active
            if not input_widget.disabled:
                input_widget.focus()

    # 启动 braille spinner 动画（每帧 80ms），thinking 期间持续旋转。
    def _start_spinner(self) -> None:
        if self._spinner_timer is not None:
            return
        self._spinner_timer = self.set_interval(0.08, self._tick_spinner)

    # 停止 spinner 动画。
    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    # 推进 spinner 标签上的动画帧，每 5 帧滚动一次聊天区。
    def _tick_spinner(self) -> None:
        self._spinner_idx += 1
        frame = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
        elapsed = time.monotonic() - self._thinking_start
        if self._spinner_label is not None:
            self._spinner_label.update(
                f"  {frame} {self._thinking_verb}…  ({elapsed:.0f}s)"
            )
            if self._spinner_idx % 5 == 0:
                try:
                    self.query_one("#chat-area", VerticalScroll).scroll_end(
                        animate=False
                    )
                except Exception:
                    pass

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
    # batch11：触发 shutdown 事件；用 ensure_future 后台执行，避免 on_unmount
    # 等待 Hook 完成（shutdown Hook 异常由 _run_single 兜底捕获记 warning）。
    # batch12：取消后台任务通知轮询协程，避免退出后仍尝试访问已关闭的对话历史。
    # batch13：取消 stale worktree 清理 task 与 restore_session task，避免退出后残留。
    # batch14：取消 TeammateTree 周期刷新 task，避免退出后仍访问已销毁的 widget。
    def on_unmount(self) -> None:
        if self._notification_polling_task is not None:
            self._notification_polling_task.cancel()
            self._notification_polling_task = None
        if self._stale_cleanup_task is not None:
            self._stale_cleanup_task.cancel()
            self._stale_cleanup_task = None
        if self._restore_session_task is not None:
            self._restore_session_task.cancel()
            self._restore_session_task = None
        if self._teammate_refresh_task is not None:
            self._teammate_refresh_task.cancel()
            self._teammate_refresh_task = None
        if self._hook_engine is not None:
            try:
                asyncio.ensure_future(self._trigger_shutdown_hooks())
            except RuntimeError:
                # 事件循环已关闭时静默跳过；shutdown Hook 仍可在循环关闭前执行。
                pass
        if self._session is not None:
            self._session.close()

    # batch11：触发 shutdown Hook；只 await 完成，渲染通知由 drain 后异步处理。
    async def _trigger_shutdown_hooks(self) -> None:
        if self._hook_engine is None:
            return
        try:
            await self._hook_engine.run_hooks(
                "shutdown", HookContext(event_name="shutdown")
            )
        except Exception:
            # shutdown Hook 异常不阻断退出流程。
            pass

    # batch11：把 HookNotification 渲染到对话区作为状态行。
    # 与 CompactNotification / MCPConnectEvent 呈现层级一致，不重排已有 TUI 布局。
    async def _render_hook_notification(self, notification: Any) -> None:
        status = "OK" if notification.success else "FAIL"
        output = notification.output
        if len(output) > 200:
            output = output[:200] + "…"
        line = f"Hook [{notification.hook_id}] {status} {output}"
        await self._show_system_message(line)

    # -----------------------------------------------------------------
    # batch12：子 Agent 后台任务通知轮询
    # -----------------------------------------------------------------

    # 后台任务通知轮询循环：每 2 秒检查一次是否有已完成的后台任务。
    # 流式回合期间跳过避免与活动 Agent Loop 竞争对话历史；
    # 检测到完成任务时注入 <task-notification> 并触发新一轮 Agent Loop。
    async def _start_notification_polling(self) -> None:
        try:
            while True:
                await asyncio.sleep(2)
                # 流式回合期间不处理通知，避免与活动 Agent Loop 竞争对话历史。
                if self._streaming:
                    continue
                if self.task_manager is None:
                    continue
                await self._process_task_notifications()
        except asyncio.CancelledError:
            # on_unmount 取消时静默退出。
            return

    # 处理已完成的后台任务：注入 <task-notification> 到主对话，
    # 渲染状态行提示用户，并触发新一轮 Agent Loop 让模型基于通知回复。
    async def _process_task_notifications(self) -> None:
        if self.task_manager is None:
            return
        completed = self.task_manager.poll_completed()
        if not completed:
            return
        # 把 <task-notification> XML 块以 user message 注入主对话。
        inject_task_notifications(self._conversation, completed)
        # 在对话区渲染状态行提示用户后台任务完成。
        for task in completed:
            icon = "✓" if task.status == "completed" else "✗"
            await self._show_system_message(
                f"{icon} 后台任务完成: [{task.id}] {task.name} — {task.status}"
            )
        # 触发新一轮 Agent Loop 让模型基于通知回复；
        # 用空 user message 占位（实际通知已注入到对话历史）。
        if self._client is not None and not self._streaming:
            self._streaming = True
            self._agent_task = asyncio.create_task(self._run_turn(""))
