"""teams/coordinator.py 单测：模式判定、session 匹配、提示词构造与 user context。"""

from __future__ import annotations

from seacode.teams.coordinator import (
    get_coordinator_system_prompt,
    get_coordinator_user_context,
    is_coordinator_mode,
    match_session_mode,
)


# 验证 is_coordinator_mode 按布尔开关返回。
# True 返回 True，False 返回 False。
def test_is_coordinator_mode() -> None:
    assert is_coordinator_mode(True) is True
    assert is_coordinator_mode(False) is False


# 验证 match_session_mode 在 coordinator + enabled / coordinator + disabled / 其它 三种场景的返回。
# 三条分支分别断言 (是否启用, 说明文案)。
def test_match_session_mode_branches() -> None:
    enabled = match_session_mode("coordinator", True)
    assert enabled == (True, "已从 session 恢复 Coordinator 模式")

    disabled = match_session_mode("coordinator", False)
    assert disabled[0] is False
    assert "回退普通模式" in disabled[1]

    other = match_session_mode("", True)
    assert other == (False, "")

    other2 = match_session_mode("normal", True)
    assert other2 == (False, "")


# 验证 get_coordinator_system_prompt 含 6 节标题。
# 断言所有 ## 标题出现。
def test_coordinator_system_prompt_sections() -> None:
    prompt = get_coordinator_system_prompt()
    assert "## Your Role" in prompt
    assert "## Your Tools" in prompt
    assert "## Workers" in prompt
    assert "## Task Workflow" in prompt
    assert "## Writing Worker Prompts" in prompt
    assert "## Example Session" in prompt


# 验证 get_coordinator_system_prompt 默认含 general-purpose 与 Verification。
# 不传 catalog 时使用 DEFAULT_AGENT_CATALOG。
def test_coordinator_system_prompt_default_catalog() -> None:
    prompt = get_coordinator_system_prompt()
    assert "general-purpose" in prompt
    assert "Verification" in prompt


# 验证 get_coordinator_system_prompt 接受自定义 catalog。
# 传入 [("custom", "custom desc")]，断言含 custom 与 custom desc。
def test_coordinator_system_prompt_custom_catalog() -> None:
    prompt = get_coordinator_system_prompt([("custom", "custom desc")])
    assert "custom" in prompt
    assert "custom desc" in prompt
    # Workers 段落应含 custom 类型。
    assert "- custom: custom desc" in prompt


# 验证 get_coordinator_user_context 格式。
# 传入 ["ReadFile", "Bash"]，断言返回 dict 含工具名拼接。
def test_get_coordinator_user_context() -> None:
    ctx = get_coordinator_user_context(["ReadFile", "Bash"])
    assert "workerToolsContext" in ctx
    assert "ReadFile" in ctx["workerToolsContext"]
    assert "Bash" in ctx["workerToolsContext"]
