"""生成带行号的文件编辑 diff 摘要，供 EditFile 输出。"""

from __future__ import annotations

from dataclasses import dataclass

# diff 上下文行数与最大行数限制，防止超大文件产出天量 diff。
_CONTEXT_LINES: int = 3
_MAX_DIFF_LINES: int = 200


@dataclass(frozen=True)
class DiffResult:
    """表示一段 diff 的文本与增删行数。"""

    text: str
    additions: int
    removals: int


def build_diff(old_content: str, new_content: str) -> DiffResult:
    """对比编辑前后的文件内容，生成带行号的 diff 摘要。

    利用编辑只改动中间一小段的特点，从两端找公共前缀/后缀行，
    避免跑通用 LCS/Myers diff 算法，对大文件更快且实现更简单。
    """
    old_lines = old_content.split("\n")
    new_lines = new_content.split("\n")

    prefix_len = 0
    max_prefix = min(len(old_lines), len(new_lines))
    while prefix_len < max_prefix and old_lines[prefix_len] == new_lines[prefix_len]:
        prefix_len += 1

    suffix_len = 0
    max_suffix = max_prefix - prefix_len
    while (
        suffix_len < max_suffix
        and old_lines[len(old_lines) - 1 - suffix_len] == new_lines[len(new_lines) - 1 - suffix_len]
    ):
        suffix_len += 1

    removed_lines = old_lines[prefix_len : len(old_lines) - suffix_len]
    added_lines = new_lines[prefix_len : len(new_lines) - suffix_len]

    context_start = max(0, prefix_len - _CONTEXT_LINES)
    context_before = old_lines[context_start:prefix_len]
    context_end = min(len(old_lines), len(old_lines) - suffix_len + _CONTEXT_LINES)
    context_after = old_lines[len(old_lines) - suffix_len : context_end]

    out: list[str] = []
    old_line_no = context_start + 1
    new_line_no = context_start + 1
    truncated = False

    def push(prefix: str, line_no: int, content: str) -> None:
        nonlocal truncated
        if len(out) >= _MAX_DIFF_LINES:
            truncated = True
            return
        out.append(f"{prefix} {line_no:>4}  {content}")

    for line in context_before:
        push(" ", old_line_no, line)
        old_line_no += 1
        new_line_no += 1
    for line in removed_lines:
        push("-", old_line_no, line)
        old_line_no += 1
    for line in added_lines:
        push("+", new_line_no, line)
        new_line_no += 1
    for line in context_after:
        push(" ", old_line_no, line)
        old_line_no += 1
        new_line_no += 1

    if truncated:
        out.append(f"  … (diff truncated at {_MAX_DIFF_LINES} lines)")

    return DiffResult(text="\n".join(out), additions=len(added_lines), removals=len(removed_lines))
