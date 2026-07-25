"""项目指令加载：按优先级发现并拼接指令文件，支持 @include 递归展开。"""

from __future__ import annotations

import os
from pathlib import Path

# @include 递归展开的最大深度，防止 A→B→A 无限递归。
MAX_INCLUDE_DEPTH = 5


# ---------------------------------------------------------------------------
# @ 引用指令格式
# ---------------------------------------------------------------------------
# 支持以下格式：
#   @./relative/path  @../relative/path  @~/home/path  @/absolute/path
# 其他 @-token（如 @username）被忽略，不视为引用指令。


# 解析一行文本，提取 @ 引用路径。
# 仅识别 @./ @.. @~/ @/ 四种前缀；@@ 视为转义不展开；含空白视为非引用。
def _parse_include(trimmed: str) -> str:
    if not trimmed.startswith("@") or trimmed.startswith("@@"):
        return ""
    rest = trimmed[1:]
    if not rest:
        return ""
    if " " in rest or "\t" in rest:
        return ""
    if (
        rest.startswith("./")
        or rest.startswith("../")
        or rest.startswith("~/")
        or rest.startswith("/")
    ):
        return rest
    return ""


# 将 include 路径解析为绝对路径：~/ 展开为 home，相对路径基于 base_dir 解析。
def _resolve_include(path: str, base_dir: Path) -> Path:
    if path.startswith("~/"):
        return Path.home() / path[2:]
    if os.path.isabs(path):
        return Path(path)
    return base_dir / path


# 递归展开 @ 引用指令：
# - 循环检测：seen 集合记录已包含文件的绝对路径
# - 代码块跳过：``` 围栏代码块内的 @ 引用不展开
# - 深度限制：最多递归 MAX_INCLUDE_DEPTH 层
def process_includes(
    content: str,
    base_dir: Path,
    project_root: Path,
    depth: int = 0,
    seen: set[str] | None = None,
) -> str:
    del project_root  # 保留参数语义对齐；当前实现不需要单独的 project_root 引用
    if depth > MAX_INCLUDE_DEPTH:
        return content

    if seen is None:
        seen = set()

    lines = content.split("\n")
    result: list[str] = []
    in_code = False  # 追踪是否处于 ``` 围栏代码块内

    for line in lines:
        stripped = line.strip()

        # 检测围栏代码块边界
        if stripped.startswith("```"):
            in_code = not in_code
            result.append(line)
            continue

        # 代码块内不展开 include 指令
        if not in_code:
            include_path = _parse_include(stripped)
            if include_path:
                resolved = _resolve_include(include_path, base_dir)
                try:
                    abs_str = str(resolved.resolve())
                except OSError:
                    result.append(line)
                    continue

                # 循环检测：已包含过的文件跳过
                if abs_str in seen:
                    result.append(line)
                    continue

                if not resolved.exists() or not resolved.is_file():
                    result.append("<!-- @ skipped: file not found -->")
                    continue

                try:
                    included = resolved.read_text(encoding="utf-8")
                except OSError:
                    result.append(line)
                    continue

                seen.add(abs_str)
                result.append(f"<!-- included from {include_path} -->")
                result.append(
                    process_includes(
                        included, resolved.parent, base_dir, depth + 1, seen
                    )
                )
                continue

        result.append(line)

    return "\n".join(result)


# 从 start 向上查找 .git 目录，返回 git 仓库根目录。
def _find_git_root(start: Path) -> Path | None:
    cur = start.resolve()
    while True:
        if (cur / ".git").exists():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


# 返回从 git root 到 work_dir 的所有目录。
# 如果 work_dir 不在 git 仓库内，只返回 [work_dir]。支持 monorepo 子目录场景。
def _project_instruction_dirs(work_dir: Path) -> list[Path]:
    abs_dir = work_dir.resolve()
    root = _find_git_root(abs_dir)
    if root is None:
        return [abs_dir]

    dirs: list[Path] = []
    cur = abs_dir
    while True:
        dirs.insert(0, cur)
        if cur == root:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return dirs


# 发现并拼接项目和用户指令文件。
#
# 发现顺序（低优先级在前，高优先级在后，拼接用 --- 分隔）：
#   1. 用户全局：~/.seacode/SEACODE.md, ~/.seacode/AGENTS.md
#   2. 项目目录链：从 git root 到 work_dir，每级的 SEACODE.md 和 AGENTS.md
#   3. work_dir/.seacode/INSTRUCTIONS.md（遗留格式兼容）
#   4. work_dir/SEACODE.local.md（本地覆盖）
def load_instructions(project_root: str) -> str:
    root = Path(project_root).resolve()
    home = Path.home()
    seen: set[str] = set()  # 用于文件去重
    sources: list[tuple[str, str]] = []  # (label, content)

    def _add(path: Path) -> None:
        """尝试加载一个指令文件，处理 include 展开。"""
        try:
            abs_path = path.resolve()
            abs_str = str(abs_path)
        except OSError:
            return
        if abs_str in seen:
            return
        if not abs_path.exists() or not abs_path.is_file():
            return
        try:
            data = abs_path.read_text(encoding="utf-8")
        except OSError:
            return
        seen.add(abs_str)
        # 每个文件独立的 include seen 集合，但共享全局文件去重
        include_seen: set[str] = {abs_str}
        content = process_includes(data, abs_path.parent, root, 0, include_seen)

        # 生成标签：尽量用相对路径
        try:
            label = str(abs_path.relative_to(root))
        except ValueError:
            label = abs_str
        sources.append((label, content.rstrip("\n")))

    # 1. 用户全局
    _add(home / ".seacode" / "SEACODE.md")
    _add(home / ".seacode" / "AGENTS.md")

    # 2. 项目目录链
    for d in _project_instruction_dirs(root):
        _add(d / "SEACODE.md")
        _add(d / "AGENTS.md")

    # 3. 遗留格式
    _add(root / ".seacode" / "INSTRUCTIONS.md")

    # 4. 本地覆盖
    _add(root / "SEACODE.local.md")

    if not sources:
        return ""

    parts = [f"Contents of {label}:\n\n{content}" for label, content in sources]
    return "\n\n---\n\n".join(parts)
