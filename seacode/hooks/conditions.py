"""条件解析与匹配：支持 ==/!=/=~/~= 四操作符与 &&/|| 组合（不可混用）。"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from seacode.hooks.models import HookContext

# 四操作符按解析优先级排列；解析时取首个命中的操作符。
_OPERATORS: tuple[str, ...] = ("==", "!=", "=~", "~=")


@dataclass
class Condition:
    """单个条件谓词；field 通过 HookContext.get_field 取值后与 value 比较。"""

    field: str
    operator: str
    value: str

    # == 精确相等、!= 不等、=~ 正则匹配（re.search，支持 /pattern/ 包裹）、~= glob 匹配。
    def evaluate(self, ctx: HookContext) -> bool:
        field_value = ctx.get_field(self.field)
        if self.operator == "==":
            return field_value == self.value
        if self.operator == "!=":
            return field_value != self.value
        if self.operator == "=~":
            pattern = self.value
            # /pattern/ 包裹形式剥离首尾斜杠，便于在 YAML 中书写带特殊字符的正则。
            if pattern.startswith("/") and pattern.endswith("/") and len(pattern) >= 2:
                pattern = pattern[1:-1]
            try:
                return re.search(pattern, field_value) is not None
            except re.error:
                # 非法正则视为不匹配，避免单条 Hook 配置错误阻塞整个引擎。
                return False
        if self.operator == "~=":
            return fnmatch.fnmatch(field_value, self.value)
        return False


@dataclass
class ConditionGroup:
    """条件组合；logic=and 用 all，logic=or 用 any，空 conditions 无条件命中。"""

    conditions: list[Condition] = field(default_factory=list)
    logic: str = "and"

    def evaluate(self, ctx: HookContext) -> bool:
        if not self.conditions:
            return True
        if self.logic == "and":
            return all(c.evaluate(ctx) for c in self.conditions)
        return any(c.evaluate(ctx) for c in self.conditions)


class ConditionParseError(Exception):
    """条件表达式语法错误；混用 && 与 || 或缺操作符时抛出。"""


# 解析单个条件表达式为 Condition；缺操作符或空 field 抛 ConditionParseError。
def _parse_single(expr: str) -> Condition:
    expr = expr.strip()
    for op in _OPERATORS:
        idx = expr.find(op)
        if idx == -1:
            continue
        field_part = expr[:idx].strip()
        value_part = expr[idx + len(op):].strip()
        # 剥离单引号或双引号包裹的值。
        if len(value_part) >= 2 and value_part[0] == value_part[-1] and value_part[0] in ("'", '"'):
            value_part = value_part[1:-1]
        if not field_part:
            raise ConditionParseError(f"Empty field in condition: '{expr}'")
        return Condition(field=field_part, operator=op, value=value_part)
    raise ConditionParseError(f"No valid operator found in condition: '{expr}'")


# 解析条件表达式为 ConditionGroup；空字符串返回 None；&& 与 || 不可混用。
def parse_condition(expr: str) -> ConditionGroup | None:
    if not expr or not expr.strip():
        return None

    expr = expr.strip()
    has_and = "&&" in expr
    has_or = "||" in expr

    # 显式约束：同一条表达式不可同时使用 && 与 ||，要求拆分为多条 Hook。
    if has_and and has_or:
        raise ConditionParseError(
            "Cannot mix '&&' and '||' in a single condition expression. "
            "Split into separate hooks instead."
        )

    if has_and:
        parts = [p.strip() for p in expr.split("&&") if p.strip()]
        logic = "and"
    elif has_or:
        parts = [p.strip() for p in expr.split("||") if p.strip()]
        logic = "or"
    else:
        parts = [expr]
        logic = "and"

    return ConditionGroup(
        conditions=[_parse_single(p) for p in parts], logic=logic
    )
