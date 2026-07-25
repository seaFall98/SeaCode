"""条件解析与匹配单元测试：覆盖 Condition/ConditionGroup/parse_condition。"""

from __future__ import annotations

import pytest

from seacode.hooks.conditions import (
    Condition,
    ConditionGroup,
    ConditionParseError,
    parse_condition,
)
from seacode.hooks.models import HookContext

# ---------------------------------------------------------------------------
# Condition.evaluate 四操作符
# ---------------------------------------------------------------------------


# 验证 == 操作符精确相等时返回 True。
# 构造 field=tool、value=WriteFile 的 Condition，断言匹配返回 True。
def test_condition_evaluate_eq_match() -> None:
    cond = Condition(field="tool", operator="==", value="WriteFile")
    ctx = HookContext(tool_name="WriteFile")
    assert cond.evaluate(ctx) is True


# 验证 == 操作符不匹配时返回 False。
# tool_name 与 value 不同，断言返回 False。
def test_condition_evaluate_eq_mismatch() -> None:
    cond = Condition(field="tool", operator="==", value="WriteFile")
    ctx = HookContext(tool_name="ReadFile")
    assert cond.evaluate(ctx) is False


# 验证 != 操作符不等时返回 True。
# tool_name 与 value 不同，断言返回 True。
def test_condition_evaluate_neq_match() -> None:
    cond = Condition(field="tool", operator="!=", value="WriteFile")
    ctx = HookContext(tool_name="ReadFile")
    assert cond.evaluate(ctx) is True


# 验证 != 操作符相等时返回 False。
# tool_name 与 value 相同，断言返回 False。
def test_condition_evaluate_neq_mismatch() -> None:
    cond = Condition(field="tool", operator="!=", value="WriteFile")
    ctx = HookContext(tool_name="WriteFile")
    assert cond.evaluate(ctx) is False


# 验证 =~ 操作符正则匹配时返回 True。
# file_path 以 .json 结尾，正则 \.json$ 命中。
def test_condition_evaluate_regex_match() -> None:
    cond = Condition(field="args.file_path", operator="=~", value="\\.json$")
    ctx = HookContext(tool_args={"file_path": "a.json"})
    assert cond.evaluate(ctx) is True


# 验证 =~ 操作符正则不匹配时返回 False。
# file_path 以 .txt 结尾，正则 \.json$ 不命中。
def test_condition_evaluate_regex_mismatch() -> None:
    cond = Condition(field="args.file_path", operator="=~", value="\\.json$")
    ctx = HookContext(tool_args={"file_path": "a.txt"})
    assert cond.evaluate(ctx) is False


# 验证 =~ 的 /pattern/ 包裹形式剥离首尾斜杠后匹配。
# value 为 /\.json$/，剥离斜杠后与裸正则等价。
def test_condition_evaluate_regex_with_slash_wrap() -> None:
    cond = Condition(field="args.file_path", operator="=~", value="/\\.json$/")
    ctx = HookContext(tool_args={"file_path": "a.json"})
    assert cond.evaluate(ctx) is True


# 验证 =~ 非法正则返回 False 而非抛异常。
# value 为未闭合字符类，re.error 被捕获转为 False。
def test_condition_evaluate_regex_invalid_returns_false() -> None:
    cond = Condition(field="tool", operator="=~", value="[invalid")
    ctx = HookContext()
    assert cond.evaluate(ctx) is False


# 验证 ~= 操作符 glob 匹配时返回 True。
# file_path 为 a.json，模式 *.json 命中。
def test_condition_evaluate_glob_match() -> None:
    cond = Condition(field="args.file_path", operator="~=", value="*.json")
    ctx = HookContext(tool_args={"file_path": "a.json"})
    assert cond.evaluate(ctx) is True


# 验证 ~= 操作符 glob 不匹配时返回 False。
# file_path 为 a.txt，模式 *.json 不命中。
def test_condition_evaluate_glob_mismatch() -> None:
    cond = Condition(field="args.file_path", operator="~=", value="*.json")
    ctx = HookContext(tool_args={"file_path": "a.txt"})
    assert cond.evaluate(ctx) is False


# 验证字段不存在时返回空字符串参与比较。
# Condition 字段 missing 与 value="" 比较返回 True。
def test_condition_evaluate_missing_field_compares_empty() -> None:
    cond = Condition(field="missing", operator="==", value="")
    ctx = HookContext()
    assert cond.evaluate(ctx) is True


# ---------------------------------------------------------------------------
# ConditionGroup.evaluate
# ---------------------------------------------------------------------------


# 验证空 conditions 列表返回 True（无条件命中）。
# 构造 conditions=[] 的 ConditionGroup，断言 evaluate 返回 True。
def test_condition_group_empty_conditions_returns_true() -> None:
    cg = ConditionGroup(conditions=[], logic="and")
    assert cg.evaluate(HookContext()) is True


# 验证 logic="and" 在有 False 条件时返回 False。
# 两个 Condition 一个 True 一个 False，断言 all 行为返回 False。
def test_condition_group_and_with_false_returns_false() -> None:
    cg = ConditionGroup(
        conditions=[
            Condition(field="tool", operator="==", value="WriteFile"),
            Condition(field="tool", operator="==", value="ReadFile"),
        ],
        logic="and",
    )
    ctx = HookContext(tool_name="WriteFile")
    assert cg.evaluate(ctx) is False


# 验证 logic="and" 在全部 True 时返回 True。
# 两个 Condition 都匹配，断言 all 返回 True。
def test_condition_group_and_all_true_returns_true() -> None:
    cg = ConditionGroup(
        conditions=[
            Condition(field="tool", operator="==", value="WriteFile"),
            Condition(field="tool", operator="!=", value="ReadFile"),
        ],
        logic="and",
    )
    ctx = HookContext(tool_name="WriteFile")
    assert cg.evaluate(ctx) is True


# 验证 logic="or" 在有 True 条件时返回 True。
# 两个 Condition 一个 False 一个 True，断言 any 返回 True。
def test_condition_group_or_with_true_returns_true() -> None:
    cg = ConditionGroup(
        conditions=[
            Condition(field="tool", operator="==", value="ReadFile"),
            Condition(field="tool", operator="==", value="WriteFile"),
        ],
        logic="or",
    )
    ctx = HookContext(tool_name="WriteFile")
    assert cg.evaluate(ctx) is True


# 验证 logic="or" 在全部 False 时返回 False。
# 两个 Condition 都不匹配，断言 any 返回 False。
def test_condition_group_or_all_false_returns_false() -> None:
    cg = ConditionGroup(
        conditions=[
            Condition(field="tool", operator="==", value="ReadFile"),
            Condition(field="tool", operator="==", value="EditFile"),
        ],
        logic="or",
    )
    ctx = HookContext(tool_name="WriteFile")
    assert cg.evaluate(ctx) is False


# ---------------------------------------------------------------------------
# parse_condition
# ---------------------------------------------------------------------------


# 验证 parse_condition 对空字符串返回 None。
# parse_condition("") 不构造 ConditionGroup，直接返回 None。
def test_parse_condition_empty_string_returns_none() -> None:
    assert parse_condition("") is None


# 验证 parse_condition 对纯空白字符串返回 None。
# 仅含空格的表达式视为空，返回 None。
def test_parse_condition_whitespace_returns_none() -> None:
    assert parse_condition("   ") is None


# 验证 parse_condition 解析单条件为 logic="and" 的 ConditionGroup。
# 单条件表达式返回单个 Condition 的 and 组。
def test_parse_condition_single_condition() -> None:
    cg = parse_condition('tool == "WriteFile"')
    assert cg is not None
    assert len(cg.conditions) == 1
    assert cg.logic == "and"
    assert cg.conditions[0].field == "tool"
    assert cg.conditions[0].operator == "=="
    assert cg.conditions[0].value == "WriteFile"


# 验证 parse_condition 解析 && 组合为 logic="and" 的多条件组。
# 两个 && 连接的条件返回长度 2 的 and 组。
def test_parse_condition_and_combination() -> None:
    cg = parse_condition('tool == "WriteFile" && args.file_path =~ "\\.json$"')
    assert cg is not None
    assert len(cg.conditions) == 2
    assert cg.logic == "and"


# 验证 parse_condition 解析 || 组合为 logic="or" 的多条件组。
# 两个 || 连接的条件返回长度 2 的 or 组。
def test_parse_condition_or_combination() -> None:
    cg = parse_condition('tool == "WriteFile" || tool == "EditFile"')
    assert cg is not None
    assert len(cg.conditions) == 2
    assert cg.logic == "or"


# 验证 parse_condition 解析多个 && 组合为长度 3 的 and 组。
# 三个条件用 && 连接，断言 conditions 长度 3 且 logic="and"。
def test_parse_condition_multiple_and() -> None:
    cg = parse_condition('a == "1" && b == "2" && c == "3"')
    assert cg is not None
    assert len(cg.conditions) == 3
    assert cg.logic == "and"


# 验证 parse_condition 对 && 与 || 混用抛 ConditionParseError。
# 同时含 && 与 || 时抛异常，要求拆分为多条 Hook。
def test_parse_condition_mixed_and_or_raises() -> None:
    with pytest.raises(ConditionParseError):
        parse_condition(
            'tool == "WriteFile" && tool == "EditFile" || tool == "ReadFile"'
        )


# 验证 parse_condition 对缺操作符抛 ConditionParseError。
# 表达式无四操作符之一时抛异常。
def test_parse_condition_missing_operator_raises() -> None:
    with pytest.raises(ConditionParseError):
        parse_condition("tool WriteFile")


# 验证 parse_condition 对空 field 抛 ConditionParseError。
# 表达式左侧为空时抛异常。
def test_parse_condition_empty_field_raises() -> None:
    with pytest.raises(ConditionParseError):
        parse_condition('== "WriteFile"')


# 验证 parse_condition 剥离单引号包裹的值。
# value 用单引号包裹，断言剥离后值为 WriteFile。
def test_parse_condition_strips_single_quotes() -> None:
    cg = parse_condition("tool == 'WriteFile'")
    assert cg is not None
    assert cg.conditions[0].value == "WriteFile"


# 验证 parse_condition 剥离双引号包裹的值。
# value 用双引号包裹，断言剥离后值为 WriteFile。
def test_parse_condition_strips_double_quotes() -> None:
    cg = parse_condition('tool == "WriteFile"')
    assert cg is not None
    assert cg.conditions[0].value == "WriteFile"
