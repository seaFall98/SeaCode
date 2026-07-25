# TeammateTree：Textual Widget，按 team-lead + 子节点树形展示 teammates 进度。
"""TUI 队友树 widget；周期刷新展示 team-lead 与 teammates 的运行时状态。"""

from __future__ import annotations

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from seacode.teams.progress import TeammateProgress


# 把 token 数格式化为短字符串：1M+ 用 M、1k+ 用 k、否则原样。
# 与 TeammateProgress.format_tokens 同语义，独立实现避免对 progress 实例的依赖。
def _format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


class TeammateTree(Widget):
    """团队树 widget：顶部 team-lead 行 + 子节点 teammates 进度行。"""

    DEFAULT_CSS = """\
TeammateTree {
    height: auto;
    margin: 0 1;
}
"""

    # reactive 字段：teammates 列表与 lead 累计 token 数；变化时触发重渲染。
    teammates: reactive[list[TeammateProgress]] = reactive(list, layout=True)
    leader_tokens: reactive[int] = reactive(0)

    # 渲染 team-lead 顶行 + teammates 子节点行；空列表返回空 Text。
    def render(self) -> Text:
        if not self.teammates:
            return Text()

        text = Text()
        # team-lead 顶行：bold blue 名称 + dim token 累计提示。
        text.append("team-lead", style="bold blue")
        text.append(
            f" thinking… ({_format_tokens(self.leader_tokens)} tokens)\n",
            style="dim",
        )

        # teammates 子节点：最后一个用 └─，其余用 ├─。
        for i, progress in enumerate(self.teammates):
            is_last = i == len(self.teammates) - 1
            prefix = "└─ " if is_last else "├─ "
            text.append(prefix)

            # 名称 bold，状态按字段着色或显示活动摘要。
            text.append(f"@{progress.name} ", style="bold")
            if progress.status == "completed":
                text.append("completed", style="green")
            elif progress.status == "failed":
                text.append("failed", style="red")
            elif progress.status == "idle":
                text.append("idle", style="dim")
            elif progress.status == "stopped":
                text.append("stopped", style="yellow")
            else:
                text.append(progress.activity_summary(), style="cyan")

            # 工具调用计数与 token 累计 dim 显示。
            text.append(
                f" [{progress.tool_use_count} tools, {progress.format_tokens()}]",
                style="dim",
            )
            if not is_last:
                text.append("\n")

        return text
