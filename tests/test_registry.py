from __future__ import annotations

from pydantic import BaseModel

from seacode.tools import ToolRegistry, create_default_registry
from seacode.tools.base import Tool, ToolCategory, ToolResult


# 最小可注册工具，用于隔离测试注册中心行为而不依赖真实工具。
class _Params(BaseModel):
    value: str = ""


class _DummyTool(Tool):
    name = "Dummy"
    description = "A dummy tool for registry tests."
    params_model = _Params
    category = ToolCategory.READ

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(content="ok")


# 另一个最小工具，用于验证多工具注册与名称区分。
class _OtherTool(Tool):
    name = "Other"
    description = "Another dummy tool."
    params_model = _Params
    category = ToolCategory.READ

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(content="other")


# 验证 register 后可通过 get 按名查找，未注册名返回 None。
# 注册两个工具，断言按名查找命中、未知名返回 None。
def test_register_and_get_by_name() -> None:
    registry = ToolRegistry()
    dummy = _DummyTool()
    other = _OtherTool()

    registry.register(dummy)
    registry.register(other)

    assert registry.get("Dummy") is dummy
    assert registry.get("Other") is other
    assert registry.get("UnknownTool") is None


# 验证 disable 使工具从 Schema 列表移除，enable 后恢复。
# 注册后禁用，断言 Schema 不含该工具；启用后断言重新出现。
def test_disable_and_enable_toggle_schema_visibility() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool())

    assert registry.is_enabled("Dummy") is True
    registry.disable("Dummy")
    assert registry.is_enabled("Dummy") is False
    assert registry.get_all_schemas() == []

    registry.enable("Dummy")
    assert registry.is_enabled("Dummy") is True
    assert len(registry.get_all_schemas()) == 1


# 验证 Anthropic 协议 Schema 包含 name、description、input_schema 三个键。
# 注册工具后获取 Anthropic Schema，断言键集合与预期一致。
def test_anthropic_schema_format() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool())

    schemas = registry.get_all_schemas(protocol="anthropic")

    assert len(schemas) == 1
    schema = schemas[0]
    assert set(schema.keys()) == {"name", "description", "input_schema"}
    assert schema["name"] == "Dummy"
    assert schema["description"] == "A dummy tool for registry tests."
    assert isinstance(schema["input_schema"], dict)


# 验证 OpenAI 协议 Schema 包含 type、name、description、parameters 四个键。
# 注册工具后获取 OpenAI Schema，断言 type 为 function 且 parameters 存在。
def test_openai_schema_format() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool())

    schemas = registry.get_all_schemas(protocol="openai")

    assert len(schemas) == 1
    schema = schemas[0]
    assert set(schema.keys()) == {"type", "name", "description", "parameters"}
    assert schema["type"] == "function"
    assert schema["name"] == "Dummy"
    assert isinstance(schema["parameters"], dict)


# 验证 openai-compat 协议与 openai 协议生成相同的 Schema 格式。
# 注册工具后获取两协议 Schema，断言结构一致。
def test_openai_compat_schema_matches_openai_format() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool())

    compat_schema = registry.get_all_schemas(protocol="openai-compat")
    openai_schema = registry.get_all_schemas(protocol="openai")

    assert compat_schema == openai_schema


# 验证 should_defer=False 的工具默认纳入 Schema 列表。
# Dummy 工具 should_defer 为 False，断言它出现在 Schema 列表中。
def test_non_deferred_tool_appears_in_schemas() -> None:
    registry = ToolRegistry()
    tool = _DummyTool()
    assert tool.should_defer is False
    registry.register(tool)

    schemas = registry.get_all_schemas()

    assert len(schemas) == 1
    assert schemas[0]["name"] == "Dummy"


# 验证 create_default_registry 返回包含六个核心工具的注册中心。
# 列出全部工具名，断言集合等于六个核心工具名。
def test_default_registry_contains_six_core_tools() -> None:
    registry = create_default_registry()

    names = {tool.name for tool in registry.list_tools()}
    assert names == {"ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep"}


# 验证默认注册中心的双协议 Schema 数量与工具数一致。
# 获取两协议 Schema，断言各六个且名称集合匹配。
def test_default_registry_schemas_for_both_protocols() -> None:
    registry = create_default_registry()

    anthropic_schemas = registry.get_all_schemas(protocol="anthropic")
    openai_schemas = registry.get_all_schemas(protocol="openai")

    assert len(anthropic_schemas) == 6
    assert len(openai_schemas) == 6
    assert {s["name"] for s in anthropic_schemas} == {
        "ReadFile",
        "WriteFile",
        "EditFile",
        "Bash",
        "Glob",
        "Grep",
    }


# 验证禁用未知工具名不会创建无效禁用条目。
# 禁用未注册名后启用全部，断言不产生副作用。
def test_disable_unknown_tool_is_noop() -> None:
    registry = ToolRegistry()

    registry.disable("NonExistent")
    assert registry.is_enabled("NonExistent") is False

    registry.register(_DummyTool())
    assert registry.is_enabled("Dummy") is True
    assert len(registry.get_all_schemas()) == 1


# 验证 enable_all 清除所有禁用状态。
# 注册并禁用两个工具，调用 enable_all 后断言全部恢复可见。
def test_enable_all_restores_all_tools() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool())
    registry.register(_OtherTool())
    registry.disable("Dummy")
    registry.disable("Other")

    assert registry.get_all_schemas() == []

    registry.enable_all()

    assert len(registry.get_all_schemas()) == 2
