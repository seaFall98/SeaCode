"""teams/registry.py 单测：AgentNameRegistry 单例与 resolve 全分支。"""

from __future__ import annotations

from seacode.teams.registry import AgentNameRegistry


# 验证 AgentNameRegistry.instance 单例行为。
# 两次调用 instance 返回同一对象；reset 后再调用返回新实例。
def test_instance_singleton() -> None:
    AgentNameRegistry.instance().reset()
    a = AgentNameRegistry.instance()
    b = AgentNameRegistry.instance()
    assert a is b


# 验证 reset 清空数据但不重建单例。
# reset 后 instance 仍返回同一对象，但已注册的映射被清空。
def test_reset_clears_data() -> None:
    reg = AgentNameRegistry.instance()
    reg.reset()
    reg.register("alice", "id-1")
    assert reg.resolve("alice") == "id-1"
    reg.reset()
    # 单例仍保持，但数据已清空。
    assert AgentNameRegistry.instance() is reg
    assert reg.resolve("alice") is None


# 验证 register 覆盖语义。
# 同 name 两次 register 不同 agent_id，resolve 返回最新 agent_id。
def test_register_overrides() -> None:
    reg = AgentNameRegistry()
    reg.reset()
    reg.register("alice", "id-1")
    reg.register("alice", "id-2")
    assert reg.resolve("alice") == "id-2"


# 验证 resolve 接受 name 返回对应 agent_id。
# register("alice", "id-1") 后 resolve("alice") 返回 "id-1"。
def test_resolve_by_name() -> None:
    reg = AgentNameRegistry()
    reg.reset()
    reg.register("alice", "id-1")
    assert reg.resolve("alice") == "id-1"


# 验证 resolve 接受 agent_id 直接返回。
# register("alice", "id-1") 后 resolve("id-1") 返回 "id-1"。
def test_resolve_by_agent_id() -> None:
    reg = AgentNameRegistry()
    reg.reset()
    reg.register("alice", "id-1")
    assert reg.resolve("id-1") == "id-1"


# 验证 resolve 未知名返回 None。
# 未注册的 name 与 agent_id 都返回 None。
def test_resolve_unknown_returns_none() -> None:
    reg = AgentNameRegistry()
    reg.reset()
    assert reg.resolve("nobody") is None
    assert reg.resolve("unknown-id") is None


# 验证 unregister 清除映射。
# unregister("alice") 后 resolve("alice") 返回 None。
def test_unregister_removes_mapping() -> None:
    reg = AgentNameRegistry()
    reg.reset()
    reg.register("alice", "id-1")
    reg.unregister("alice")
    assert reg.resolve("alice") is None


# 验证 unregister 不存在的 name 静默无操作。
# 对未注册的 name 调用 unregister 不抛异常。
def test_unregister_unknown_silent() -> None:
    reg = AgentNameRegistry()
    reg.reset()
    reg.unregister("nobody")  # 不抛异常
