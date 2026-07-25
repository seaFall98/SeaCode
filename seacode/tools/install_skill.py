"""InstallSkill 系统工具：从 URL 安装第三方 Skill 包到用户全局目录。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from seacode.skills.install import install_skill, parse_skill_url
from seacode.tools.base import Tool, ToolCategory, ToolResult


class _InstallSkillParams(BaseModel):
    """InstallSkill 工具参数：仅 url 一个必填字段。"""

    url: str = Field(..., description="Skill 包 URL")


class InstallSkill(Tool):
    """从 URL 安装第三方 Skill 包到用户全局目录的系统工具。

    通过 set_loader/set_on_installed 注入依赖；execute 调用 parse_skill_url
    解析 → install_skill 拉取 → loader.reload 刷新 → on_installed 回调通知
    TUI 重新注册命令。
    """

    name: str = "InstallSkill"
    description: str = "从 URL 安装第三方 Skill 包到用户全局目录"
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

        # 安装到用户级 ~/.seacode/skills/ 目录；子目录名由 parsed.name 决定。
        user_dir = getattr(self._loader, "_user_dir", None)
        if user_dir is None:
            return ToolResult(
                content="InstallSkill 未初始化：loader 缺少 _user_dir",
                is_error=True,
            )
        try:
            report = await install_skill(parsed, install_root=user_dir)
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
            content=f"已安装 Skill：{report.skill_name} 到 {report.target_dir}",
            is_error=False,
        )
