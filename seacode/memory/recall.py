"""记忆召回：扫描双目录、LLM 选择器挑选相关记忆、渲染 system-reminder。"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_MEMORY_FILES = 200
FRONTMATTER_MAX_LINES = 30
ENTRYPOINT_NAME = "MEMORY.md"
VALID_TYPES = {"user", "feedback", "project", "reference"}

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# 选择器系统提示：让 LLM 按用户查询挑选最多 5 条相关记忆文件名。
SELECTOR_SYSTEM_PROMPT = (
    "You are selecting memories that will be useful to SeaCode as it processes "
    "a user's query. You will be given the user's query and a list of available "
    "memory files with their filenames and descriptions.\n\n"
    "Return a list of filenames for the memories that will clearly be useful to "
    "SeaCode as it processes the user's query (up to 5). Only include memories "
    "that you are certain will be helpful based on their name and description.\n"
    "- If you are unsure if a memory will be useful in processing the user's "
    "query, then do not include it in your list. Be selective and discerning.\n"
    "- If there are no memories in the list that would clearly be useful, feel "
    "free to return an empty list.\n"
    "- If a list of recently-used tools is provided, do not select memories "
    "that are usage reference or API documentation for those tools (SeaCode is "
    "already exercising them). DO still select memories containing warnings, "
    "gotchas, or known issues about those tools — active use is exactly when "
    "those matter.\n\n"
    'Respond with valid JSON only, no markdown, in this exact shape: '
    '{"selected_memories": ["filename1.md", "filename2.md"]}'
)

# 异步选择器类型别名：接收 (system_prompt, user_message)，返回 LLM 原始输出。
SelectorFn = Callable[[str, str], Awaitable[str]]


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class MemoryHeader:
    """一个记忆文件的元信息头，用于 manifest 生成与选择器输入。"""

    filename: str      # 相对 memory_dir 的路径
    file_path: str     # 绝对路径
    scope: str         # "user" 或 "project"
    mtime_ms: int      # 修改时间，毫秒级 epoch
    description: str   # frontmatter description；缺失为 ""
    type: str          # frontmatter type；未识别为 ""


@dataclass
class RelevantMemory:
    """选择器挑选出的相关记忆，承载路径与 mtime 供 render_reminder 使用。"""

    path: str
    mtime_ms: int


# ---------------------------------------------------------------------------
# 记忆年龄辅助
# ---------------------------------------------------------------------------


# 返回 floor 后的"距今 N 天"；今天 0、昨天 1。
def memory_age_days(mtime_ms: int) -> int:
    d = (int(time.time() * 1000) - mtime_ms) // 86_400_000
    return max(d, 0)


# 返回人类可读的年龄字符串：today / yesterday / N days ago。
def memory_age(mtime_ms: int) -> str:
    d = memory_age_days(mtime_ms)
    if d == 0:
        return "today"
    if d == 1:
        return "yesterday"
    return f"{d} days ago"


# 返回过时警告文本：超过 1 天的记忆附新鲜度提醒；新鲜返回空串。
def memory_freshness_text(mtime_ms: int) -> str:
    d = memory_age_days(mtime_ms)
    if d <= 1:
        return ""
    return (
        f"This memory is {d} days old. "
        "Memories are point-in-time observations, not live state — "
        "claims about code behavior or file:line citations may be outdated. "
        "Verify against current code before asserting as fact."
    )


# ---------------------------------------------------------------------------
# Frontmatter 解析
# ---------------------------------------------------------------------------


# 从 YAML-ish frontmatter 中提取 name/description/type。
# 只读取三个已知字段，未知字段忽略；无 frontmatter 返回空字段。
def parse_frontmatter(content: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {"name": "", "description": "", "type": ""}

    block = m.group(1)
    result: dict[str, str] = {"name": "", "description": "", "type": ""}
    for line in block.split("\n"):
        colon = line.find(":")
        if colon < 0:
            continue
        key = line[:colon].strip()
        val = line[colon + 1:].strip()
        # 去除引号
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]
        if key == "name":
            result["name"] = val
        elif key == "description":
            result["description"] = val
        elif key == "type":
            if val in VALID_TYPES:
                result["type"] = val
    return result


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


# 扫描 memory_dir 中的 .md 文件（排除 MEMORY.md），读取 frontmatter，
# 返回 newest-first 排序、最多 MAX_MEMORY_FILES 条的 header 列表。
def scan_memory_files(memory_dir: Path, scope: str) -> list[MemoryHeader]:
    if not memory_dir.is_dir():
        return []

    md_files: list[Path] = []
    try:
        for fp in memory_dir.rglob("*.md"):
            if fp.is_file() and fp.name != ENTRYPOINT_NAME:
                md_files.append(fp)
    except OSError:
        return []

    results: list[MemoryHeader] = []
    for fp in md_files:
        hdr = _read_memory_header(fp, memory_dir, scope)
        if hdr is not None:
            results.append(hdr)

    # newest-first 排序
    results.sort(key=lambda h: h.mtime_ms, reverse=True)
    if len(results) > MAX_MEMORY_FILES:
        results = results[:MAX_MEMORY_FILES]
    return results


# 读取单个记忆文件的 header：mtime + 前 FRONTMATTER_MAX_LINES 行的 frontmatter。
def _read_memory_header(
    file_path: Path, memory_dir: Path, scope: str
) -> MemoryHeader | None:
    try:
        mtime_ms = int(file_path.stat().st_mtime * 1000)
    except OSError:
        return None

    # 仅读取前 FRONTMATTER_MAX_LINES 行做 frontmatter 解析，避免读大文件。
    try:
        lines: list[str] = []
        with file_path.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= FRONTMATTER_MAX_LINES:
                    break
                lines.append(line)
        content = "".join(lines)
    except OSError:
        return None

    fm = parse_frontmatter(content)
    try:
        rel = str(file_path.relative_to(memory_dir))
    except ValueError:
        rel = file_path.name

    return MemoryHeader(
        filename=rel,
        file_path=str(file_path.resolve()),
        scope=scope,
        mtime_ms=mtime_ms,
        description=fm["description"],
        type=fm["type"],
    )


# ---------------------------------------------------------------------------
# Manifest 格式化
# ---------------------------------------------------------------------------


# 把 MemoryHeader 列表格式化为选择器 prompt 用的 manifest 文本。
def format_memory_manifest(memories: list[MemoryHeader]) -> str:
    if not memories:
        return ""
    lines: list[str] = []
    for m in memories:
        scope_tag = f"[{m.scope}-scope] " if m.scope else ""
        type_tag = f"[{m.type}] " if m.type else ""
        ts = datetime.fromtimestamp(
            m.mtime_ms / 1000, tz=UTC
        ).strftime("%Y-%m-%dT%H:%M:%S.") + f"{m.mtime_ms % 1000:03d}Z"
        path = m.file_path if m.file_path else m.filename
        if m.description:
            lines.append(f"- {scope_tag}{type_tag}{path} ({ts}): {m.description}")
        else:
            lines.append(f"- {scope_tag}{type_tag}{path} ({ts})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 查找相关记忆
# ---------------------------------------------------------------------------


# 扫描双目录、过滤已 surfaced、调用选择器挑选最多 5 条相关文件名。
# 选择器失败静默返回 []；recall 是 best-effort，绝不阻塞主对话。
async def find_relevant_memories(
    query: str,
    user_mem_dir: Path | None,
    project_mem_dir: Path | None,
    recent_tools: list[str] | None,
    already_surfaced: set[str] | None,
    selector: SelectorFn,
) -> list[RelevantMemory]:
    all_headers: list[MemoryHeader] = []
    if user_mem_dir is not None:
        all_headers.extend(scan_memory_files(user_mem_dir, "user"))
    if project_mem_dir is not None:
        all_headers.extend(scan_memory_files(project_mem_dir, "project"))

    surfaced = already_surfaced or set()
    candidates = [m for m in all_headers if m.file_path not in surfaced]
    if not candidates:
        return []

    selected_filenames = await _select_relevant_memories(
        query, candidates, recent_tools, selector
    )

    # 同时按 file_path 和 filename 建索引，选择器返回任一形式都能命中。
    by_key: dict[str, MemoryHeader] = {}
    for m in candidates:
        by_key[m.file_path] = m
        by_key.setdefault(m.filename, m)

    result: list[RelevantMemory] = []
    for fn in selected_filenames:
        mem = by_key.get(fn)
        if mem is not None:
            result.append(RelevantMemory(path=mem.file_path, mtime_ms=mem.mtime_ms))
    return result


# 格式化 manifest、调用选择器、解析 JSON、返回有效文件名列表。
async def _select_relevant_memories(
    query: str,
    memories: list[MemoryHeader],
    recent_tools: list[str] | None,
    selector: SelectorFn,
) -> list[str]:
    valid_filenames = {m.filename for m in memories}

    manifest = format_memory_manifest(memories)

    tools_section = ""
    if recent_tools:
        tools_section = "\n\nRecently used tools: " + ", ".join(recent_tools)

    user_message = f"Query: {query}\n\nAvailable memories:\n{manifest}{tools_section}"

    try:
        raw = await selector(SELECTOR_SYSTEM_PROMPT, user_message)
    except Exception:
        return []

    clean = _extract_json_object(raw)
    if not clean:
        return []

    try:
        parsed = json.loads(clean)
        arr = parsed.get("selected_memories", [])
        if not isinstance(arr, list):
            return []
        return [f for f in arr if isinstance(f, str) and f in valid_filenames]
    except (json.JSONDecodeError, AttributeError):
        return []


# 从原始输出中提取第一个 {...} 子串；容忍 markdown 围栏或前后散文。
def _extract_json_object(raw: str) -> str:
    trimmed = raw.strip()
    if trimmed.startswith("{"):
        return trimmed
    start = trimmed.find("{")
    if start < 0:
        return ""
    end = trimmed.rfind("}")
    if end < start:
        return ""
    return trimmed[start: end + 1]


# ---------------------------------------------------------------------------
# Reminder 渲染
# ---------------------------------------------------------------------------


# 读取每条相关记忆的全文，附加新鲜度警告，渲染为 system-reminder body。
def render_reminder(memories: list[RelevantMemory]) -> str:
    if not memories:
        return ""

    parts: list[str] = []
    parts.append("The following relevant memories from prior conversations may help:\n")
    for mem in memories:
        try:
            content = Path(mem.path).read_text(encoding="utf-8")
        except OSError:
            continue  # 跳过不可读文件
        basename = Path(mem.path).name
        parts.append(f"## Memory: {basename} (saved {memory_age(mem.mtime_ms)})\n")
        note = memory_freshness_text(mem.mtime_ms)
        if note:
            parts.append(note + "\n")
        parts.append(content + "\n\n---\n")
    return "\n".join(parts)
