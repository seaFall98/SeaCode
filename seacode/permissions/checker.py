"""权限检查器：协调规则引擎、危险命令检测、路径沙箱与模式矩阵，编排五层防御链。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from seacode.permissions.dangerous import DangerousCommandDetector, is_safe_command
from seacode.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from seacode.permissions.rules import RuleEngine, extract_content
from seacode.permissions.sandbox import PathSandbox
from seacode.tools.base import Tool

# Plan 模式例外放行的工具白名单；本步六个核心工具不在此列，自然进入后续层判定。
_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion", "ExitPlanMode"})


@dataclass
class Decision:
    """单次权限决策结果：效果 + 原因。"""

    effect: DecisionEffect
    reason: str


class PermissionChecker:
    """权限检查器：依赖注入 detector / sandbox / rule_engine / mode / sandbox_enabled。

    check() 串联五层防御链：
      Layer 0  Plan 例外放行
      Layer 1  安全命令白名单自动放行
      Layer 1b 危险命令黑名单硬拦截（不可被任何模式绕过）
      Layer 1c OS 沙箱启用时命令类工具自动放行（拆分复合命令逐条查规则）
      Layer 2  路径沙箱拦截文件类工具的越权访问（BYPASS 模式放行）
      Layer 3  规则引擎三层优先级匹配
      Layer 4b 会话级放行集合（内存中，优先于模式兜底）
      Layer 4  模式矩阵兜底判定
      Layer 5  触发人工确认（HITL ask）
    """

    def __init__(
        self,
        detector: DangerousCommandDetector,
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        mode: PermissionMode = PermissionMode.DEFAULT,
        sandbox_enabled: bool = False,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        # Plan 文件路径；第 04 步 prompt-pipeline 负责填充，本步默认空串 fallback 到路径包含检查。
        self.plan_file_path: str = ""
        # OS 级沙箱是否启用（开启后命令类工具可自动放行，因为内核会兜底）。
        self.sandbox_enabled = sandbox_enabled
        # Layer 4b: 会话级 allow-always 集合（内存中，不持久化）。
        # 存放格式为 "ToolName:content"，用户选择 "don't ask again" 时记录。
        self._session_allowed: set[str] = set()

    # 将工具+内容模式加入会话级放行集合（Layer 4b）；会话结束即消失。
    def add_session_allow(self, tool_name: str, content: str) -> None:
        key = f"{tool_name}:{content}"
        self._session_allowed.add(key)

    # 检查是否匹配会话级放行记录；支持精确匹配与前缀匹配（pattern 带 * 尾缀）。
    def _check_session_allowed(self, tool_name: str, content: str) -> bool:
        if not self._session_allowed:
            return False
        key = f"{tool_name}:{content}"
        if key in self._session_allowed:
            return True
        for allowed in self._session_allowed:
            if allowed.endswith("*") and key.startswith(allowed[:-1]):
                return True
        return False

    # 为 HITL 确认生成人类可读的操作描述；优先从标准字段提取，否则拼接参数摘要。
    @staticmethod
    def describe_tool_action(tool_name: str, arguments: dict[str, Any]) -> str:
        content = extract_content(tool_name, arguments)
        if content:
            return content
        parts: list[str] = []
        for k, v in arguments.items():
            sv = str(v)
            if len(sv) > 80:
                sv = sv[:77] + "..."
            parts.append(f"{k}={sv}")
        return ", ".join(parts) if parts else tool_name

    # 主入口：对工具调用进行权限检查，返回 Decision。
    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        content = extract_content(tool.name, arguments)

        # Layer 0: Plan 模式例外放行
        if self.mode == PermissionMode.PLAN:
            if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision(effect="allow", reason="Plan mode: allowed tool")
            if tool.name in ("WriteFile", "EditFile") and content:
                if self._is_plan_file(content):
                    return Decision(effect="allow", reason="Plan mode: plan file write")

        # Layer 1: 安全的只读命令（自动放行）
        if tool.category == "system" and is_safe_command(content or ""):
            return Decision(effect="allow", reason="Safe read-only command")

        # Layer 1b: 危险命令黑名单（仅 Bash；命中即硬拦截）
        if tool.category == "system":
            hit, reason = self.detector.detect(content)
            if hit:
                return Decision(effect="deny", reason=f"危险命令拦截: {reason}")

        # Layer 1c: OS 沙箱自动放行
        # 沙箱开启时，命令类工具通过了危险命令检查后直接放行——
        # 内核级隔离会阻止越权写入，无需再弹确认。
        # 拆分复合命令逐条检查，防止通过命令拼接绕过权限检查，
        # deny 规则和 ask 规则不受沙箱影响。
        if self.sandbox_enabled and tool.category == "system":
            subcommands = [
                s.strip()
                for s in re.split(r"\s*(?:&&|\|\||[;|])\s*", content)
                if s.strip()
            ]
            if not subcommands:
                subcommands = [content]
            has_ask = False
            for sub in subcommands:
                rule_result = self.rule_engine.evaluate(tool.name, sub)
                if rule_result == "deny":
                    return Decision(effect="deny", reason="权限规则拒绝")
                if rule_result == "ask":
                    has_ask = True
            if has_ask:
                return Decision(effect="ask", reason="权限规则要求确认")
            return Decision(effect="allow", reason="OS 沙箱自动放行")

        # Layer 2: 路径沙箱（仅文件类工具）
        if tool.category in ("read", "write") and content:
            ok, reason = self.sandbox.check(content)
            if not ok and self.mode != PermissionMode.BYPASS:
                return Decision(effect="ask", reason=f"路径沙箱拦截: {reason}")

        # Layer 3: 规则引擎匹配
        rule_result = self.rule_engine.evaluate(tool.name, content)
        if rule_result == "allow":
            return Decision(effect="allow", reason="权限规则放行")
        if rule_result == "deny":
            return Decision(effect="deny", reason="权限规则拒绝")

        # Layer 4b: 会话级放行（内存中，优先于模式兜底）
        if self._check_session_allowed(tool.name, content or ""):
            return Decision(effect="allow", reason="会话级放行（session allow-always）")

        # Layer 4: 权限模式兜底判定
        effect = mode_decide(self.mode, tool.category)
        if effect == "allow":
            return Decision(effect="allow", reason=f"权限模式 {self.mode.value} 放行")
        if effect == "deny":
            return Decision(effect="deny", reason=f"权限模式 {self.mode.value} 拒绝")

        # Layer 5: 触发人工确认（HITL）
        return Decision(effect="ask", reason="需要用户确认")

    # 判断目标路径是否为 Plan 文件；三级降级避免 plan_file_path 未填充时误判。
    def _is_plan_file(self, target_path: str) -> bool:
        if not self.plan_file_path or not target_path:
            return ".seacode/plans/" in target_path
        try:
            abs_target = os.path.abspath(target_path)
            abs_plan = os.path.abspath(self.plan_file_path)
            if abs_target == abs_plan:
                return True
        except Exception:
            pass
        if os.path.basename(target_path) == os.path.basename(self.plan_file_path):
            return True
        return ".seacode/plans/" in target_path
