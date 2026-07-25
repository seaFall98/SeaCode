"""子 Agent 定义解析的单元测试：覆盖 AgentDef 数据类、frontmatter 解析与字段校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from seacode.agents.parser import (
    VALID_ISOLATION_MODES,
    VALID_PERMISSION_MODES,
    AgentDef,
    AgentParseError,
    _validate_agent_meta,
    parse_agent_file,
    parse_frontmatter,
)

# ---------------------------------------------------------------------------
# AgentDef 数据类
# ---------------------------------------------------------------------------


# 验证 AgentDef 十二字段默认值符合规格。
# 构造仅传必填三项的 AgentDef，断言其余字段取默认值。
def test_agent_def_defaults_match_expected_values() -> None:
    agent_def = AgentDef(agent_type="x", when_to_use="y", system_prompt="z")
    assert agent_def.tools == []
    assert agent_def.disallowed_tools == []
    assert agent_def.model == "inherit"
    assert agent_def.max_turns == 200
    assert agent_def.permission_mode == "default"
    assert agent_def.background is False
    assert agent_def.isolation == ""
    assert agent_def.file_path is None
    assert agent_def.source == "project"


# 验证 AgentDef 构造时传入的全部字段值被完整保留。
# 逐字段传入非默认值后断言每个字段都持有传入值。
def test_agent_def_constructor_preserves_all_fields() -> None:
    path = Path("/x.md")
    agent_def = AgentDef(
        agent_type="Explore",
        when_to_use="探索",
        system_prompt="body",
        tools=["ReadFile"],
        disallowed_tools=["Agent"],
        model="haiku",
        max_turns=50,
        permission_mode="acceptEdits",
        background=True,
        isolation="worktree",
        file_path=path,
        source="user",
    )
    assert agent_def.agent_type == "Explore"
    assert agent_def.when_to_use == "探索"
    assert agent_def.system_prompt == "body"
    assert agent_def.tools == ["ReadFile"]
    assert agent_def.disallowed_tools == ["Agent"]
    assert agent_def.model == "haiku"
    assert agent_def.max_turns == 50
    assert agent_def.permission_mode == "acceptEdits"
    assert agent_def.background is True
    assert agent_def.isolation == "worktree"
    assert agent_def.file_path is path
    assert agent_def.source == "user"


# 验证 AgentDef.tools 默认值是独立实例。
# 两个 AgentDef 实例的 tools 字段不应是同一对象，避免共享可变默认值。
def test_agent_def_tools_default_is_independent_instance() -> None:
    a1 = AgentDef(agent_type="x", when_to_use="y", system_prompt="z")
    a2 = AgentDef(agent_type="x", when_to_use="y", system_prompt="z")
    assert a1.tools is not a2.tools


# 验证 AgentDef.disallowed_tools 默认值是独立实例。
# 两个 AgentDef 实例的 disallowed_tools 字段不应是同一对象。
def test_agent_def_disallowed_tools_default_is_independent_instance() -> None:
    a1 = AgentDef(agent_type="x", when_to_use="y", system_prompt="z")
    a2 = AgentDef(agent_type="x", when_to_use="y", system_prompt="z")
    assert a1.disallowed_tools is not a2.disallowed_tools


# 验证合法权限模式集合包含规格定义的四个值。
# 直接读取常量断言四个合法值都存在。
def test_valid_permission_modes_contains_expected_values() -> None:
    assert "default" in VALID_PERMISSION_MODES
    assert "acceptEdits" in VALID_PERMISSION_MODES
    assert "bypassPermissions" in VALID_PERMISSION_MODES
    assert "" in VALID_PERMISSION_MODES


# 验证合法 isolation 集合包含规格定义的两个值。
# 直接读取常量断言空串与 worktree 都存在。
def test_valid_isolation_modes_contains_expected_values() -> None:
    assert "" in VALID_ISOLATION_MODES
    assert "worktree" in VALID_ISOLATION_MODES


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


# 验证 parse_frontmatter 解析合法 frontmatter + body。
# 构造标准格式文本，断言返回的 meta 与 body 与输入一致。
def test_parse_frontmatter_parses_valid_input() -> None:
    text = "---\nname: Explore\ndescription: 探索\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "Explore", "description": "探索"}
    assert body == "body"


# 验证 parse_frontmatter 缺起始分隔符抛 AgentParseError。
# 构造无 --- 起始的文本，断言抛错且消息含 missing frontmatter delimiter。
def test_parse_frontmatter_missing_start_delimiter_raises() -> None:
    text = "name: Explore"
    with pytest.raises(AgentParseError, match="missing frontmatter delimiter"):
        parse_frontmatter(text)


# 验证 parse_frontmatter 缺闭合分隔符抛 AgentParseError。
# 构造只有起始 --- 的文本，断言抛错且消息含 missing closing delimiter。
def test_parse_frontmatter_missing_closing_delimiter_raises() -> None:
    text = "---\nname: Explore\nbody"
    with pytest.raises(AgentParseError, match="missing closing delimiter"):
        parse_frontmatter(text)


# 验证 parse_frontmatter 非 YAML mapping 抛 AgentParseError。
# frontmatter 内容为列表，断言抛错且消息含 must be a mapping。
def test_parse_frontmatter_non_mapping_raises() -> None:
    text = "---\n- list\n---\nbody"
    with pytest.raises(AgentParseError, match="must be a mapping"):
        parse_frontmatter(text)


# 验证 parse_frontmatter 对 body 含多行换行的情况保留原样。
# 构造含多行 body 的文本，断言 body 字符串保留换行。
def test_parse_frontmatter_preserves_multiline_body() -> None:
    text = "---\nname: x\ndescription: y\n---\nline1\nline2\nline3"
    _meta, body = parse_frontmatter(text)
    assert body == "line1\nline2\nline3"


# ---------------------------------------------------------------------------
# _validate_agent_meta
# ---------------------------------------------------------------------------


# 验证 _validate_agent_meta 缺 name 抛 AgentParseError。
# 构造只含 description 的 meta，断言抛错且消息含 missing 'name'。
def test_validate_agent_meta_missing_name_raises() -> None:
    meta = {"description": "x"}
    with pytest.raises(AgentParseError, match="missing 'name'"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta 缺 description 抛 AgentParseError。
# 构造只含 name 的 meta，断言抛错且消息含 missing 'description'。
def test_validate_agent_meta_missing_description_raises() -> None:
    meta = {"name": "x"}
    with pytest.raises(AgentParseError, match="missing 'description'"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta 非法 permissionMode 抛 AgentParseError。
# 构造非法 permissionMode 的 meta，断言抛错且消息含 invalid permissionMode。
def test_validate_agent_meta_invalid_permission_mode_raises() -> None:
    meta = {"name": "x", "description": "y", "permissionMode": "invalid"}
    with pytest.raises(AgentParseError, match="invalid permissionMode"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta 合法 permissionMode 四值全部通过。
# 分别用 default / acceptEdits / bypassPermissions / 空串 调用，断言不抛错。
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypassPermissions", ""])
def test_validate_agent_meta_valid_permission_modes_pass(mode: str) -> None:
    meta = {"name": "x", "description": "y", "permissionMode": mode}
    validated = _validate_agent_meta(meta)
    assert validated["permissionMode"] == mode


# 验证 _validate_agent_meta 非法 isolation 抛 AgentParseError。
# 构造非法 isolation 的 meta，断言抛错且消息含 invalid isolation。
def test_validate_agent_meta_invalid_isolation_raises() -> None:
    meta = {"name": "x", "description": "y", "isolation": "invalid"}
    with pytest.raises(AgentParseError, match="invalid isolation"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta 合法 isolation 两值全部通过。
# 分别用空串与 worktree 调用，断言不抛错。
@pytest.mark.parametrize("iso", ["", "worktree"])
def test_validate_agent_meta_valid_isolation_modes_pass(iso: str) -> None:
    meta = {"name": "x", "description": "y", "isolation": iso}
    validated = _validate_agent_meta(meta)
    assert validated["isolation"] == iso


# 验证 _validate_agent_meta maxTurns 为 0 抛错。
# 构造 maxTurns=0 的 meta，断言抛错且消息含 positive integer。
def test_validate_agent_meta_max_turns_zero_raises() -> None:
    meta = {"name": "x", "description": "y", "maxTurns": 0}
    with pytest.raises(AgentParseError, match="positive integer"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta maxTurns 为负数抛错。
# 构造 maxTurns=-1 的 meta，断言抛错且消息含 positive integer。
def test_validate_agent_meta_max_turns_negative_raises() -> None:
    meta = {"name": "x", "description": "y", "maxTurns": -1}
    with pytest.raises(AgentParseError, match="positive integer"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta maxTurns 为浮点数抛错。
# 构造 maxTurns=1.5 的 meta，断言抛错且消息含 positive integer。
def test_validate_agent_meta_max_turns_float_raises() -> None:
    meta = {"name": "x", "description": "y", "maxTurns": 1.5}
    with pytest.raises(AgentParseError, match="positive integer"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta maxTurns 为字符串抛错。
# 构造 maxTurns="200" 的 meta，断言抛错且消息含 positive integer。
def test_validate_agent_meta_max_turns_string_raises() -> None:
    meta = {"name": "x", "description": "y", "maxTurns": "200"}
    with pytest.raises(AgentParseError, match="positive integer"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta maxTurns 为 bool 抛错。
# 构造 maxTurns=True 的 meta，断言抛错且消息含 positive integer。
def test_validate_agent_meta_max_turns_bool_raises() -> None:
    meta = {"name": "x", "description": "y", "maxTurns": True}
    with pytest.raises(AgentParseError, match="positive integer"):
        _validate_agent_meta(meta)


# 验证 _validate_agent_meta maxTurns 正整数合法通过。
# 构造 maxTurns=50 的 meta，断言返回值保留 50。
def test_validate_agent_meta_max_turns_positive_int_passes() -> None:
    meta = {"name": "x", "description": "y", "maxTurns": 50}
    validated = _validate_agent_meta(meta)
    assert validated["maxTurns"] == 50


# 验证 _validate_agent_meta 对 model 别名做大小写归一化。
# 分别传 INHERIT / Inherit / inherit 三种写法，断言归一化为 inherit。
@pytest.mark.parametrize("raw", ["INHERIT", "Inherit", "inherit"])
def test_validate_agent_meta_normalizes_inherit_model(raw: str) -> None:
    meta = {"name": "x", "description": "y", "model": raw}
    validated = _validate_agent_meta(meta)
    assert validated["model"] == "inherit"


# 验证 _validate_agent_meta 对非别名 model 直通不归一化。
# 构造具体模型名，断言返回值原样保留。
def test_validate_agent_meta_passes_through_non_alias_model() -> None:
    meta = {"name": "x", "description": "y", "model": "claude-sonnet-4"}
    validated = _validate_agent_meta(meta)
    assert validated["model"] == "claude-sonnet-4"


# ---------------------------------------------------------------------------
# parse_agent_file
# ---------------------------------------------------------------------------


# 验证 parse_agent_file 合法文件解析为 AgentDef。
# 构造临时 .md 文件含 frontmatter + body，断言各字段正确填充。
def test_parse_agent_file_parses_valid_file(tmp_path: Path) -> None:
    agent_md = tmp_path / "explore.md"
    agent_md.write_text(
        "---\nname: Explore\ndescription: 探索\n---\nbody content",
        encoding="utf-8",
    )
    agent_def = parse_agent_file(agent_md, source="project")
    assert agent_def.agent_type == "Explore"
    assert agent_def.when_to_use == "探索"
    assert agent_def.system_prompt == "body content"
    assert agent_def.file_path == agent_md
    assert agent_def.source == "project"


# 验证 parse_agent_file 展开 @ 引用。
# 构造主文件含 @./other.md 引用与被引用文件，断言 system_prompt 含被引用内容。
def test_parse_agent_file_expands_at_includes(tmp_path: Path) -> None:
    other_md = tmp_path / "other.md"
    other_md.write_text("INCLUDED_CONTENT", encoding="utf-8")
    agent_md = tmp_path / "agent.md"
    agent_md.write_text(
        "---\nname: x\ndescription: y\n---\n@./other.md",
        encoding="utf-8",
    )
    agent_def = parse_agent_file(agent_md, source="project")
    assert "INCLUDED_CONTENT" in agent_def.system_prompt


# 验证 parse_agent_file 跳过围栏代码块内的 @。
# 构造主文件 body 含反引号包裹的 @not-a-reference，断言不被展开。
def test_parse_agent_file_skips_at_in_fenced_code(tmp_path: Path) -> None:
    agent_md = tmp_path / "agent.md"
    agent_md.write_text(
        "---\nname: x\ndescription: y\n---\n`@not-a-reference`",
        encoding="utf-8",
    )
    agent_def = parse_agent_file(agent_md, source="project")
    assert "@not-a-reference" in agent_def.system_prompt


# 验证 parse_agent_file 解析 frontmatter 校验失败时抛 AgentParseError。
# 构造缺 name 的 frontmatter，断言抛错。
def test_parse_agent_file_invalid_frontmatter_raises(tmp_path: Path) -> None:
    agent_md = tmp_path / "bad.md"
    agent_md.write_text(
        "---\ndescription: missing name\n---\nbody",
        encoding="utf-8",
    )
    with pytest.raises(AgentParseError, match="missing 'name'"):
        parse_agent_file(agent_md, source="project")


# 验证 parse_agent_file 把 isolation: worktree 字段传入 AgentDef。
# 构造 frontmatter 含 isolation: worktree 的文件，断言 agent_def.isolation == "worktree"。
def test_parse_agent_file_passes_isolation_field(tmp_path: Path) -> None:
    agent_md = tmp_path / "iso.md"
    agent_md.write_text(
        "---\nname: Iso\ndescription: y\nisolation: worktree\n---\nbody",
        encoding="utf-8",
    )
    agent_def = parse_agent_file(agent_md, source="project")
    assert agent_def.isolation == "worktree"


# 验证 parse_agent_file 在 isolation 缺失时使用默认空串。
# 构造无 isolation 字段的 frontmatter，断言 agent_def.isolation == ""。
def test_parse_agent_file_defaults_isolation_to_empty(tmp_path: Path) -> None:
    agent_md = tmp_path / "noiso.md"
    agent_md.write_text(
        "---\nname: NoIso\ndescription: y\n---\nbody",
        encoding="utf-8",
    )
    agent_def = parse_agent_file(agent_md, source="project")
    assert agent_def.isolation == ""


# 验证 parse_agent_file 非法 isolation 抛 AgentParseError。
# 构造 isolation: invalid 的 frontmatter，断言抛错且消息含 invalid isolation。
def test_parse_agent_file_invalid_isolation_raises(tmp_path: Path) -> None:
    agent_md = tmp_path / "badiso.md"
    agent_md.write_text(
        "---\nname: BadIso\ndescription: y\nisolation: invalid\n---\nbody",
        encoding="utf-8",
    )
    with pytest.raises(AgentParseError, match="invalid isolation"):
        parse_agent_file(agent_md, source="project")
