"""LoadSkill 系统工具单元测试：覆盖依赖注入、加载、错误分支与类属性。"""

from __future__ import annotations

from seacode.skills.parser import SkillDef
from seacode.tools.base import ToolCategory
from seacode.tools.load_skill import LoadSkill, _LoadSkillParams


# 假 loader：可预设 get 与 get_catalog 返回值，记录调用参数。
class _FakeLoader:
    def __init__(
        self,
        skill: SkillDef | None = None,
        catalog: list[tuple[str, str]] | None = None,
    ) -> None:
        self._skill = skill
        self._catalog = catalog if catalog is not None else []
        self.get_calls: list[str] = []

    def get(self, name: str) -> SkillDef | None:
        self.get_calls.append(name)
        return self._skill

    def get_catalog(self) -> list[tuple[str, str]]:
        return self._catalog


# 假 Agent：记录 activate_skill 调用参数；recovery_state 显式 None 走跳过分支。
class _FakeAgent:
    def __init__(self) -> None:
        self.activate_calls: list[tuple[str, str]] = []
        # 显式 None 模拟未设置 recovery_state 的 Agent。
        self.recovery_state = None

    def activate_skill(self, name: str, prompt_body: str) -> None:
        self.activate_calls.append((name, prompt_body))


# 构造默认 commit SkillDef 供测试复用。
def _make_skill(
    name: str = "commit",
    description: str = "提交",
    prompt_body: str = "commit SOP body",
) -> SkillDef:
    return SkillDef(
        name=name,
        description=description,
        prompt_body=prompt_body,
        mode="inline",
        context="none",
    )


# ---------- 类属性与 schema ----------


# 验证 LoadSkill 类属性符合系统只读工具约定。
# 直接读类属性断言 name/category/is_system_tool 与设计一致。
def test_load_skill_class_attributes() -> None:
    assert LoadSkill.name == "LoadSkill"
    assert LoadSkill.category == ToolCategory.READ
    assert LoadSkill.is_system_tool is True


# 验证 LoadSkill input_schema 含 name 必填字段。
# 调 get_schema() 取 input_schema 断言含 name 字段且 required 包含 name。
def test_load_skill_input_schema_contains_name() -> None:
    schema = LoadSkill().get_schema()["input_schema"]
    assert "name" in schema["properties"]
    assert "name" in schema.get("required", [])


# ---------- execute 正常分支 ----------


# 验证 LoadSkill 正常加载返回 ToolResult 并调用 activate_skill。
# 注入 fake loader/agent，执行 _LoadSkillParams(name="commit")，断言 content 与 activate 调用。
async def test_load_skill_success_activates_skill() -> None:
    skill = _make_skill()
    loader = _FakeLoader(skill=skill, catalog=[("commit", "提交")])
    agent = _FakeAgent()
    tool = LoadSkill()
    tool.set_loader(loader)
    tool.set_agent(agent)

    result = await tool.execute(_LoadSkillParams(name="commit"))

    assert result.is_error is False
    assert result.content == f"# Skill: commit\n\n{skill.prompt_body}"
    assert agent.activate_calls == [("commit", skill.prompt_body)]
    assert loader.get_calls == ["commit"]


# 验证 LoadSkill 激活时使用原始 prompt_body 不调 substitute_arguments。
# prompt_body 含 $ARGUMENTS 占位符，激活后 prompt_body 仍含字面量占位符。
async def test_load_skill_does_not_substitute_arguments() -> None:
    skill = SkillDef(
        name="commit",
        description="提交",
        prompt_body="SOP with $ARGUMENTS placeholder",
        mode="inline",
        context="none",
    )
    loader = _FakeLoader(skill=skill, catalog=[("commit", "提交")])
    agent = _FakeAgent()
    tool = LoadSkill()
    tool.set_loader(loader)
    tool.set_agent(agent)

    result = await tool.execute(_LoadSkillParams(name="commit"))

    assert result.is_error is False
    # activate_skill 收到的是原始 prompt_body，未做 $ARGUMENTS 替换。
    assert agent.activate_calls == [("commit", "SOP with $ARGUMENTS placeholder")]
    assert "$ARGUMENTS" in result.content


# ---------- execute 错误分支 ----------


# 验证 LoadSkill 未知 name 返回错误与可用列表。
# loader.get 返回 None + catalog 返回 [("x","y")]，断言 is_error=True 与列表格式。
async def test_load_skill_unknown_name_returns_error_with_catalog() -> None:
    loader = _FakeLoader(skill=None, catalog=[("x", "y")])
    agent = _FakeAgent()
    tool = LoadSkill()
    tool.set_loader(loader)
    tool.set_agent(agent)

    result = await tool.execute(_LoadSkillParams(name="unknown"))

    assert result.is_error is True
    assert "未知 Skill：unknown" in result.content
    assert "- x: y" in result.content


# 验证 LoadSkill 可用列表格式为 "- name: description" 多行。
# catalog 返回 [("commit","提交"),("review","审查")]，断言两行均在 content 中。
async def test_load_skill_catalog_format() -> None:
    loader = _FakeLoader(
        skill=None,
        catalog=[("commit", "提交"), ("review", "审查")],
    )
    agent = _FakeAgent()
    tool = LoadSkill()
    tool.set_loader(loader)
    tool.set_agent(agent)

    result = await tool.execute(_LoadSkillParams(name="unknown"))

    assert "- commit: 提交" in result.content
    assert "- review: 审查" in result.content


# 验证 LoadSkill 未初始化 loader 返回错误。
# 强制 _loader=None，断言 is_error=True 与 "LoadSkill 未初始化" 提示。
async def test_load_skill_uninitialized_loader_returns_error() -> None:
    tool = LoadSkill()
    tool._loader = None
    tool._agent = _FakeAgent()

    result = await tool.execute(_LoadSkillParams(name="x"))

    assert result.is_error is True
    assert "LoadSkill 未初始化" in result.content


# 验证 LoadSkill 未初始化 agent 返回错误。
# 强制 _agent=None，断言 is_error=True 与 "LoadSkill 未初始化" 提示。
async def test_load_skill_uninitialized_agent_returns_error() -> None:
    tool = LoadSkill()
    tool.set_loader(_FakeLoader())
    tool._agent = None

    result = await tool.execute(_LoadSkillParams(name="x"))

    assert result.is_error is True
    assert "LoadSkill 未初始化" in result.content


# 验证 LoadSkill 未注入 loader 与 agent 时也返回错误。
# 不调 set_loader/set_agent，execute 应直接返回初始化错误。
async def test_load_skill_without_injection_returns_error() -> None:
    tool = LoadSkill()

    result = await tool.execute(_LoadSkillParams(name="x"))

    assert result.is_error is True
    assert "LoadSkill 未初始化" in result.content
