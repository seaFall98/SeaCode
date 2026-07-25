"""模型相关常量与内置映射表，服务于上下文窗口的四层降级解析。"""

from __future__ import annotations

# 上下文窗口的保守默认值（第 4 层 fallback）。
# 与主流 Claude 模型的窗口对齐；其它模型由映射表或显式配置覆盖。
DEFAULT_CONTEXT_WINDOW: int = 200_000

# 内置"模型名子串 -> context window（最大输入 token 数）"映射表，
# 是上下文窗口回退链的第 3 层。按从最具体到最通用排序，第一个子串命中即生效。
# 值仅为合理起始点，模型更新/重命名后可能过时；用户可在配置中显式设置
# context_window 覆盖（最高优先级）。
MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("1m", 1_000_000),       # 也覆盖 "-1m" 后缀（如 claude-...-1m）
    ("gpt-4.1", 1_000_000),  # GPT-4.1 系列的 window 为 1M
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o1", 200_000),         # OpenAI 推理模型 o1 / o3 / o4
    ("o3", 200_000),
    ("o4", 200_000),
    ("gpt-3.5", 16_385),
    ("claude", 200_000),
]


def lookup_model_context_window(model: str) -> int:
    """通过子串匹配返回内置映射表中该模型对应的 context window；未命中返回 0。"""
    m = model.lower()
    for substr, window in MODEL_CONTEXT_WINDOWS:
        if substr in m:
            return window
    return 0
