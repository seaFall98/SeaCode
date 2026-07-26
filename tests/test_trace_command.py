"""/trace 命令单元测试：覆盖调用树渲染、空状态、合计与别名。"""

from __future__ import annotations

import time

from seacode.agents.trace import TraceManager, TraceNode
from seacode.commands.handlers.trace import (
    _format_trace_status,
    create_trace_command,
    create_trace_handler,
)
from seacode.commands.registry import CommandContext


# 实现 UIController 协议的假对象：记录 add_system_message 调用供断言。
class _FakeUI:
    def __init__(self) -> None:
        self.system_messages: list[str] = []

    def add_system_message(self, text: str) -> None:
        self.system_messages.append(text)

    def send_user_message(self, text: str) -> None:
        del text

    def set_plan_mode(self, enabled: bool) -> None:
        del enabled

    def get_token_count(self) -> tuple[int, int]:
        return (0, 0)

    def refresh_status(self) -> None:
        return None


# 构造 CommandContext；args 为空，ui 捕获输出。agent 默认 None，可注入用于 Lead 显示测试。
def _make_ctx(
    ui: _FakeUI | None = None,
    agent: object | None = None,
) -> CommandContext:
    return CommandContext(
        args="",
        agent=agent,  # type: ignore[arg-type]
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui if ui is not None else _FakeUI(),
        config=None,
    )


# 直接往 TraceManager 插入预设 TraceNode，避免依赖 create 生成随机 id。
def _seed_node(
    trm: TraceManager,
    *,
    agent_id: str,
    agent_type: str = "Explore",
    parent_id: str | None = None,
    trace_id: str = "trace1",
    status: str = "completed",
    input_tokens: int = 0,
    output_tokens: int = 0,
    start_time: float | None = None,
    end_time: float | None = None,
) -> TraceNode:
    node = TraceNode(
        agent_id=agent_id,
        agent_type=agent_type,
        parent_id=parent_id,
        trace_id=trace_id,
        status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        start_time=start_time if start_time is not None else time.time(),
        end_time=end_time,
    )
    trm._nodes[agent_id] = node  # type: ignore[assignment]
    return node


# ---------------------------------------------------------------------------
# _format_trace_status
# ---------------------------------------------------------------------------


# 验证 _format_trace_status 四种状态图标映射。
# 直接断言 running/completed/failed/cancelled 对应图标。
def test_format_trace_status_four_icons() -> None:
    assert _format_trace_status("running") == "⏳"
    assert _format_trace_status("completed") == "✓"
    assert _format_trace_status("failed") == "✗"
    assert _format_trace_status("cancelled") == "⊘"


# 验证 _format_trace_status 未知状态返回问号。
# 直接断言未知状态返回 "?"。
def test_format_trace_status_unknown_returns_question() -> None:
    assert _format_trace_status("unknown") == "?"


# ---------------------------------------------------------------------------
# /trace 渲染
# ---------------------------------------------------------------------------


# 验证 /trace 渲染调用树含 agent_type / agent_id / 状态图标 / 耗时 / token。
# 构造 root + 2 children，断言输出含关键信息。
async def test_trace_renders_call_tree() -> None:
    trm = TraceManager()
    now = time.time()
    _seed_node(
        trm,
        agent_id="root00123456",
        agent_type="main",
        parent_id=None,
        trace_id="trace1",
        status="completed",
        input_tokens=100,
        output_tokens=50,
        start_time=now - 5,
        end_time=now,
    )
    _seed_node(
        trm,
        agent_id="child1234567",
        agent_type="Explore",
        parent_id="root00123456",
        trace_id="trace1",
        status="completed",
        input_tokens=200,
        output_tokens=100,
        start_time=now - 3,
        end_time=now,
    )
    handler = create_trace_handler(trm, lead_agent_id="root00123456")
    ui = _FakeUI()
    ctx = _make_ctx(ui=ui)

    await handler(ctx)

    assert len(ui.system_messages) == 1
    output = ui.system_messages[0]
    assert "main" in output
    assert "Explore" in output
    assert "root00123456"[:8] in output
    assert "✓" in output
    assert "↑100" in output
    assert "↓50" in output


# 验证 /trace 空状态显示 "没有 Agent 追踪记录"。
# 空 TraceManager，断言输出 == "没有 Agent 追踪记录"。
async def test_trace_empty_returns_message() -> None:
    trm = TraceManager()
    handler = create_trace_handler(trm, lead_agent_id=None)
    ui = _FakeUI()
    ctx = _make_ctx(ui=ui)

    await handler(ctx)

    assert ui.system_messages == ["没有 Agent 追踪记录"]


# 验证 /trace 合计 agents 数与 token 总量。
# 构造 3 个节点同 trace_id，断言输出含 "3 个 Agent" 与 token 合计。
async def test_trace_shows_totals() -> None:
    trm = TraceManager()
    _seed_node(
        trm,
        agent_id="node00000001",
        agent_type="main",
        parent_id=None,
        trace_id="trace1",
        input_tokens=100,
        output_tokens=50,
    )
    _seed_node(
        trm,
        agent_id="node00000002",
        agent_type="Explore",
        parent_id="node00000001",
        trace_id="trace1",
        input_tokens=200,
        output_tokens=100,
    )
    _seed_node(
        trm,
        agent_id="node00000003",
        agent_type="Plan",
        parent_id="node00000001",
        trace_id="trace1",
        input_tokens=300,
        output_tokens=150,
    )
    handler = create_trace_handler(trm, lead_agent_id=None)
    ui = _FakeUI()
    ctx = _make_ctx(ui=ui)

    await handler(ctx)

    output = ui.system_messages[0]
    assert "3 个 Agent" in output
    # input_tokens 合计 100+200+300=600；output_tokens 合计 50+100+150=300。
    assert "↑600" in output
    assert "↓300" in output


# 验证 /trace 状态图标渲染三种状态。
# 构造 running/completed/failed 节点，断言三种图标都出现。
async def test_trace_renders_status_icons() -> None:
    trm = TraceManager()
    _seed_node(
        trm,
        agent_id="node00000001",
        agent_type="main",
        parent_id=None,
        trace_id="trace1",
        status="running",
    )
    _seed_node(
        trm,
        agent_id="node00000002",
        agent_type="Explore",
        parent_id="node00000001",
        trace_id="trace1",
        status="completed",
    )
    _seed_node(
        trm,
        agent_id="node00000003",
        agent_type="Plan",
        parent_id="node00000001",
        trace_id="trace1",
        status="failed",
    )
    handler = create_trace_handler(trm, lead_agent_id=None)
    ui = _FakeUI()
    ctx = _make_ctx(ui=ui)

    await handler(ctx)

    output = ui.system_messages[0]
    assert "⏳" in output
    assert "✓" in output
    assert "✗" in output


# 验证 /trace 渲染嵌套子节点缩进。
# 构造 root + child + grandchild，断言 child 行比 root 行缩进更多。
async def test_trace_renders_nested_indentation() -> None:
    trm = TraceManager()
    _seed_node(
        trm,
        agent_id="root000000001",
        agent_type="main",
        parent_id=None,
        trace_id="trace1",
    )
    _seed_node(
        trm,
        agent_id="child00000001",
        agent_type="Explore",
        parent_id="root000000001",
        trace_id="trace1",
    )
    handler = create_trace_handler(trm, lead_agent_id=None)
    ui = _FakeUI()
    ctx = _make_ctx(ui=ui)

    await handler(ctx)

    output = ui.system_messages[0]
    lines = output.split("\n")
    # 找到含 "main" 和 "Explore" 的行。
    main_line = next((ln for ln in lines if "main" in ln), "")
    explore_line = next((ln for ln in lines if "Explore" in ln), "")
    # 子节点行应有前导空格缩进。
    assert explore_line.startswith("  ")
    assert not main_line.startswith("  ")


# ---------------------------------------------------------------------------
# create_trace_command
# ---------------------------------------------------------------------------


# 验证 create_trace_command 返回含 tree 别名的 Command。
# 构造 command，断言 aliases 含 "tree"。
def test_create_trace_command_has_tree_alias() -> None:
    trm = TraceManager()
    cmd = create_trace_command(trm, lead_agent_id=None)
    assert cmd.name == "trace"
    assert "tree" in cmd.aliases


# 验证 create_trace_command 返回的 Command 含 handler。
# 构造 command，断言 handler 可调用。
def test_create_trace_command_has_handler() -> None:
    trm = TraceManager()
    cmd = create_trace_command(trm, lead_agent_id=None)
    assert cmd.handler is not None
    assert callable(cmd.handler)


# 验证 /trace 无 lead_agent_id 也能正常渲染。
# lead_agent_id=None，构造单节点，断言输出含节点信息。
async def test_trace_renders_without_lead_agent_id() -> None:
    trm = TraceManager()
    _seed_node(
        trm,
        agent_id="root000000001",
        agent_type="main",
        parent_id=None,
        trace_id="trace1",
        status="completed",
        input_tokens=10,
        output_tokens=5,
    )
    handler = create_trace_handler(trm, lead_agent_id=None)
    ui = _FakeUI()
    ctx = _make_ctx(ui=ui)

    await handler(ctx)

    output = ui.system_messages[0]
    assert "main" in output
    assert "1 个 Agent" in output


# 验证 /trace 在 ctx.agent 存在时显示 Lead agent_id 前缀。
# 构造带 agent_id 的 agent，断言输出含 "Lead:" 与 agent_id 前 8 位。
async def test_trace_displays_lead_agent_id() -> None:
    trm = TraceManager()
    _seed_node(
        trm,
        agent_id="root000000001",
        agent_type="main",
        parent_id=None,
        trace_id="trace1",
        status="completed",
    )

    class _FakeAgent:
        agent_id = "leadabcd1234"

    handler = create_trace_handler(trm, lead_agent_id=None)
    ui = _FakeUI()
    ctx = _make_ctx(ui=ui, agent=_FakeAgent())

    await handler(ctx)

    output = ui.system_messages[0]
    assert "Lead: leadabcd" in output
