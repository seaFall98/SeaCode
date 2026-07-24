"""应用层路径沙箱：限制文件工具访问范围，防止符号链接逃逸与禁写路径覆盖。"""

from __future__ import annotations

import tempfile
from pathlib import Path


class PathSandbox:
    """应用层路径沙箱：deny_write 优先于 allow_write。

    - _allowed_roots：项目根 + 系统临时目录 + extra_allowed
    - _deny_write：默认三敏感路径（.seacode/config.yaml / permissions.local.yaml / skills/）
    - check() 解析符号链接；不存在文件向上找祖先解析后拼回原路径
    """

    # 默认禁写路径：含 API key、可写权限规则文件与可执行 Skill 目录。
    _DEFAULT_DENY_WRITE: list[str] = [
        ".seacode/config.yaml",
        ".seacode/permissions.local.yaml",
        ".seacode/skills/",
    ]

    def __init__(
        self,
        project_root: str,
        extra_allowed: list[str] | None = None,
        deny_write: list[str] | None = None,
    ) -> None:
        root = Path(project_root).resolve()
        self._allowed_roots: list[Path] = [
            root,
            Path(tempfile.gettempdir()).resolve(),
        ]
        if extra_allowed:
            for p in extra_allowed:
                self._allowed_roots.append(Path(p).resolve())

        # 禁写路径列表：相对路径基于 project_root 解析为绝对路径。
        self._deny_write: list[Path] = []
        for dp in (deny_write or self._DEFAULT_DENY_WRITE):
            dp_path = Path(dp)
            if not dp_path.is_absolute():
                dp_path = root / dp_path
            self._deny_write.append(dp_path.resolve())

    # 返回项目根路径，供外部展示与路径拼接使用。
    @property
    def project_root(self) -> Path:
        return self._allowed_roots[0]

    # 检查路径是否命中禁写列表；支持精确匹配与目录前缀（relative_to）匹配。
    def _is_deny_write(self, real_path: Path) -> bool:
        for deny_path in self._deny_write:
            if real_path == deny_path:
                return True
            try:
                real_path.relative_to(deny_path)
                return True
            except ValueError:
                continue
        return False

    # 检查路径是否在沙箱内；返回 (是否允许, 原因)。
    def check(self, path: str) -> tuple[bool, str]:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.project_root / p
        abs_path = p.absolute()

        # 不存在的文件向上找祖先解析，避免符号链接断链导致误判。
        try:
            real_path = abs_path.resolve(strict=True)
        except OSError:
            ancestor = abs_path
            while not ancestor.exists():
                parent = ancestor.parent
                if parent == ancestor:
                    return False, f"无法解析路径: {path}"
                ancestor = parent
            try:
                resolved_ancestor = ancestor.resolve(strict=True)
            except OSError:
                return False, f"无法解析路径: {path}"
            real_path = resolved_ancestor / abs_path.relative_to(ancestor)

        # 禁写检查优先于允许检查。
        if self._is_deny_write(real_path):
            return False, f"路径 {path} 在禁写列表中"

        for root in self._allowed_roots:
            try:
                real_path.relative_to(root)
                return True, ""
            except ValueError:
                continue

        return False, f"路径 {path} 超出沙箱范围"
