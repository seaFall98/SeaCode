"""InstallSkill 系统工具单元测试：覆盖安装、URL 解析、异常处理与类属性。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from seacode.skills.install import InstallReport
from seacode.tools.base import ToolCategory
from seacode.tools.install_skill import InstallSkill, _InstallSkillParams


# 测试 loader：只实现 SkillLoader 的公开安装目录契约和 reload 行为。
class _FakeLoader:
    def __init__(
        self,
        project_dir: str = "/repo/.seacode/skills",
        user_dir: str = "/home/.seacode/skills",
    ) -> None:
        self.project_dir = Path(project_dir)
        self.user_dir = Path(user_dir)
        self.reload_calls = 0

    def get_install_root(self, scope: str) -> Path:
        if scope == "project":
            return self.project_dir
        if scope == "user":
            return self.user_dir
        raise ValueError(f"unsupported scope: {scope}")

    def reload(self) -> None:
        self.reload_calls += 1


# 假回调：记录调用次数，用于校验 on_installed 触发。
class _FakeCallback:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


# patch install_skill 的上下文管理器：返回 AsyncMock，便于断言调用。
def _patch_install_skill(mock: AsyncMock):
    return patch("seacode.tools.install_skill.install_skill", new=mock)


# ---------- 类属性与 schema ----------


# 验证 InstallSkill 类属性符合系统写入工具约定。
# 直接读类属性断言 name/category/is_system_tool 与设计一致。
def test_install_skill_class_attributes() -> None:
    assert InstallSkill.name == "InstallSkill"
    assert InstallSkill.category == ToolCategory.WRITE
    assert InstallSkill.is_system_tool is True


# 验证 InstallSkill input_schema 含 url 必填字段。
# 调 get_schema() 取 input_schema 断言含 url 字段且 required 包含 url。
def test_install_skill_input_schema_contains_url() -> None:
    schema = InstallSkill().get_schema()["input_schema"]
    assert "url" in schema["properties"]
    assert "url" in schema.get("required", [])
    assert schema["properties"]["scope"]["enum"] == ["project", "user"]
    assert schema["properties"]["scope"]["default"] == "project"


# ---------- execute 正常分支 ----------


# 验证 InstallSkill 正常安装调 install_skill、reload、callback 并返回成功。
# patch install_skill 为 AsyncMock 返回 InstallReport，注入 loader 与 callback，
# 断言全部副作用被触发。
async def test_install_skill_success_invokes_install_reload_callback() -> None:
    loader = _FakeLoader(user_dir="/home/.seacode/skills")
    callback = _FakeCallback()
    tool = InstallSkill()
    tool.set_loader(loader)
    tool.set_on_installed(callback)

    fake_install = AsyncMock(
        return_value=InstallReport(
            skill_name="commit",
            target_dir="/home/.seacode/skills/commit",
            file_count=1,
            total_bytes=10,
        )
    )
    with _patch_install_skill(fake_install):
        result = await tool.execute(
            _InstallSkillParams(
                url="https://skills.sh/user/repo/commit", scope="user"
            )
        )

    assert result.is_error is False
    assert "已安装" in result.content
    assert "commit" in result.content
    assert "user" in result.content
    fake_install.assert_awaited_once()
    assert loader.reload_calls == 1
    assert callback.calls == 1


# 验证 InstallSkill 无 callback 时正常安装不抛异常。
# 强制 _on_installed=None，正常安装路径仍返回成功且 reload 被调用。
async def test_install_skill_no_callback_does_not_raise() -> None:
    loader = _FakeLoader()
    tool = InstallSkill()
    tool.set_loader(loader)
    tool._on_installed = None

    fake_install = AsyncMock(
        return_value=InstallReport(
            skill_name="x", target_dir="/home/.seacode/skills/x"
        )
    )
    with _patch_install_skill(fake_install):
        result = await tool.execute(
            _InstallSkillParams(url="https://skills.sh/user/repo/x", scope="project")
        )

    assert result.is_error is False
    assert "已安装" in result.content
    assert loader.reload_calls == 1


# 验证未指定 scope 时安装到项目级目录，并把项目范围写入结果。
# loader 只暴露公开 get_install_root，断言工具不会依赖用户目录私有字段。
async def test_install_skill_defaults_to_project_scope() -> None:
    loader = _FakeLoader(project_dir="/repo/.seacode/skills")
    tool = InstallSkill()
    tool.set_loader(loader)

    fake_install = AsyncMock(
        return_value=InstallReport(
            skill_name="frontend-design",
            target_dir="/repo/.seacode/skills/frontend-design",
        )
    )
    with _patch_install_skill(fake_install):
        result = await tool.execute(
            _InstallSkillParams(url="https://skills.sh/user/repo/frontend-design")
        )

    assert result.is_error is False
    assert "project" in result.content
    assert "/repo/.seacode/skills/frontend-design" in result.content
    fake_install.assert_awaited_once_with(
        fake_install.call_args.args[0], install_root=Path("/repo/.seacode/skills")
    )


# 验证 scope=project 选择 loader 的项目根目录，而不是用户根目录。
# 返回项目目标路径，断言安装器收到项目根目录并触发 reload。
async def test_install_skill_project_scope_uses_project_root() -> None:
    loader = _FakeLoader(
        project_dir="/workspace/.seacode/skills",
        user_dir="/home/.seacode/skills",
    )
    tool = InstallSkill()
    tool.set_loader(loader)

    fake_install = AsyncMock(
        return_value=InstallReport(
            skill_name="frontend-design",
            target_dir="/workspace/.seacode/skills/frontend-design",
        )
    )
    with _patch_install_skill(fake_install):
        result = await tool.execute(
            _InstallSkillParams(
                url="https://skills.sh/user/repo/frontend-design", scope="project"
            )
        )

    assert result.is_error is False
    assert "/workspace/.seacode/skills/frontend-design" in result.content
    assert fake_install.call_args.kwargs["install_root"] == Path(
        "/workspace/.seacode/skills"
    )


# ---------- execute 错误分支 ----------


# 验证 InstallSkill URL 解析失败返回错误且不调 install_skill。
# 传不匹配三种格式的 URL，断言 is_error=True 与 "URL 解析失败" 提示。
async def test_install_skill_invalid_url_returns_error() -> None:
    loader = _FakeLoader()
    tool = InstallSkill()
    tool.set_loader(loader)

    fake_install = AsyncMock()
    with _patch_install_skill(fake_install):
        result = await tool.execute(
            _InstallSkillParams(url="https://example.com/invalid")
        )

    assert result.is_error is True
    assert "URL 解析失败" in result.content
    fake_install.assert_not_awaited()
    assert loader.reload_calls == 0


# 验证 InstallSkill install_skill 抛异常返回错误且不调 reload 与 callback。
# patch install_skill 抛 ValueError("boom")，断言错误提示与副作用均未触发。
async def test_install_skill_install_exception_returns_error() -> None:
    loader = _FakeLoader()
    callback = _FakeCallback()
    tool = InstallSkill()
    tool.set_loader(loader)
    tool.set_on_installed(callback)

    fake_install = AsyncMock(side_effect=ValueError("boom"))
    with _patch_install_skill(fake_install):
        result = await tool.execute(
            _InstallSkillParams(url="https://skills.sh/user/repo/x")
        )

    assert result.is_error is True
    assert "安装失败：boom" in result.content
    assert loader.reload_calls == 0
    assert callback.calls == 0


# 验证 InstallSkill 未初始化 loader 返回错误。
# 强制 _loader=None，断言 is_error=True 与 "InstallSkill 未初始化" 提示。
async def test_install_skill_uninitialized_loader_returns_error() -> None:
    tool = InstallSkill()
    tool._loader = None

    result = await tool.execute(
        _InstallSkillParams(url="https://skills.sh/user/repo/x")
    )

    assert result.is_error is True
    assert "InstallSkill 未初始化" in result.content


# 验证 InstallSkill loader 缺少 _user_dir 返回错误。
# 构造无 _user_dir 属性的 loader，断言 is_error=True 与初始化提示。
async def test_install_skill_loader_missing_user_dir_returns_error() -> None:
    class _LoaderWithoutUserDir:
        reload_calls = 0

        def reload(self) -> None:
            self.reload_calls += 1

    tool = InstallSkill()
    tool.set_loader(_LoaderWithoutUserDir())

    fake_install = AsyncMock()
    with _patch_install_skill(fake_install):
        result = await tool.execute(
            _InstallSkillParams(url="https://skills.sh/user/repo/x")
        )

    assert result.is_error is True
    assert "InstallSkill 未初始化" in result.content
    fake_install.assert_not_awaited()


# 验证 InstallSkill callback 抛异常不阻塞安装成功路径。
# patch callback 抛异常，断言返回仍为成功且 reload 已被调用。
async def test_install_skill_callback_exception_does_not_block_success() -> None:
    loader = _FakeLoader()

    def raising_callback() -> None:
        raise RuntimeError("callback boom")

    tool = InstallSkill()
    tool.set_loader(loader)
    tool.set_on_installed(raising_callback)

    fake_install = AsyncMock(
        return_value=InstallReport(
            skill_name="x", target_dir="/home/.seacode/skills/x"
        )
    )
    with _patch_install_skill(fake_install):
        result = await tool.execute(
            _InstallSkillParams(url="https://skills.sh/user/repo/x")
        )

    assert result.is_error is False
    assert "已安装" in result.content
    assert loader.reload_calls == 1
