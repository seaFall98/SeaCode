"""Skill 解析器单元测试：SkillDef 数据类、frontmatter 解析、单文件与目录型解析、参数替换。"""

from __future__ import annotations

from pathlib import Path

import pytest

from seacode.skills import (
    SkillDef,
    parse_frontmatter,
    parse_skill_directory,
    parse_skill_file,
    substitute_arguments,
)

# ---------- SkillDef ----------


# 验证 SkillDef 八字段默认值。
# 仅传必填三字段构造，断言 mode/model/context/source_path/is_directory 取默认。
def test_skilldef_defaults() -> None:
    skill = SkillDef(name="x", description="d", prompt_body="b")
    assert skill.name == "x"
    assert skill.description == "d"
    assert skill.prompt_body == "b"
    assert skill.mode == "inline"
    assert skill.model is None
    assert skill.context == "full"
    assert skill.source_path is None
    assert skill.is_directory is False


# 验证 SkillDef 保留传入的字段值。
# 构造时传入全部八字段，断言持有原值。
def test_skilldef_retains_passed_values() -> None:
    path = Path("/x")
    skill = SkillDef(
        name="x",
        description="d",
        prompt_body="b",
        mode="fork",
        model="claude",
        context="recent",
        source_path=path,
        is_directory=True,
    )
    assert skill.mode == "fork"
    assert skill.model == "claude"
    assert skill.context == "recent"
    assert skill.source_path is path
    assert skill.is_directory is True


# ---------- parse_frontmatter ----------


# 验证 parse_frontmatter 解析合法 frontmatter 与 body。
# 输入标准 --- 包裹的 YAML 与 body，断言返回 dict 与 body 字符串。
def test_parse_frontmatter_valid() -> None:
    text = "---\nname: x\ndescription: d\n---\nbody"
    data, body = parse_frontmatter(text)
    assert data == {"name": "x", "description": "d"}
    assert body == "body"


# 验证 parse_frontmatter 缺起始 --- 抛 ValueError。
# 输入不以 ---\n 起始，断言抛 ValueError。
def test_parse_frontmatter_missing_start_raises() -> None:
    text = "name: x\n---\nbody"
    with pytest.raises(ValueError, match="起始"):
        parse_frontmatter(text)


# 验证 parse_frontmatter 缺结束 --- 抛 ValueError。
# 输入只有起始 ---，断言抛 ValueError。
def test_parse_frontmatter_missing_end_raises() -> None:
    text = "---\nname: x\nbody"
    with pytest.raises(ValueError, match="结束"):
        parse_frontmatter(text)


# 验证 parse_frontmatter YAML 语法错误包装为 ValueError。
# 输入未闭合的 flow 序列触发 YAML 错误，断言抛 ValueError 含"语法错误"。
def test_parse_frontmatter_yaml_syntax_error_raises() -> None:
    text = "---\nname: [unclosed\n---\nbody"
    with pytest.raises(ValueError, match="语法错误"):
        parse_frontmatter(text)


# 验证 parse_frontmatter 空 frontmatter 抛 ValueError。
# 输入 --- 紧跟 ---，yaml.safe_load 返回 None，断言抛 ValueError 含"字典"。
def test_parse_frontmatter_empty_raises_valueerror() -> None:
    text = "---\n---\nbody"
    with pytest.raises(ValueError, match="字典"):
        parse_frontmatter(text)


# 验证 parse_frontmatter 多行 body 原样返回。
# 输入含标题与多行正文，断言 body 包含多行。
def test_parse_frontmatter_multiline_body() -> None:
    text = "---\nname: x\n---\n# Title\n\nbody\n"
    data, body = parse_frontmatter(text)
    assert data == {"name": "x"}
    assert body == "# Title\n\nbody\n"


# ---------- parse_skill_file ----------


# 写入 SKILL.md 文件并返回路径。
def _write_md(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# 验证 parse_skill_file 解析合法 SKILL.md。
# 临时文件写入合法 frontmatter，断言返回 SkillDef 各字段正确。
def test_parse_skill_file_valid(tmp_path: Path) -> None:
    path = _write_md(
        tmp_path / "commit.md",
        "---\nname: commit\ndescription: 提交\nmode: inline\n"
        "context: full\n---\n# Commit\n执行提交\n",
    )
    skill = parse_skill_file(path)
    assert skill.name == "commit"
    assert skill.description == "提交"
    assert skill.mode == "inline"
    assert skill.context == "full"
    assert skill.is_directory is False
    assert skill.source_path == path
    assert "# Commit" in skill.prompt_body


# 验证 parse_skill_file 缺 name 抛 ValueError。
# frontmatter 不含 name，断言抛 ValueError。
def test_parse_skill_file_missing_name_raises(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\ndescription: d\n---\nbody")
    with pytest.raises(ValueError, match="缺少 name"):
        parse_skill_file(path)


# 验证 parse_skill_file 非法 name（大写）抛 ValueError。
# name 含大写字母不匹配正则，断言抛 ValueError。
def test_parse_skill_file_invalid_name_uppercase_raises(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: Commit\ndescription: d\n---\nbody")
    with pytest.raises(ValueError, match="name 非法"):
        parse_skill_file(path)


# 验证 parse_skill_file 非法 name（下划线）抛 ValueError。
# name 含下划线不匹配正则，断言抛 ValueError。
def test_parse_skill_file_invalid_name_underscore_raises(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: commit_fix\ndescription: d\n---\nbody")
    with pytest.raises(ValueError, match="name 非法"):
        parse_skill_file(path)


# 验证 parse_skill_file 非法 name（数字开头）抛 ValueError。
# name 以数字开头不匹配正则，断言抛 ValueError。
def test_parse_skill_file_invalid_name_digit_start_raises(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: 1commit\ndescription: d\n---\nbody")
    with pytest.raises(ValueError, match="name 非法"):
        parse_skill_file(path)


# 验证 parse_skill_file 非法 mode 抛 ValueError。
# mode 不在 inline/fork 中，断言抛 ValueError。
def test_parse_skill_file_invalid_mode_raises(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: x\ndescription: d\nmode: invalid\n---\nbody")
    with pytest.raises(ValueError, match="mode 非法"):
        parse_skill_file(path)


# 验证 parse_skill_file 非法 context 抛 ValueError。
# context 不在 full/recent/none 中，断言抛 ValueError。
def test_parse_skill_file_invalid_context_raises(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: x\ndescription: d\ncontext: invalid\n---\nbody")
    with pytest.raises(ValueError, match="context 非法"):
        parse_skill_file(path)


# 验证 parse_skill_file 缺 description 抛 ValueError。
# frontmatter 不含 description，断言抛 ValueError。
def test_parse_skill_file_missing_description_raises(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: x\n---\nbody")
    with pytest.raises(ValueError, match="缺少 description"):
        parse_skill_file(path)


# 验证 parse_skill_file model 字段可选。
# frontmatter 含 model，断言 SkillDef.model 持有该值。
def test_parse_skill_file_model_optional(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: x\ndescription: d\nmodel: claude-3\n---\nbody")
    skill = parse_skill_file(path)
    assert skill.model == "claude-3"


# 验证 parse_skill_file mode 默认 inline。
# frontmatter 不含 mode，断言 SkillDef.mode == "inline"。
def test_parse_skill_file_mode_defaults_inline(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: x\ndescription: d\n---\nbody")
    skill = parse_skill_file(path)
    assert skill.mode == "inline"


# 验证 parse_skill_file context 默认 full。
# frontmatter 不含 context，断言 SkillDef.context == "full"。
def test_parse_skill_file_context_defaults_full(tmp_path: Path) -> None:
    path = _write_md(tmp_path / "x.md", "---\nname: x\ndescription: d\n---\nbody")
    skill = parse_skill_file(path)
    assert skill.context == "full"


# 验证 parse_skill_file mode=fork 与 context=recent。
# frontmatter 显式指定 fork/recent，断言 SkillDef 持有。
def test_parse_skill_file_mode_fork(tmp_path: Path) -> None:
    path = _write_md(
        tmp_path / "x.md",
        "---\nname: x\ndescription: d\nmode: fork\ncontext: recent\n---\nbody",
    )
    skill = parse_skill_file(path)
    assert skill.mode == "fork"
    assert skill.context == "recent"


# ---------- parse_skill_directory ----------


# 验证 parse_skill_directory 合法 skill.yaml + prompt.md。
# 临时目录创建两文件，断言返回 SkillDef 各字段正确且 is_directory 为 True。
def test_parse_skill_directory_valid(tmp_path: Path) -> None:
    dir_path = tmp_path / "commit-skill"
    dir_path.mkdir()
    (dir_path / "skill.yaml").write_text(
        "name: commit\ndescription: 提交\nmode: inline\n", encoding="utf-8"
    )
    (dir_path / "prompt.md").write_text("# Commit\n执行提交\n", encoding="utf-8")
    skill = parse_skill_directory(dir_path)
    assert skill.name == "commit"
    assert skill.description == "提交"
    assert skill.mode == "inline"
    assert skill.is_directory is True
    assert "# Commit" in skill.prompt_body


# 验证 parse_skill_directory 缺 name 从目录名派生。
# skill.yaml 仅含 description，断言 name 取自目录名。
def test_parse_skill_directory_name_from_dir(tmp_path: Path) -> None:
    dir_path = tmp_path / "commit-skill"
    dir_path.mkdir()
    (dir_path / "skill.yaml").write_text("description: d\n", encoding="utf-8")
    (dir_path / "prompt.md").write_text("body\n", encoding="utf-8")
    skill = parse_skill_directory(dir_path)
    assert skill.name == "commit-skill"


# 验证 parse_skill_directory 缺 description 从 prompt.md 推断。
# skill.yaml 仅含 name，prompt.md 含标题与正文，断言 description 为首个非标题行。
def test_parse_skill_directory_description_from_prompt(tmp_path: Path) -> None:
    dir_path = tmp_path / "x"
    dir_path.mkdir()
    (dir_path / "skill.yaml").write_text("name: x\n", encoding="utf-8")
    (dir_path / "prompt.md").write_text("# Title\n执行提交\n", encoding="utf-8")
    skill = parse_skill_directory(dir_path)
    assert skill.description == "执行提交"


# 验证 parse_skill_directory 非法 YAML 抛 ValueError。
# skill.yaml 含未闭合 flow 序列触发 YAML 错误，断言抛 ValueError。
def test_parse_skill_directory_invalid_yaml_raises(tmp_path: Path) -> None:
    dir_path = tmp_path / "x"
    dir_path.mkdir()
    (dir_path / "skill.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    (dir_path / "prompt.md").write_text("body\n", encoding="utf-8")
    with pytest.raises(ValueError, match="skill.yaml YAML 语法错误"):
        parse_skill_directory(dir_path)


# 验证 parse_skill_directory 回退 SKILL.md。
# 目录无 skill.yaml 但有 SKILL.md，断言返回 SkillDef 且 is_directory 为 True。
def test_parse_skill_directory_falls_back_to_skill_md(tmp_path: Path) -> None:
    dir_path = tmp_path / "x"
    dir_path.mkdir()
    _write_md(dir_path / "SKILL.md", "---\nname: x\ndescription: d\n---\nbody")
    skill = parse_skill_directory(dir_path)
    assert skill.name == "x"
    assert skill.is_directory is True


# 验证 parse_skill_directory 两种格式都缺失抛 ValueError。
# 空目录无任何 manifest，断言抛 ValueError。
def test_parse_skill_directory_no_manifest_raises(tmp_path: Path) -> None:
    dir_path = tmp_path / "empty"
    dir_path.mkdir()
    with pytest.raises(ValueError, match="缺少 skill.yaml"):
        parse_skill_directory(dir_path)


# 验证 parse_skill_directory 目录名下划线连字符合规化。
# 目录名 Commit_Skill，断言派生 name 为 commit-skill。
def test_parse_skill_directory_normalizes_underscore_dir_name(tmp_path: Path) -> None:
    dir_path = tmp_path / "Commit_Skill"
    dir_path.mkdir()
    (dir_path / "skill.yaml").write_text("description: d\n", encoding="utf-8")
    (dir_path / "prompt.md").write_text("body\n", encoding="utf-8")
    skill = parse_skill_directory(dir_path)
    assert skill.name == "commit-skill"


# ---------- substitute_arguments ----------


# 验证 substitute_arguments 替换 $ARGUMENTS 占位符。
# body 含 $ARGUMENTS，args 非空，断言占位符被替换。
def test_substitute_arguments_replaces_placeholder() -> None:
    assert substitute_arguments("执行 $ARGUMENTS", "fix typo") == "执行 fix typo"


# 验证 substitute_arguments 无占位符时追加 User Request 段落。
# body 无 $ARGUMENTS，args 非空，断言追加 ## User Request。
def test_substitute_arguments_appends_user_request() -> None:
    assert substitute_arguments("执行提交", "fix typo") == "执行提交\n\n## User Request\n\nfix typo"


# 验证 substitute_arguments 无 args 原样返回。
# args 为空字符串，断言返回原 body。
def test_substitute_arguments_no_args_returns_original() -> None:
    assert substitute_arguments("执行提交", "") == "执行提交"


# 验证 substitute_arguments 替换多个 $ARGUMENTS。
# body 含两个占位符，断言都被替换。
def test_substitute_arguments_replaces_multiple_placeholders() -> None:
    assert substitute_arguments("$ARGUMENTS 和 $ARGUMENTS", "a") == "a 和 a"


# 验证 substitute_arguments args 含特殊字符原样替换。
# args 含反引号，断言原样替换不转义。
def test_substitute_arguments_args_with_special_chars() -> None:
    assert substitute_arguments("执行 $ARGUMENTS", "fix `code` typo") == "执行 fix `code` typo"


# ---------- __init__.py 重导出 ----------


# 验证 seacode.skills 包重导出全部解析器公开 API。
# 从包根导入模块，断言各名字与子模块同源。
def test_skills_package_reexports_parser_api() -> None:
    import seacode.skills as skills_pkg

    assert skills_pkg.SkillDef is SkillDef
    assert skills_pkg.parse_frontmatter is parse_frontmatter
    assert skills_pkg.parse_skill_directory is parse_skill_directory
    assert skills_pkg.parse_skill_file is parse_skill_file
    assert skills_pkg.substitute_arguments is substitute_arguments
