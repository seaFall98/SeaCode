"""/trace 命令：渲染 Agent 父子调用树。

按 ``parent_id`` 递归渲染调用树；根节点为 ``parent_id is None or parent_id not in
trace_manager._nodes``。显示状态图标（⏳ / ✓ / ✗）、耗时、Token、合计 agents 数与
token 总量。
"""

from __future__ import annotations

import time
from typing import Any

from seacode.agents.trace import TraceManager
from seacode.commands.registry import Command, CommandContext, CommandType


# 调用树节点状态图标；cancelled 用 ⊘，未知用 ?。
def _format_trace_status(status: str) -> str:
    return {
        "running": "⏳",
        "completed": "✓",
        "failed": "✗",
        "cancelled": "⊘",
    }.get(status, "?")


# 递归渲染调用树节点；depth 控制缩进。
def _render_node(node: Any, all_nodes: list[Any], depth: int = 0) -> str:
    now = time.time()
    elapsed = (node.end_time or now) - node.start_time
    icon = _format_trace_status(node.status)
    indent = "  " * depth
    line = (
        f"{indent}{icon} {node.agent_type} ({node.agent_id[:8]}) "
        f"{elapsed:.1f}s ↑{node.input_tokens} ↓{node.output_tokens}"
    )
    children = [n for n in all_nodes if n.parent_id == node.agent_id]
    if children:
        child_lines = [
            _render_node(c, all_nodes, depth + 1) for c in children
        ]
        return line + "\n" + "\n".join(child_lines)
    return line


# 构造 /trace handler；闭包捕获 trace_manager 与 lead_agent_id。
def create_trace_handler(trace_manager: TraceManager, lead_agent_id: str | None) -> Any:
    async def handler(ctx: CommandContext) -> None:
        nodes = list(trace_manager._nodes.values())
        if not nodes:
            ctx.ui.add_system_message("没有 Agent 追踪记录")
            return

        # 根节点：parent_id 为 None 或指向不存在节点（外部根）。
        node_ids = {n.agent_id for n in nodes}
        roots = [
            n for n in nodes
            if n.parent_id is None or n.parent_id not in node_ids
        ]
        tree_text = "\n".join(_render_node(r, nodes, 0) for r in roots)

        # 合计 token：取第一个根节点的 trace_id（单调用链场景）。
        if roots:
            total_in, total_out = trace_manager.get_total_tokens(roots[0].trace_id)
        else:
            total_in, total_out = 0, 0

        ctx.ui.add_system_message(
            f"{tree_text}\n合计 {len(nodes)} 个 Agent，"
            f"↑{total_in} ↓{total_out} tokens"
        )

    return handler


# 构造 /trace 命令定义；别名 tree。
def create_trace_command(
    trace_manager: TraceManager, lead_agent_id: str | None
) -> Command:
    return Command(
        name="trace",
        description="显示 Agent 调用链",
        type=CommandType.LOCAL,
        handler=create_trace_handler(trace_manager, lead_agent_id),
        aliases=["tree"],
        usage="/trace",
        arg_prompt="",
    )
