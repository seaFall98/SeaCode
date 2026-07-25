"""动作执行器：command/prompt/http/agent 四类 + execute_action 分发器。"""

from __future__ import annotations

import asyncio
import logging
import urllib.request
from urllib.error import URLError

from seacode.hooks.models import Action, ActionResult, HookContext

log = logging.getLogger(__name__)


# 执行 shell 命令；超时 kill 子进程后等待退出避免僵尸；异常转为失败结果。
async def execute_command(action: Action, ctx: HookContext) -> ActionResult:
    command = ctx.expand(action.command)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=action.timeout
            )
        except TimeoutError:
            # 超时后必须 kill 并等待回收，避免子进程变成僵尸。
            proc.kill()
            await proc.wait()
            return ActionResult(
                output=f"Command timed out after {action.timeout}s: {command}",
                success=False,
            )
        output = stdout.decode(errors="replace").strip() if stdout else ""
        return ActionResult(output=output, success=proc.returncode == 0)
    except Exception as e:
        return ActionResult(output=f"Command execution error: {e}", success=False)


# 注入提示词；返回展开占位符后的 message，由引擎追加到 _prompt_messages。
async def execute_prompt(action: Action, ctx: HookContext) -> ActionResult:
    message = ctx.expand(action.message)
    return ActionResult(output=message, success=True)


# 发起 HTTP 请求；用 run_in_executor 推到线程池避免阻塞事件循环；响应截断 500 字符。
async def execute_http(action: Action, ctx: HookContext) -> ActionResult:
    url = ctx.expand(action.url)
    body = ctx.expand(action.body) if action.body else None
    method = action.method or "POST"

    # headers 中的占位符也需展开（用于鉴权令牌等）。
    headers = dict(action.headers)
    for k, v in headers.items():
        headers[k] = ctx.expand(v)
    # body 存在时自动加 Content-Type，便于常见 JSON API 调用。
    if body and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"

    def _do_request() -> tuple[int, str]:
        # 阻塞的 urlopen 在线程池中执行；返回 (status, body) 供外层组装结果。
        data = body.encode() if body else None
        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode(errors="replace")[:500]
            return resp.status, resp_body

    try:
        loop = asyncio.get_running_loop()
        status, resp_body = await loop.run_in_executor(None, _do_request)
        return ActionResult(
            output=f"HTTP {status}: {resp_body}",
            success=200 <= status < 300,
        )
    except URLError as e:
        return ActionResult(output=f"HTTP error: {e}", success=False)
    except Exception as e:
        return ActionResult(output=f"HTTP error: {e}", success=False)


# agent 执行器占位；真实实现留给第 12 步 SubAgent 体系，本步返回 not yet implemented。
async def execute_agent(action: Action, ctx: HookContext) -> ActionResult:
    prompt = ctx.expand(action.prompt)
    log.info("Agent executor stub called with prompt: %s", prompt[:100])
    return ActionResult(
        output="agent executor not yet implemented",
        success=True,
    )


# 动作类型 -> 执行器映射；execute_action 按此分发，未知类型返回失败。
_EXECUTOR_MAP = {
    "command": execute_command,
    "prompt": execute_prompt,
    "http": execute_http,
    "agent": execute_agent,
}


# 按 action.type 分发到对应执行器；未知类型返回 success=False。
async def execute_action(action: Action, ctx: HookContext) -> ActionResult:
    executor = _EXECUTOR_MAP.get(action.type)
    if executor is None:
        return ActionResult(
            output=f"Unknown action type: {action.type}",
            success=False,
        )
    return await executor(action, ctx)
