"""TraceManager 单元测试：覆盖 create/update/complete/get/get_tree/remove/
complete_all_running/get_total_tokens。
"""

from __future__ import annotations

from seacode.agents.trace import TraceManager

# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


# 验证 create 生成 12 字符 hex 格式的 agent_id。
# 调用 create 后断言 agent_id 长度为 12 且是十六进制字符串。
def test_create_generates_twelve_char_hex_agent_id() -> None:
    tm = TraceManager()
    node = tm.create("Explore", None, "trace1")
    assert len(node.agent_id) == 12
    # 断言是十六进制字符。
    int(node.agent_id, 16)


# 验证 create 设置节点字段。
# 调用 create 传入 agent_type/parent_id/trace_id，断言字段保留且 status 为 running。
def test_create_sets_node_fields() -> None:
    tm = TraceManager()
    node = tm.create("Explore", "parent1", "trace1")
    assert node.agent_type == "Explore"
    assert node.parent_id == "parent1"
    assert node.trace_id == "trace1"
    assert node.status == "running"
    assert node.end_time is None


# 验证 create 生成唯一 agent_id。
# 连续调用 create 两次，断言两次的 agent_id 不同。
def test_create_generates_unique_agent_ids() -> None:
    tm = TraceManager()
    n1 = tm.create("a", None, "t1")
    n2 = tm.create("b", None, "t1")
    assert n1.agent_id != n2.agent_id


# ---------------------------------------------------------------------------
# update
# ------------------------------------------------------------------


# 验证 update 修改节点字段。
# create 后 update input_tokens/output_tokens/tool_calls，断言字段被更新。
def test_update_modifies_node_fields() -> None:
    tm = TraceManager()
    node = tm.create("Explore", None, "trace1")
    tm.update(
        node.agent_id,
        input_tokens=100,
        output_tokens=50,
        tool_calls=3,
    )
    assert node.input_tokens == 100
    assert node.output_tokens == 50
    assert node.tool_calls == 3


# 验证 update 对不存在节点静默返回。
# update 不存在的 agent_id，断言不抛异常。
def test_update_silently_ignores_unknown_agent_id() -> None:
    tm = TraceManager()
    tm.update("nonexistent", input_tokens=100)
    # 不抛异常即通过。


# 验证 update 忽略节点上不存在的字段。
# create 后 update 不存在的字段，断言不抛异常且不影响其它字段。
def test_update_ignores_unknown_fields() -> None:
    tm = TraceManager()
    node = tm.create("Explore", None, "trace1")
    tm.update(node.agent_id, unknown_field=999)
    assert not hasattr(node, "unknown_field")


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


# 验证 complete 设置 end_time 与 status。
# create 后 complete，断言 end_time 非 None 且 status 为 completed。
def test_complete_sets_end_time_and_status() -> None:
    tm = TraceManager()
    node = tm.create("Explore", None, "trace1")
    tm.complete(node.agent_id)
    assert node.end_time is not None
    assert node.status == "completed"


# 验证 complete 自定义 status。
# create 后 complete(status="failed")，断言 status 为 failed。
def test_complete_with_custom_status() -> None:
    tm = TraceManager()
    node = tm.create("Explore", None, "trace1")
    tm.complete(node.agent_id, status="failed")
    assert node.status == "failed"


# 验证 complete 对不存在节点静默返回。
# complete 不存在的 agent_id，断言不抛异常。
def test_complete_silently_ignores_unknown_agent_id() -> None:
    tm = TraceManager()
    tm.complete("nonexistent")
    # 不抛异常即通过。


# ---------------------------------------------------------------------------
# get / get_tree / remove
# ---------------------------------------------------------------------------


# 验证 get 返回节点或 None。
# create 后 get 同一 agent_id 返回节点；get 不存在返回 None。
def test_get_returns_node_or_none() -> None:
    tm = TraceManager()
    node = tm.create("Explore", None, "trace1")
    assert tm.get(node.agent_id) is node
    assert tm.get("nonexistent") is None


# 验证 get_tree 按 trace_id 聚合返回相关节点。
# 创建 3 个节点（2 个 trace_id=trace1、1 个 trace_id=trace2），断言 get_tree 返回正确数量。
def test_get_tree_aggregates_by_trace_id() -> None:
    tm = TraceManager()
    tm.create("a", None, "trace1")
    tm.create("b", "p1", "trace1")
    tm.create("c", None, "trace2")
    tree = tm.get_tree("trace1")
    assert len(tree) == 2
    assert all(n.trace_id == "trace1" for n in tree)


# 验证 get_tree 不存在 trace_id 返回空列表。
# 调用 get_tree 查询不存在的 trace_id，断言返回空列表。
def test_get_tree_returns_empty_for_unknown_trace_id() -> None:
    tm = TraceManager()
    assert tm.get_tree("nonexistent") == []


# 验证 remove 移除节点。
# create 后 remove，再 get 同一 agent_id 返回 None。
def test_remove_deletes_node() -> None:
    tm = TraceManager()
    node = tm.create("Explore", None, "trace1")
    tm.remove(node.agent_id)
    assert tm.get(node.agent_id) is None


# 验证 remove 对不存在节点静默返回。
# remove 不存在的 agent_id，断言不抛异常。
def test_remove_silently_ignores_unknown_agent_id() -> None:
    tm = TraceManager()
    tm.remove("nonexistent")
    # 不抛异常即通过。


# ---------------------------------------------------------------------------
# complete_all_running 与 get_total_tokens
# ---------------------------------------------------------------------------


# 验证 complete_all_running 完成指定父的所有 running 子节点。
# 创建 3 个节点（2 个 parent_id=p1 running、1 个 parent_id=p2 running），断言只完成 p1 的。
def test_complete_all_running_completes_children_of_parent() -> None:
    tm = TraceManager()
    n1 = tm.create("a", "p1", "t1")
    n2 = tm.create("b", "p1", "t1")
    n3 = tm.create("c", "p2", "t1")
    tm.complete_all_running("p1")
    assert n1.status == "completed"
    assert n1.end_time is not None
    assert n2.status == "completed"
    assert n2.end_time is not None
    # p2 的子节点不受影响。
    assert n3.status == "running"


# 验证 complete_all_running 对已完成节点不重复处理。
# 先 complete 一个节点，再 complete_all_running，断言 status 仍为 completed。
def test_complete_all_running_skips_non_running() -> None:
    tm = TraceManager()
    n1 = tm.create("a", "p1", "t1")
    tm.complete(n1.agent_id, status="failed")
    tm.complete_all_running("p1")
    # 已是 failed 的节点不被改写为 completed。
    assert n1.status == "failed"


# 验证 get_total_tokens 合计指定调用链的 input/output token。
# 创建 3 个同 trace_id 节点（input=100/200/300，output=50/100/150），断言合计为 (600, 300)。
def test_get_total_tokens_sums_input_and_output() -> None:
    tm = TraceManager()
    n1 = tm.create("a", None, "t1")
    n2 = tm.create("b", n1.agent_id, "t1")
    n3 = tm.create("c", n1.agent_id, "t1")
    tm.update(n1.agent_id, input_tokens=100, output_tokens=50)
    tm.update(n2.agent_id, input_tokens=200, output_tokens=100)
    tm.update(n3.agent_id, input_tokens=300, output_tokens=150)
    total_in, total_out = tm.get_total_tokens("t1")
    assert total_in == 600
    assert total_out == 300


# 验证 get_total_tokens 不存在 trace_id 返回 (0, 0)。
# 调用 get_total_tokens 查询不存在的 trace_id，断言返回 (0, 0)。
def test_get_total_tokens_returns_zero_for_unknown_trace_id() -> None:
    tm = TraceManager()
    total_in, total_out = tm.get_total_tokens("nonexistent")
    assert total_in == 0
    assert total_out == 0
