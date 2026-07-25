"""/review 命令：发起代码审查（构造提示词发给 LLM）。"""

from __future__ import annotations

import os

from seacode.commands.registry import Command, CommandContext, CommandType


# /review：构造代码审查提示词（工作目录 + 变更范围 + 审查重点），追加用户额外关注点，
# 通过 send_user_message 发给当前 Provider 触发 LLM 审查。
async def handle_review(ctx: CommandContext) -> None:
    work_dir = os.getcwd()
    lines = [
        "请对当前工作目录的代码变更进行审查。",
        f"工作目录：{work_dir}",
        "审查重点：",
        "  - 逻辑正确性与边界条件",
        "  - 错误处理与异常恢复",
        "  - 安全性（输入校验、权限边界、敏感信息泄露）",
        "  - 可读性与命名",
        "  - 性能与资源占用",
    ]
    focus = ctx.args.strip()
    if focus:
        lines.append(f"额外关注点：{focus}")
    lines.append("")
    lines.append("请给出具体的问题位置、风险等级与改进建议，不要泛泛而谈。")
    ctx.ui.send_user_message("\n".join(lines))


# 命令定义：PROMPT 类型，把构造好的提示词发给 LLM。
REVIEW_COMMAND = Command(
    name="review",
    description="发起代码审查",
    type=CommandType.PROMPT,
    handler=handle_review,
    aliases=[],
    usage="/review [focus]",
    arg_prompt="",
)
