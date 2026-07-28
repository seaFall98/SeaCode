"""InstallSkill 系统工具：从 URL 安装第三方 Skill 包到用户全局目录。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from seacode.skills.install import install_skill, parse_skill_url
from seacode.tools.base import Tool, ToolCategory, ToolResult


class _InstallSkillParams(BaseModel):
    """InstallSkill 工具参数：URL 加受控的项目/用户安装范围。"""

    url: str = Field(..., description="Skill 包 URL")
    scope: Literal["project", "user"] = Field(
        default="project",
        description="安装范围：project 写入当前项目，user 写入用户全局目录",
    )


class InstallSkill(Tool):
    """从 URL 安装第三方 Skill 包到项目或用户目录的系统工具。

    通过 set_loader/set_on_installed 注入依赖；execute 调用 parse_skill_url
    解析 → 按 scope 选择受控安装根目录 → install_skill 拉取 → loader.reload 刷新
    → on_installed 回调通知 TUI 重新注册命令。
    """

    name: str = "InstallSkill"
    description: str = (
        "从 URL 下载并安装第三方 Skill 包。默认安装到当前项目的 .seacode/skills/；"
        "如果用户明确要求所有项目共用，使用 scope=user 安装到 ~/.seacode/skills/。"
        "支持三种 URL 格式：skills.sh 短链（https://www.skills.sh/<owner>/<repo>/<name>）、"
        "GitHub tree 路径（https://github.com/<owner>/<repo>/tree/<ref>/<path>）、"
        "以及指向 SKILL.md 的原始 URL。"
        "安装完成后可通过 /<name> 命令或 LoadSkill 工具激活使用。"
        "当用户粘贴 Skill URL 并要求安装时调用此工具。"
    )
    params_model: type[BaseModel] = _InstallSkillParams
    category: ToolCategory = ToolCategory.WRITE
    is_system_tool: bool = True

    def __init__(self) -> None:
        self._loader: Any = None
        self._on_installed: Callable[[], None] | None = None

    # 注入 SkillLoader 引用。
    def set_loader(self, loader: Any) -> None:
        self._loader = loader

    # 注入安装后回调；TUI 用于触发命令重注册。
    def set_on_installed(self, callback: Callable[[], None]) -> None:
        self._on_installed = callback

    # 从 URL 安装 Skill；URL 解析失败或安装异常返回 is_error=True。
    # 安装目录名取 parsed.name（skill-name），避免多 skill 仓库互相覆盖。
    async def execute(self, params: BaseModel) -> ToolResult:
        if self._loader is None:
            return ToolResult(
                content="InstallSkill 未初始化：缺少 loader", is_error=True
            )
        url = params.url  # type: ignore[attr-defined]
        try:
            parsed = parse_skill_url(url)
        except ValueError as e:
            return ToolResult(
                content=f"URL 解析失败：{e}", is_error=True
            )

        scope = params.scope  # type: ignore[attr-defined]
        try:
            install_root = self._loader.get_install_root(scope)
        except (AttributeError, ValueError) as e:
            return ToolResult(
                content=f"InstallSkill 未初始化：缺少有效安装范围契约（{e}）",
                is_error=True,
            )
        try:
            report = await install_skill(parsed, install_root=install_root)
        except Exception as e:
            return ToolResult(
                content=f"安装失败：{e}", is_error=True
            )

        # 安装成功后刷新 catalog 与命令注册。
        self._loader.reload()
        if self._on_installed is not None:
            try:
                self._on_installed()
            except Exception:
                # 回调失败不阻塞安装成功路径。
                pass
        return ToolResult(
            content=(
                f"已安装 Skill：{report.skill_name}（范围：{scope}）"
                f" 到 {report.target_dir}"
            ),
            is_error=False,
        )
