# Teammate 运行时进度：工具调用计数、token 累积、最近活动与 spinner 动词。
"""teams 子包的 TeammateProgress 与 ToolActivity。"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from datetime import datetime

# 100 个英文现在进行时动词，用于 teammate 运行中态展示；与 app THINKING_VERBS 风格对齐。
SPINNER_VERBS: list[str] = [
    "analyzing", "reading", "writing", "editing", "searching",
    "scanning", "indexing", "parsing", "compiling", "validating",
    "computing", "calculating", "estimating", "measuring", "comparing",
    "matching", "filtering", "sorting", "grouping", "merging",
    "splitting", "joining", "transforming", "converting", "formatting",
    "rendering", "drawing", "painting", "sketching", "drafting",
    "composing", "assembling", "building", "constructing", "fabricating",
    "engineering", "architecting", "designing", "modeling", "simulating",
    "testing", "verifying", "checking", "auditing", "inspecting",
    "examining", "investigating", "exploring", "discovering", "finding",
    "locating", "identifying", "recognizing", "detecting", "sensing",
    "listening", "watching", "observing", "monitoring", "tracking",
    "tracing", "following", "chasing", "pursuing", "hunting",
    "gathering", "collecting", "harvesting", "mining", "extracting",
    "deriving", "inferring", "deducing", "reasoning", "thinking",
    "pondering", "reflecting", "contemplating", "planning", "scheduling",
    "coordinating", "orchestrating", "managing", "directing", "guiding",
    "leading", "supporting", "assisting", "helping", "aiding",
    "fixing", "repairing", "patching", "mending", "correcting",
    "adjusting", "tuning", "optimizing", "refining", "polishing",
    "preparing", "arranging", "organizing", "structuring", "formatting",
]


# 从 SPINNER_VERBS 随机选一个动词；用于 teammate 运行中态展示。
def random_verb() -> str:
    return random.choice(SPINNER_VERBS)


@dataclass
class ToolActivity:
    # 单次工具调用的运行时记录；timestamp 由调用方在创建时钉死。
    tool_name: str
    args: dict
    timestamp: datetime

    # 按工具类型生成简短描述；Bash 截断到 40 字符避免状态行过长。
    def _describe(self) -> str:
        if self.tool_name in ("ReadFile", "EditFile", "WriteFile"):
            path = self.args.get("file_path", self.args.get("path", ""))
            return f"{self.tool_name} {path}"
        if self.tool_name == "Bash":
            command = self.args.get("command", "")
            return f"Bash {command[:40]}"
        if self.tool_name == "Glob":
            pattern = self.args.get("pattern", "")
            return f"Glob {pattern}"
        if self.tool_name == "Grep":
            pattern = self.args.get("pattern", "")
            return f"Grep {pattern}"
        return self.tool_name


@dataclass
class TeammateProgress:
    # 单个 teammate 的运行时进度；_lock 保护并发更新。
    name: str
    team_name: str
    status: str = "running"
    tool_use_count: int = 0
    token_count: int = 0
    last_activity: str = ""
    recent_activities: list[ToolActivity] = field(default_factory=list)
    spinner_verb: str = ""
    start_time: datetime = field(default_factory=datetime.now)
    last_message: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # 记录工具调用：计数 +1、追加 activity（最多 5 条）、刷新 last_activity 与 spinner_verb。
    def record_tool_use(self, tool_name: str, args: dict) -> None:
        with self._lock:
            self.tool_use_count += 1
            activity = ToolActivity(
                tool_name=tool_name, args=args, timestamp=datetime.now()
            )
            self.recent_activities.append(activity)
            if len(self.recent_activities) > 5:
                # 只保留最近 5 条，避免无限增长。
                self.recent_activities = self.recent_activities[-5:]
            self.last_activity = activity._describe()
            self.spinner_verb = random_verb()

    # 累加 token 用量；由流式事件 usage 回调触发。
    def record_tokens(self, tokens: int) -> None:
        with self._lock:
            self.token_count += tokens

    # 返回当前活动摘要：有 last_activity 用它，否则用 spinner_verb。
    def activity_summary(self) -> str:
        with self._lock:
            return self.last_activity or self.spinner_verb

    # 格式化 token 数；1M+ 用 M、1k+ 用 k、否则原样。
    def format_tokens(self) -> str:
        with self._lock:
            t = self.token_count
        if t >= 1_000_000:
            return f"{t / 1_000_000:.1f}M"
        if t >= 1_000:
            return f"{t / 1_000:.1f}k"
        return str(t)
