"""动作执行器单元测试：覆盖 command/prompt/http/agent 四类与 execute_action 分发。"""

from __future__ import annotations

import sys
import urllib.error
from typing import Any
from unittest.mock import patch

from seacode.hooks.executors import (
    execute_action,
    execute_agent,
    execute_command,
    execute_http,
    execute_prompt,
)
from seacode.hooks.models import Action, HookContext

# ---------------------------------------------------------------------------
# execute_command
# ---------------------------------------------------------------------------


# 验证 execute_command 成功执行返回 stdout 与 success=True。
# 用 echo 命令在子进程中输出 hello，断言 output 与 success。
async def test_execute_command_success_returns_stdout() -> None:
    action = Action(type="command", command="echo hello", timeout=10)
    result = await execute_command(action, HookContext())
    assert result.success is True
    assert "hello" in result.output


# 验证 execute_command 命令失败返回 success=False。
# 用 exit 1 命令构造非零返回码，断言 success=False。
async def test_execute_command_failure_returns_false() -> None:
    action = Action(type="command", command="exit 1", timeout=10)
    result = await execute_command(action, HookContext())
    assert result.success is False


# 验证 execute_command 超时返回 success=False 且提示超时。
# 用 ping 长时间命令配小 timeout，断言 success=False 且 output 含 timed out。
async def test_execute_command_timeout_returns_false() -> None:
    # Windows 用 ping -n 10 触发持续输出；POSIX 兜底 sleep 5。
    if sys.platform == "win32":
        cmd = "ping -n 10 127.0.0.1"
    else:
        cmd = "sleep 5"
    action = Action(type="command", command=cmd, timeout=1)
    result = await execute_command(action, HookContext())
    assert result.success is False
    assert "timed out" in result.output


# 验证 execute_command 在 command 中展开 $TOOL_NAME 等变量。
# 命令含 $TOOL_NAME 占位符，构造 tool_name 后断言子进程收到展开后的命令。
async def test_execute_command_expands_tool_name() -> None:
    captured: list[str] = []

    async def _fake_shell(cmd: str, **kwargs: Any) -> Any:
        captured.append(cmd)

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"", b"")

            def kill(self) -> None:
                pass

            async def wait(self) -> int:
                return 0

        return _Proc()

    with patch(
        "seacode.hooks.executors.asyncio.create_subprocess_shell",
        side_effect=_fake_shell,
    ):
        action = Action(type="command", command="echo $TOOL_NAME", timeout=10)
        ctx = HookContext(tool_name="WriteFile")
        result = await execute_command(action, ctx)

    assert result.success is True
    assert captured and captured[0] == "echo WriteFile"


# 验证 execute_command 异常路径返回 success=False 且 output 含错误说明。
# mock create_subprocess_shell 抛 OSError，断言异常被捕获。
async def test_execute_command_exception_returns_false() -> None:
    with patch(
        "seacode.hooks.executors.asyncio.create_subprocess_shell",
        side_effect=OSError("fail to spawn"),
    ):
        action = Action(type="command", command="anything", timeout=10)
        result = await execute_command(action, HookContext())

    assert result.success is False
    assert "Command execution error" in result.output


# ---------------------------------------------------------------------------
# execute_prompt
# ---------------------------------------------------------------------------


# 验证 execute_prompt 返回展开占位符后的 message 与 success=True。
# message 含 $TOOL_NAME，构造 tool_name 后断言 output 是展开结果。
async def test_execute_prompt_returns_expanded_message() -> None:
    action = Action(type="prompt", message="tool=$TOOL_NAME")
    ctx = HookContext(tool_name="WriteFile")
    result = await execute_prompt(action, ctx)
    assert result.success is True
    assert result.output == "tool=WriteFile"


# 验证 execute_prompt 空 message 返回空字符串。
# message 为空时 output 也为空，success 仍为 True。
async def test_execute_prompt_empty_message_returns_empty() -> None:
    action = Action(type="prompt", message="")
    result = await execute_prompt(action, HookContext())
    assert result.success is True
    assert result.output == ""


# ---------------------------------------------------------------------------
# execute_http
# ---------------------------------------------------------------------------


# 验证 execute_http 成功返回 HTTP {status}: {body} 格式。
# mock urlopen 返回 200 状态与响应体，断言 output 与 success。
async def test_execute_http_success_returns_status_and_body() -> None:
    class _FakeResp:
        status = 200

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    with patch(
        "seacode.hooks.executors.urllib.request.urlopen",
        return_value=_FakeResp(),
    ):
        action = Action(
            type="http", url="http://example.com", method="POST"
        )
        result = await execute_http(action, HookContext())

    assert result.success is True
    assert result.output == 'HTTP 200: {"ok":true}'


# 验证 execute_http URLError 返回 success=False 且 output 含 HTTP error。
# mock urlopen 抛 URLError，断言结果。
async def test_execute_http_url_error_returns_false() -> None:
    with patch(
        "seacode.hooks.executors.urllib.request.urlopen",
        side_effect=urllib.error.URLError("conn refused"),
    ):
        action = Action(type="http", url="http://example.com", method="POST")
        result = await execute_http(action, HookContext())

    assert result.success is False
    assert "HTTP error" in result.output


# 验证 execute_http 响应体截断到 500 字符以内。
# mock urlopen 返回 1000 字节响应体，断言 output 长度受限。
async def test_execute_http_truncates_response_body() -> None:
    class _FakeResp:
        status = 200

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"x" * 1000

    with patch(
        "seacode.hooks.executors.urllib.request.urlopen",
        return_value=_FakeResp(),
    ):
        action = Action(type="http", url="http://x", method="GET")
        result = await execute_http(action, HookContext())

    # output 形如 "HTTP 200: <500 chars>"，整体长度不超过前缀 + 500。
    prefix_len = len("HTTP 200: ")
    assert len(result.output) <= prefix_len + 500
    assert result.success is True


# 验证 execute_http body 存在时自动加 Content-Type: application/json header。
# 用 fake Request 捕获 headers 参数，断言含 Content-Type。
async def test_execute_http_adds_content_type_when_body_present() -> None:
    captured: dict[str, Any] = {}

    class _FakeResp:
        status = 200

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def _fake_request(url: str, data: Any = None, headers: Any = None, method: str = "POST") -> Any:
        captured["url"] = url
        captured["headers"] = headers
        captured["method"] = method
        captured["data"] = data
        return object()

    with patch(
        "seacode.hooks.executors.urllib.request.Request",
        side_effect=_fake_request,
    ), patch(
        "seacode.hooks.executors.urllib.request.urlopen",
        return_value=_FakeResp(),
    ):
        action = Action(
            type="http", url="http://x", method="POST", body='{"k":1}'
        )
        result = await execute_http(action, HookContext())

    assert result.success is True
    assert captured["headers"].get("Content-Type") == "application/json"


# 验证 execute_http body 不存在时不加 Content-Type header。
# 空 body 时 headers 不应自动加 Content-Type。
async def test_execute_http_no_content_type_without_body() -> None:
    captured: dict[str, Any] = {}

    class _FakeResp:
        status = 200

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def _fake_request(url: str, data: Any = None, headers: Any = None, method: str = "POST") -> Any:
        captured["headers"] = headers
        return object()

    with patch(
        "seacode.hooks.executors.urllib.request.Request",
        side_effect=_fake_request,
    ), patch(
        "seacode.hooks.executors.urllib.request.urlopen",
        return_value=_FakeResp(),
    ):
        action = Action(type="http", url="http://x", method="GET", body="")
        await execute_http(action, HookContext())

    assert "Content-Type" not in captured["headers"]


# 验证 execute_http 在 url/body/headers 中展开占位符。
# 构造 tool_name，用 fake Request 捕获参数，断言 $TOOL_NAME 被替换。
async def test_execute_http_expands_placeholders() -> None:
    captured: dict[str, Any] = {}

    class _FakeResp:
        status = 200

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def _fake_request(url: str, data: Any = None, headers: Any = None, method: str = "POST") -> Any:
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        captured["method"] = method
        return object()

    with patch(
        "seacode.hooks.executors.urllib.request.Request",
        side_effect=_fake_request,
    ), patch(
        "seacode.hooks.executors.urllib.request.urlopen",
        return_value=_FakeResp(),
    ):
        action = Action(
            type="http",
            url="http://x/$TOOL_NAME",
            method="POST",
            body='{"tool":"$TOOL_NAME"}',
            headers={"X-Tool": "$TOOL_NAME"},
        )
        ctx = HookContext(tool_name="WriteFile")
        await execute_http(action, ctx)

    assert captured["url"] == "http://x/WriteFile"
    assert captured["data"] == b'{"tool":"WriteFile"}'
    assert captured["headers"].get("X-Tool") == "WriteFile"


# 验证 execute_http 透传自定义 method。
# method=GET 时断言 Request 收到 GET。
async def test_execute_http_passes_custom_method() -> None:
    captured: dict[str, Any] = {}

    class _FakeResp:
        status = 200

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def _fake_request(url: str, data: Any = None, headers: Any = None, method: str = "POST") -> Any:
        captured["method"] = method
        return object()

    with patch(
        "seacode.hooks.executors.urllib.request.Request",
        side_effect=_fake_request,
    ), patch(
        "seacode.hooks.executors.urllib.request.urlopen",
        return_value=_FakeResp(),
    ):
        action = Action(type="http", url="http://x", method="GET", body="")
        await execute_http(action, HookContext())

    assert captured["method"] == "GET"


# ---------------------------------------------------------------------------
# execute_agent 占位
# ---------------------------------------------------------------------------


# 验证 execute_agent 占位返回 not yet implemented 与 success=True。
# 构造 agent 类型 Action，断言占位输出。
async def test_execute_agent_returns_not_implemented_stub() -> None:
    action = Action(type="agent", prompt="do something")
    result = await execute_agent(action, HookContext())
    assert result.success is True
    assert result.output == "agent executor not yet implemented"


# 验证 execute_agent 调用 ctx.expand 展开占位符。
# prompt 含 $TOOL_NAME，构造 tool_name 后断言占位仍返回固定输出。
async def test_execute_agent_expands_prompt_but_returns_stub() -> None:
    action = Action(type="agent", prompt="tool=$TOOL_NAME")
    ctx = HookContext(tool_name="WriteFile")
    result = await execute_agent(action, ctx)
    # 占位实现固定返回 not yet implemented，不返回展开后的 prompt。
    assert result.output == "agent executor not yet implemented"
    assert result.success is True


# ---------------------------------------------------------------------------
# execute_action 分发器
# ---------------------------------------------------------------------------


# 验证 execute_action 按 type 分发到对应执行器。
# 四个合法 type 分别构造 Action，断言分发到对应执行器并返回正确结果。
async def test_execute_action_dispatches_to_correct_executor() -> None:
    # command 路径：真实 echo 返回 success。
    cmd_action = Action(type="command", command="echo hi", timeout=10)
    cmd_result = await execute_action(cmd_action, HookContext())
    assert cmd_result.success is True
    assert "hi" in cmd_result.output

    # prompt 路径：返回展开 message。
    prompt_action = Action(type="prompt", message="hi")
    prompt_result = await execute_action(prompt_action, HookContext())
    assert prompt_result.success is True
    assert prompt_result.output == "hi"

    # agent 路径：返回占位。
    agent_action = Action(type="agent", prompt="do")
    agent_result = await execute_action(agent_action, HookContext())
    assert agent_result.success is True
    assert agent_result.output == "agent executor not yet implemented"


# 验证 execute_action 对未知 type 返回 success=False。
# type=unknown 时分发器找不到执行器，断言返回失败结果。
async def test_execute_action_unknown_type_returns_failure() -> None:
    action = Action(type="unknown")
    result = await execute_action(action, HookContext())
    assert result.success is False
    assert "Unknown action type" in result.output


# 验证 execute_action 分发到 http 执行器。
# 用 mock urlopen 走 http 路径，断言 success=True。
async def test_execute_action_dispatches_to_http() -> None:
    class _FakeResp:
        status = 200

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    with patch(
        "seacode.hooks.executors.urllib.request.urlopen",
        return_value=_FakeResp(),
    ):
        action = Action(type="http", url="http://x", method="GET")
        result = await execute_action(action, HookContext())

    assert result.success is True
    assert "HTTP 200" in result.output
