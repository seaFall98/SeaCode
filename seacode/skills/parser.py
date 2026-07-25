"""Skill 解析器：SkillDef 数据类、YAML frontmatter 解析、双格式支持、参数替换。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Skill name 校验：小写字母开头，允许小写字母、数字、连字符。
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]*$")

# 允许的 mode 与 context 枚举值。
_VALID_MODES = ("inline", "fork")
_VALID_CONTEXTS = ("full", "recent", "none")


@dataclass
class SkillDef:
    """Skill 定义数据类。

    name: Skill 唯一标识，匹配 ^[a-z][a-z0-9\\-*$。
    description: Skill 简短描述，用于目录摘要。
    prompt_body: Markdown 正文，作为 SOP 注入对话历史。
    mode: 执行模式 inline（注入主对话）或 fork（创建子 Agent 隔离执行）。
    model: 单 Skill 模型覆盖；为 None 表示沿用主 Agent 模型。
    context: fork 模式下子 Agent 上下文构建策略 full/recent/none。
    source_path: 文件路径，热重载时重读磁盘使用。
    is_directory: 目录型 Skill 标识，影响解析路径回退与来源标签。
    """

    name: str
    description: str
    prompt_body: str
    mode: str = "inline"
    model: str | None = None
    context: str = "full"
    source_path: Path | None = None
    is_directory: bool = False


# 解析 YAML frontmatter（--- 包裹）与 Markdown body。
# 缺起始或结束 --- 抛 ValueError；YAML 语法错误包装为 ValueError。
def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("frontmatter 必须以 --- 起始")
    lines = text.splitlines(keepends=True)
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx == -1:
        raise ValueError("frontmatter 缺少结束 ---")
    frontmatter_text = "".join(lines[1:end_idx])
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter YAML 语法错误: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("frontmatter 必须是 YAML 字典")
    body = "".join(lines[end_idx + 1 :])
    return data, body


# 校验 SkillDef 必填字段与枚举值；任一非法抛 ValueError。
def _validate_fields(
    name: Any,
    description: Any,
    mode: Any,
    context: Any,
) -> None:
    if not name or not isinstance(name, str):
        raise ValueError("Skill 缺少 name 字段")
    if not _SKILL_NAME_RE.match(name):
        raise ValueError(f"Skill name 非法: {name}（须匹配 ^[a-z][a-z0-9-]*$）")
    if not description or not isinstance(description, str):
        raise ValueError("Skill 缺少 description 字段")
    if mode not in _VALID_MODES:
        raise ValueError(f"Skill mode 非法: {mode}（允许 inline/fork）")
    if context not in _VALID_CONTEXTS:
        raise ValueError(f"Skill context 非法: {context}（允许 full/recent/none）")


# 解析 SKILL.md 单文件格式：读取文件 → parse_frontmatter → 字段校验 → 构造 SkillDef。
def parse_skill_file(path: Path) -> SkillDef:
    text = path.read_text(encoding="utf-8")
    data, body = parse_frontmatter(text)
    name = data.get("name")
    description = data.get("description")
    mode = data.get("mode", "inline")
    context = data.get("context", "full")
    model = data.get("model")
    _validate_fields(name, description, mode, context)
    return SkillDef(
        name=name,  # type: ignore[arg-type]
        description=description,  # type: ignore[arg-type]
        prompt_body=body,
        mode=mode,
        model=model,
        context=context,
        source_path=path,
        is_directory=False,
    )


# 从 prompt.md 推断 description：跳过空行与 # 开头标题行，取首个非空非标题行。
def _infer_description_from_prompt(prompt_text: str) -> str:
    for line in prompt_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


# 把目录名规整为合规的 Skill name：转小写，非 [a-z0-9-] 字符替换为 -。
def _normalize_name_from_dir(dir_name: str) -> str:
    lowered = dir_name.lower()
    normalized = re.sub(r"[^a-z0-9\-]", "-", lowered)
    # 不允许开头是数字；若开头是数字，前置 a- 前缀避免正则不匹配。
    if normalized and normalized[0].isdigit():
        normalized = "a-" + normalized
    if not _SKILL_NAME_RE.match(normalized):
        # 极端情况下仍不合规，再加一道保底。
        normalized = "skill-" + normalized
    return normalized


# 解析目录型 skill.yaml + prompt.md 分离格式。
# name 缺失从目录名派生；description 缺失从 prompt.md 首个非空非标题行推断。
def _parse_skill_yaml(
    yaml_path: Path, prompt_path: Path, dir_path: Path
) -> SkillDef:
    try:
        yaml_data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"skill.yaml YAML 语法错误: {e}") from e
    if not isinstance(yaml_data, dict):
        raise ValueError("skill.yaml 必须是 YAML 字典")

    name = yaml_data.get("name")
    if not name:
        name = _normalize_name_from_dir(dir_path.name)
    description = yaml_data.get("description")
    mode = yaml_data.get("mode", "inline")
    context = yaml_data.get("context", "full")
    model = yaml_data.get("model")
    prompt_body = prompt_path.read_text(encoding="utf-8")
    if not description:
        description = _infer_description_from_prompt(prompt_body)
    _validate_fields(name, description, mode, context)
    return SkillDef(
        name=name,
        description=description,
        prompt_body=prompt_body,
        mode=mode,
        model=model,
        context=context,
        source_path=yaml_path,
        is_directory=True,
    )


# 解析目录型 Skill：先尝试 skill.yaml + prompt.md，回退 SKILL.md。
# 两种格式都失败抛 ValueError。
def parse_skill_directory(path: Path) -> SkillDef:
    yaml_path = path / "skill.yaml"
    prompt_path = path / "prompt.md"
    skill_md_path = path / "SKILL.md"

    if yaml_path.exists() and prompt_path.exists():
        return _parse_skill_yaml(yaml_path, prompt_path, path)

    if skill_md_path.exists():
        # 回退到 SKILL.md 单文件格式；is_directory 仍标记为 True 以区分目录型。
        skill = parse_skill_file(skill_md_path)
        skill.is_directory = True
        return skill

    raise ValueError(
        f"Skill 目录 {path} 缺少 skill.yaml+prompt.md 或 SKILL.md"
    )


# 替换 prompt_body 中的 $ARGUMENTS 占位符或追加用户参数。
# 三条路径：有 $ARGUMENTS 替换；无占位符但 args 非空追加 ## User Request；args 为空原样返回。
def substitute_arguments(prompt_body: str, args: str) -> str:
    if not args:
        return prompt_body
    if "$ARGUMENTS" in prompt_body:
        return prompt_body.replace("$ARGUMENTS", args)
    return f"{prompt_body}\n\n## User Request\n\n{args}"
