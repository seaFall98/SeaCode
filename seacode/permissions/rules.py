"""权限规则引擎：三层 YAML 规则文件 + fnmatch 模式匹配。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml

Effect = Literal["allow", "deny", "ask"]

# 规则语法：ToolName(pattern)，捕获工具名与 glob 模式。
_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")

# 各工具提取内容的字段映射；规则匹配与 describe_tool_action 都依赖此映射。
_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


@dataclass(frozen=True)
class Rule:
    """单条权限规则：工具名 + glob 模式 + 效果。"""

    tool_name: str
    pattern: str
    effect: Effect

    # 工具名严格相等，内容按 fnmatch glob 匹配。
    def matches(self, tool_name: str, content: str) -> bool:
        if self.tool_name != tool_name:
            return False
        return fnmatch(content, self.pattern)


def parse_rule(raw: str, effect: Effect) -> Rule:
    """解析 "ToolName(pattern)" 字符串为 Rule；语法不合法抛 ValueError。"""
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无效的规则语法: {raw}")
    return Rule(tool_name=m.group(1), pattern=m.group(2), effect=effect)


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    """从工具参数中提取规则匹配所用的内容字段；未映射工具返回空串。"""
    field = _CONTENT_FIELDS.get(tool_name)
    if field is None:
        return ""
    return str(arguments.get(field, ""))


def _load_rules_file(path: Path) -> list[Rule]:
    """加载单个 YAML 规则文件；文件不存在、解析失败或格式不对静默跳过。"""
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_str = entry.get("rule", "")
        effect = entry.get("effect", "")
        if effect not in ("allow", "deny", "ask"):
            continue
        try:
            rules.append(parse_rule(rule_str, effect))
        except ValueError:
            continue
    return rules


class RuleEngine:
    """三层规则文件（用户级 > 项目级 > 本地级）的优先级匹配引擎。

    每次 evaluate 都重新读文件，无缓存；append_local_rule 写入后下次立即生效。
    每层内按 reversed 顺序匹配，后定义的规则优先。
    """

    def __init__(
        self,
        user_rules_path: Path | None = None,
        project_rules_path: Path | None = None,
        local_rules_path: Path | None = None,
    ) -> None:
        self._user_path = user_rules_path
        self._project_path = project_rules_path
        self._local_path = local_rules_path

    # 返回三层规则列表，顺序为 [user, project, local]；路径为 None 时该层为空。
    def _load_tiers(self) -> list[list[Rule]]:
        tiers: list[list[Rule]] = []
        for p in (self._user_path, self._project_path, self._local_path):
            tiers.append(_load_rules_file(p) if p else [])
        return tiers

    # 按三层优先级匹配；user > project > local，每层内 reversed 后定义优先。
    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        for rules in self._load_tiers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return None

    # 将规则追加写入本地规则文件；local_path 为 None 时直接返回。
    def append_local_rule(self, rule: Rule) -> None:
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_rules_file(self._local_path)
        existing.append(rule)
        entries = [
            {"rule": f"{r.tool_name}({r.pattern})", "effect": r.effect}
            for r in existing
        ]
        self._local_path.write_text(
            yaml.dump(entries, allow_unicode=True), encoding="utf-8"
        )
