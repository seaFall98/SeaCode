"""命令解析与补全：识别 / 开头的斜杠命令并按前缀匹配。"""

from __future__ import annotations

from .registry import Command, CommandRegistry

# 补全弹窗最多显示的条目数。
MAX_COMPLETION_ITEMS = 8


# 解析输入文本：识别 / 开头、切分命令名与参数、命令名小写化。
# 返回 (name, args, is_command) 三元组。
def parse_command(text: str) -> tuple[str, str, bool]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return ("", "", False)
    body = stripped[1:]
    if not body:
        return ("", "", True)
    parts = body.split(None, 1)
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return (name, args, True)


# 按前缀匹配命令名或别名，返回 (display, value) 对列表。
# display 为格式化的显示文本，value 为 /命令名 + 空格 用于补全填入并继续输入参数。
# 主名与别名同时匹配时只保留一条，按命令名去重；最多返回 MAX_COMPLETION_ITEMS 条。
def complete(registry: CommandRegistry, prefix: str) -> list[tuple[str, str]]:
    if not prefix.startswith("/"):
        return []
    query = prefix[1:].lower()
    seen: set[str] = set()
    matches: list[Command] = []
    for cmd in registry.list_commands():
        if cmd.name in seen:
            continue
        candidates = [cmd.name] + list(cmd.aliases)
        if any(c.lower().startswith(query) for c in candidates):
            seen.add(cmd.name)
            matches.append(cmd)
    matches.sort(key=lambda c: c.name)
    result: list[tuple[str, str]] = []
    for cmd in matches[:MAX_COMPLETION_ITEMS]:
        desc = cmd.description
        if len(desc) > 30:
            desc = desc[:28] + "…"
        desc = desc.replace("[", r"\[")
        display = f"/{cmd.name} - {desc}"
        result.append((display, f"/{cmd.name} "))
    return result
