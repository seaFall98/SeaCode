"""子 Agent 定义解析：``AgentDef`` 数据模型与 frontmatter 校验。

复用第 08 步的 ``process_includes`` 机制展开 ``@`` 引用；围栏代码块内的 ``@``
不展开。``AgentDef`` 是子 Agent 的不可变定义，由 ``AgentLoader`` 加载并缓存，
供 ``AgentTool`` 在运行时取出并实例化子 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from seacode.memory.instructions import process_includes

# 合法权限模式集合；空串表示沿用父 Agent 当前模式。
VALID_PERMISSION_MODES: set[str] = {"default", "acceptEdits", "bypassPermissions", ""}

# 合法 isolation 集合；本步只校验，``worktree`` 路由由第 13 步启用。
VALID_ISOLATION_MODES: set[str] = {"", "worktree"}


class AgentParseError(Exception):
    """子 Agent 定义文件解析或字段校验失败时抛出。"""


@dataclass
class AgentDef:
    """子 Agent 的不可变定义；十二字段对应 v1 既定 frontmatter 语义。

    ``file_path`` 为 None 表示内置定义（不参与热重载）；项目级与用户级定义在
    ``AgentLoader.get`` 时按 ``file_path`` 重读，失败回退缓存。
    """

    agent_type: str
    when_to_use: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: str = "inherit"
    max_turns: int = 200
    permission_mode: str = "default"
    background: bool = False
    isolation: str = ""
    file_path: Path | None = None
    source: str = "project"


# 解析 ``---`` frontmatter 并返回 (meta_dict, body_str)。
# 首行去前导空白后必须是 ``---``，否则视为缺起始分隔符；找不到闭合 ``---`` 抛错。
# frontmatter 必须是 YAML mapping，否则抛错。
def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise AgentParseError("missing frontmatter delimiter")

    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx == -1:
        raise AgentParseError("missing closing delimiter")

    frontmatter_text = "\n".join(lines[1:close_idx])
    try:
        meta = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise AgentParseError(f"frontmatter YAML parse error: {e}") from e

    if not isinstance(meta, dict):
        raise AgentParseError("frontmatter must be a mapping")

    body = "\n".join(lines[close_idx + 1 :])
    return meta, body


# 校验 frontmatter 字段并做大小写归一化；返回归一化后的 dict。
# 必填 ``name`` / ``description``；``permissionMode`` / ``isolation`` 必须在合法集合内；
# ``maxTurns`` 必须是正整数（排除 bool）；``model`` 仅对 ``inherit`` 别名做归一化。
def _validate_agent_meta(meta: dict[str, Any]) -> dict[str, Any]:
    if "name" not in meta or not meta["name"]:
        raise AgentParseError("missing 'name'")
    if "description" not in meta or not meta["description"]:
        raise AgentParseError("missing 'description'")

    permission_mode = meta.get("permissionMode", "default")
    if permission_mode not in VALID_PERMISSION_MODES:
        raise AgentParseError("invalid permissionMode")
    meta["permissionMode"] = permission_mode

    isolation = meta.get("isolation", "")
    if isolation not in VALID_ISOLATION_MODES:
        raise AgentParseError("invalid isolation")
    meta["isolation"] = isolation

    max_turns = meta.get("maxTurns", 200)
    # bool 是 int 的子类，必须显式排除，否则 True 会被误判为合法正整数。
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        raise AgentParseError("maxTurns must be a positive integer")
    meta["maxTurns"] = max_turns

    model = meta.get("model", "inherit")
    if isinstance(model, str) and model.lower() == "inherit":
        model = "inherit"
    meta["model"] = model

    return meta


# 解析单个 ``.md`` 子 Agent 定义文件；先展开 ``@`` 引用再解析 frontmatter。
# ``source`` 标记来源（project / user / builtin），写入 ``AgentDef.source``。
def parse_agent_file(path: Path, source: str = "project") -> AgentDef:
    text = path.read_text(encoding="utf-8")
    # 复用第 08 步 include 机制；围栏代码块内的 @ 不会被展开。
    text = process_includes(text, base_dir=path.parent, project_root=path.parent)
    meta, body = parse_frontmatter(text)
    validated = _validate_agent_meta(meta)

    return AgentDef(
        agent_type=validated["name"],
        when_to_use=validated["description"],
        system_prompt=body,
        tools=validated.get("tools", []),
        disallowed_tools=validated.get("disallowedTools", []),
        model=validated.get("model", "inherit"),
        max_turns=validated.get("maxTurns", 200),
        permission_mode=validated.get("permissionMode", "default"),
        background=validated.get("background", False),
        isolation=validated.get("isolation", ""),
        file_path=path,
        source=source,
    )
