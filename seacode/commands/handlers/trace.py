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
    now = time.monotonic()
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


# 构造 /trace handler；闭包仅捕获 trace_manager，lead_agent_id 从 ctx.agent 动态读取。
# 之所以不闭包捕获 lead_agent_id，是因为 SeaCode 每回合重建 Agent，
# 闭包捕获会让 lead_agent_id 永远停留在首次注册时的值。
def create_trace_handler(trace_manager: TraceManager, lead_agent_id: str | None) -> Any:
    # lead_agent_id 仅作为向后兼容的占位参数；实际运行时从 ctx.agent.agent_id 取值。
    del lead_agent_id

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
        lines = ["Agent 追踪树:"]
        # Lead agent 标识显示在树顶，让用户知道当前主 Agent 的 agent_id。
        # 每回合重建 Agent，所以从 ctx.agent 动态读取而非闭包捕获。
        agent = getattr(ctx, "agent", None)
        if agent is not None:
            lead_id = getattr(agent, "agent_id", "")
            if lead_id:
                lines.append(f"  Lead: {lead_id[:8]}")
        # 根节点 depth=0 不缩进，子节点逐层缩进。
        tree_text = "\n".join(_render_node(r, nodes, 0) for r in roots)
        lines.append(tree_text)

        # 合计 token：取第一个根节点的 trace_id（单调用链场景）。
        if roots:
            total_in, total_out = trace_manager.get_total_tokens(roots[0].trace_id)
        else:
            total_in, total_out = 0, 0

        ctx.ui.add_system_message(
            "\n".join(lines)
            + f"\n合计 {len(nodes)} 个 Agent，"
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
