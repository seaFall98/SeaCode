"""Skill 命令与自动注册单元测试：覆盖 /skill handler 与命令注册流程。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from seacode.commands.handlers.skill import SKILL_COMMAND, handle_skill
from seacode.commands.handlers.skill_register import (
    _REGISTERED_SKILL_NAMES,
    _run_fork,
    make_skill_handler,
    make_skill_register_callback,
    register_skill_commands,
)
from seacode.commands.registry import Command, CommandContext, CommandRegistry, CommandType
from seacode.permissions import PermissionMode
from seacode.skills.parser import SkillDef


# 假 UI：实现 UIController 协议，记录 system/user 消息。
class _FakeUI:
    def __init__(self) -> None:
        self.system_messages: list[str] = []
        self.user_messages: list[str] = []

    def add_system_message(self, text: str) -> None:
        self.system_messages.append(text)

    def send_user_message(self, text: str) -> None:
        self.user_messages.append(text)

    def set_plan_mode(self, enabled: bool) -> None:
        pass

    def set_permission_mode(self, mode: PermissionMode) -> None:
        del mode

    def get_token_count(self) -> tuple[int, int]:
        return (0, 100000)

    def refresh_status(self) -> None:
        pass


# 假 loader：可预设 skills/catalog/source_label，记录 reload 调用。
class _FakeLoader:
    def __init__(
        self,
        skills: dict[str, SkillDef] | None = None,
        catalog: list[tuple[str, str]] | None = None,
        source_label: str = "project",
    ) -> None:
        self._skills = skills if skills is not None else {}
        self._catalog = catalog if catalog is not None else []
        self._source_label = source_label
        self.reload_calls = 0

    def get(self, name: str) -> SkillDef | None:
        return self._skills.get(name)

    def get_catalog(self) -> list[tuple[str, str]]:
        return self._catalog

    def get_source_label(self, name: str) -> str:
        return self._source_label

    def reload(self) -> dict[str, SkillDef]:
        self.reload_calls += 1
        return self._skills


# 假 agent：携带 skill_loader 与 set_skill_catalog 调用记录。
class _FakeAgent:
    def __init__(self, loader: Any = None) -> None:
        self.skill_loader = loader
        self.set_skill_catalog_calls: list[str] = []

    def set_skill_catalog(self, text: str) -> None:
        self.set_skill_catalog_calls.append(text)


# 假 executor：记录 execute_inline/execute_fork 调用与可预设返回值。
class _FakeExecutor:
    def __init__(
        self,
        inline_return: str = "inline prompt",
        fork_return: str = "fork result",
    ) -> None:
        self._inline_return = inline_return
        self._fork_return = fork_return
        self.inline_calls: list[tuple[str, str]] = []
        self.fork_calls: list[tuple[str, str]] = []

    async def execute_inline(self, skill: SkillDef, args: str = "") -> str:
        self.inline_calls.append((skill.name, args))
        return self._inline_return

    async def execute_fork(self, skill: SkillDef, args: str = "") -> str:
        self.fork_calls.append((skill.name, args))
        return self._fork_return


# 构造 CommandContext，注入各 handler 所需的依赖与默认空值。
def _make_ctx(
    args: str = "",
    agent: Any = None,
    ui: _FakeUI | None = None,
    config: dict[str, Any] | None = None,
) -> CommandContext:
    return CommandContext(
        args=args,
        agent=agent,
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui if ui is not None else _FakeUI(),
        config=config if config is not None else {},
    )


# 构造默认 SkillDef 供测试复用。
def _make_skill(
    name: str = "commit",
    description: str = "提交",
    mode: str = "inline",
) -> SkillDef:
    return SkillDef(
        name=name,
        description=description,
        prompt_body=f"{name} SOP body",
        mode=mode,
        context="none",
    )


# 自动清理模块级 _REGISTERED_SKILL_NAMES 集合避免测试间相互污染。
@pytest.fixture(autouse=True)
def _reset_registered_names() -> Any:
    _REGISTERED_SKILL_NAMES.clear()
    yield
    _REGISTERED_SKILL_NAMES.clear()


# 捕获 asyncio.create_task 调用并返回 task 列表，用于 fork 模式等待后台 task 完成。
def _capture_create_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> list[asyncio.Task[Any]]:
    captured: list[asyncio.Task[Any]] = []
    real_create_task = asyncio.create_task

    def capture(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = real_create_task(coro, **kwargs)
        captured.append(task)
        return task

    monkeypatch.setattr("asyncio.create_task", capture)
    return captured


# ---------- /skill list ----------


# 验证 /skill list 输出所有 Skill 与来源标签。
# mock catalog 含 commit/review；调 handle_skill(args="list")；断言输出含名称与 [project] 标签。
async def test_skill_list_outputs_skills_with_source_label() -> None:
    loader = _FakeLoader(
        skills={
            "commit": _make_skill("commit"),
            "review": _make_skill("review"),
        },
        catalog=[("commit", "提交"), ("review", "审查")],
        source_label="project",
    )
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="list", agent=agent, ui=ui)

    await handle_skill(ctx)

    text = ui.system_messages[0]
    assert "Skills (2 entries)" in text
    assert "commit" in text
    assert "review" in text
    assert "[project]" in text


# 验证 /skill list 空列表显示 0 entries。
# mock catalog 返回 []；调 handle_skill(args="list")；断言输出为 "Skills (0 entries)"。
async def test_skill_list_empty_shows_zero_entries() -> None:
    loader = _FakeLoader(skills={}, catalog=[])
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="list", agent=agent, ui=ui)

    await handle_skill(ctx)

    assert ui.system_messages[0] == "Skills (0 entries)"


# 验证 /skill list 按 name 排序输出。
# mock catalog 返回 [("z","1"),("a","2")]（顺序与排序后不同）；断言输出中 a 在 z 之前。
async def test_skill_list_sorted_by_name() -> None:
    loader = _FakeLoader(
        skills={"z": _make_skill("z"), "a": _make_skill("a")},
        catalog=[("z", "1"), ("a", "2")],
    )
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="list", agent=agent, ui=ui)

    await handle_skill(ctx)

    text = ui.system_messages[0]
    assert text.index("- a") < text.index("- z")


# 验证 /skill 无参默认走 list 分支。
# args="" 调 handle_skill；断言输出走 list 路径（含 Skills 数量行）。
async def test_skill_no_args_defaults_to_list() -> None:
    loader = _FakeLoader(
        skills={"commit": _make_skill("commit")},
        catalog=[("commit", "提交")],
    )
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)

    await handle_skill(ctx)

    text = ui.system_messages[0]
    assert "Skills (1 entries)" in text
    assert "commit" in text


# ---------- /skill info ----------


# 验证 /skill info <name> 显示 Skill 详情字段。
# mock loader.get 返回 SkillDef；调 handle_skill(args="info commit")；断言输出含各详情字段。
async def test_skill_info_shows_skill_details() -> None:
    skill = SkillDef(
        name="commit",
        description="提交",
        prompt_body="body",
        mode="inline",
        model="claude-test",
        context="none",
        source_path=None,
        is_directory=False,
    )
    loader = _FakeLoader(skills={"commit": skill}, catalog=[("commit", "提交")])
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="info commit", agent=agent, ui=ui)

    await handle_skill(ctx)

    text = ui.system_messages[0]
    assert "name: commit" in text
    assert "description: 提交" in text
    assert "mode: inline" in text
    assert "context: none" in text
    assert "model: claude-test" in text
    assert "is_directory: False" in text


# 验证 /skill info 缺参显示用法。
# args="info" 调 handle_skill；断言输出含 "用法：/skill info <name>"。
async def test_skill_info_no_arg_shows_usage() -> None:
    loader = _FakeLoader()
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="info", agent=agent, ui=ui)

    await handle_skill(ctx)

    assert "用法：/skill info <name>" in ui.system_messages[0]


# 验证 /skill info 未知 Skill 显示提示。
# mock loader.get 返回 None；断言输出含 "未知 Skill：unknown"。
async def test_skill_info_unknown_skill_shows_message() -> None:
    loader = _FakeLoader(skills={}, catalog=[])
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="info unknown", agent=agent, ui=ui)

    await handle_skill(ctx)

    assert "未知 Skill：unknown" in ui.system_messages[0]


# ---------- /skill reload ----------


# 验证 /skill reload 调 loader.reload + register_skill_commands + set_skill_catalog。
# 注入 config 回调记录调用；断言 reload 与两个回调均被触发且输出含 "已重载"。
async def test_skill_reload_invokes_reload_and_callbacks() -> None:
    loader = _FakeLoader(
        skills={"commit": _make_skill("commit")},
        catalog=[("commit", "提交")],
    )
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    register_calls = 0
    catalog_calls = 0

    def register_cb() -> None:
        nonlocal register_calls
        register_calls += 1

    def build_catalog_cb() -> str:
        nonlocal catalog_calls
        catalog_calls += 1
        return "catalog text"

    config = {
        "register_skill_commands": register_cb,
        "build_skill_catalog": build_catalog_cb,
    }
    ctx = _make_ctx(args="reload", agent=agent, ui=ui, config=config)

    await handle_skill(ctx)

    assert loader.reload_calls == 1
    assert register_calls == 1
    assert catalog_calls == 1
    assert agent.set_skill_catalog_calls == ["catalog text"]
    assert "已重载" in ui.system_messages[0]


# 验证 /skill reload 输出重载数量。
# mock catalog 返回 3 个 Skill；断言输出含 "已重载 3 个 Skill"。
async def test_skill_reload_shows_count() -> None:
    catalog = [("a", "1"), ("b", "2"), ("c", "3")]
    loader = _FakeLoader(
        skills={n: _make_skill(n) for n, _ in catalog},
        catalog=catalog,
    )
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="reload", agent=agent, ui=ui)

    await handle_skill(ctx)

    assert "已重载 3 个 Skill" in ui.system_messages[0]


# ---------- 未知子命令与未初始化 ----------


# 验证 /skill 未知子命令显示用法提示。
# args="unknown" 调 handle_skill；断言输出含 "未知子命令：unknown" 与用法提示。
async def test_skill_unknown_subcommand_shows_usage() -> None:
    loader = _FakeLoader()
    agent = _FakeAgent(loader)
    ui = _FakeUI()
    ctx = _make_ctx(args="unknown", agent=agent, ui=ui)

    await handle_skill(ctx)

    text = ui.system_messages[0]
    assert "未知子命令：unknown" in text
    assert "用法：/skill [list|info|reload]" in text


# 验证 /skill loader 未初始化显示提示。
# mock agent.skill_loader=None；断言输出含 "Skill 系统未初始化"。
async def test_skill_loader_not_initialized_shows_message() -> None:
    agent = _FakeAgent(loader=None)
    ui = _FakeUI()
    ctx = _make_ctx(args="list", agent=agent, ui=ui)

    await handle_skill(ctx)

    assert "Skill 系统未初始化" in ui.system_messages[0]


# 验证 /skill agent 为 None 时也走未初始化分支。
# agent=None；断言输出含 "Skill 系统未初始化"。
async def test_skill_agent_none_shows_not_initialized() -> None:
    ui = _FakeUI()
    ctx = _make_ctx(args="list", agent=None, ui=ui)

    await handle_skill(ctx)

    assert "Skill 系统未初始化" in ui.system_messages[0]


# 验证 SKILL_COMMAND 命令定义字段。
# 直接读 SKILL_COMMAND 字段断言 name/type/usage/arg_prompt 与设计一致。
def test_skill_command_definition() -> None:
    assert SKILL_COMMAND.name == "skill"
    assert SKILL_COMMAND.type == CommandType.LOCAL
    assert SKILL_COMMAND.usage == "/skill [list|info|reload]"
    assert SKILL_COMMAND.arg_prompt == "子命令"


# ---------- register_skill_commands ----------


# 验证 register_skill_commands 注册每个 Skill 为 PROMPT 命令。
# catalog 2 个 Skill；调 register_skill_commands；断言 registry 含 2 条 PROMPT 命令。
def test_register_skill_commands_registers_each_skill() -> None:
    registry = CommandRegistry()
    loader = _FakeLoader(
        skills={
            "commit": _make_skill("commit"),
            "review": _make_skill("review"),
        },
        catalog=[("commit", "提交"), ("review", "审查")],
    )
    executor = _FakeExecutor()

    register_skill_commands(registry, loader, executor)

    commit_cmd = registry.find("commit")
    review_cmd = registry.find("review")
    assert commit_cmd is not None
    assert review_cmd is not None
    assert commit_cmd.type == CommandType.PROMPT
    assert review_cmd.type == CommandType.PROMPT
    assert commit_cmd.description == "提交"
    assert review_cmd.description == "审查"
    assert _REGISTERED_SKILL_NAMES == {"commit", "review"}


# 验证 register_skill_commands 重名 Skill 跳过注册。
# 预先注册一个同名 LOCAL 命令；调 register_skill_commands；断言 catalog 项被跳过且 warning 日志。
def test_register_skill_commands_skips_duplicate_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = CommandRegistry()

    async def noop_handler(ctx: CommandContext) -> None:
        pass

    existing = Command(
        name="commit",
        description="existing",
        type=CommandType.LOCAL,
        handler=noop_handler,
    )
    registry.register_sync(existing)
    loader = _FakeLoader(
        skills={"commit": _make_skill("commit")},
        catalog=[("commit", "提交")],
    )
    executor = _FakeExecutor()

    with caplog.at_level("WARNING", logger="seacode.commands.handlers.skill_register"):
        register_skill_commands(registry, loader, executor)

    # 重名跳过：registry 中仍只有预注册的 LOCAL 命令。
    cmd = registry.find("commit")
    assert cmd is existing
    assert "commit" not in _REGISTERED_SKILL_NAMES
    assert any("commit" in r.message for r in caplog.records)


# 验证 register_skill_commands reload 时清理旧命令。
# 第一次注册 2 个 → 第二次注册 3 个；断言旧命令被 unregister、新命令被注册。
def test_register_skill_commands_clears_old_commands_on_reload() -> None:
    registry = CommandRegistry()
    loader = _FakeLoader(
        skills={
            "commit": _make_skill("commit"),
            "review": _make_skill("review"),
        },
        catalog=[("commit", "提交"), ("review", "审查")],
    )
    executor = _FakeExecutor()

    register_skill_commands(registry, loader, executor)
    assert len(registry.list_commands()) == 2
    assert _REGISTERED_SKILL_NAMES == {"commit", "review"}

    # 第二次注册：catalog 替换为 3 个（含新项 lint），旧 commit/review 应被先 unregister 再重注册。
    loader._skills = {
        "commit": _make_skill("commit"),
        "review": _make_skill("review"),
        "lint": _make_skill("lint", description="lint 检查"),
    }
    loader._catalog = [
        ("commit", "提交"),
        ("review", "审查"),
        ("lint", "lint 检查"),
    ]
    register_skill_commands(registry, loader, executor)

    assert len(registry.list_commands()) == 3
    assert registry.find("lint") is not None
    assert _REGISTERED_SKILL_NAMES == {"commit", "review", "lint"}


# 验证 register_skill_commands 空 catalog 不注册任何命令。
# catalog=[]；断言 registry 为空且 _REGISTERED_SKILL_NAMES 为空。
def test_register_skill_commands_empty_catalog_no_op() -> None:
    registry = CommandRegistry()
    loader = _FakeLoader(skills={}, catalog=[])
    executor = _FakeExecutor()

    register_skill_commands(registry, loader, executor)

    assert len(registry.list_commands()) == 0
    assert _REGISTERED_SKILL_NAMES == set()


# 验证 _REGISTERED_SKILL_NAMES 集合跟踪已注册命令状态。
# 注册后断言集合含已注册名称；再次注册空 catalog 后断言集合为空。
def test_registered_skill_names_tracks_state() -> None:
    registry = CommandRegistry()
    loader = _FakeLoader(
        skills={"commit": _make_skill("commit")},
        catalog=[("commit", "提交")],
    )
    executor = _FakeExecutor()

    register_skill_commands(registry, loader, executor)
    assert _REGISTERED_SKILL_NAMES == {"commit"}

    # 再注册清理后集合应反映新状态（空 catalog）。
    loader._skills = {}
    loader._catalog = []
    register_skill_commands(registry, loader, executor)
    assert _REGISTERED_SKILL_NAMES == set()
    assert len(registry.list_commands()) == 0


# ---------- make_skill_handler ----------


# 验证 make_skill_handler inline 模式触发 execute_inline + send_user_message。
# skill.mode="inline"；构造 handler 调用；断言 execute_inline 与 send_user_message 被调用。
async def test_make_skill_handler_inline_triggers_execute_and_send() -> None:
    skill = _make_skill("commit", mode="inline")
    loader = _FakeLoader(skills={"commit": skill}, catalog=[("commit", "提交")])
    executor = _FakeExecutor(inline_return="inline prompt for commit")
    handler = make_skill_handler("commit", loader, executor)
    ui = _FakeUI()
    ctx = _make_ctx(args="some args", agent=None, ui=ui)

    await handler(ctx)

    assert executor.inline_calls == [("commit", "some args")]
    assert ui.user_messages == ["inline prompt for commit"]


# 验证 make_skill_handler fork 模式后台执行（asyncio.create_task）。
# skill.mode="fork"；捕获 create_task 调用；等待 task 完成后断言 system_message 含结果。
async def test_make_skill_handler_fork_runs_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_tasks = _capture_create_tasks(monkeypatch)
    skill = _make_skill("commit", mode="fork")
    loader = _FakeLoader(skills={"commit": skill}, catalog=[("commit", "提交")])
    executor = _FakeExecutor(fork_return="fork result text")
    handler = make_skill_handler("commit", loader, executor)
    ui = _FakeUI()
    ctx = _make_ctx(args="fork args", agent=None, ui=ui)

    await handler(ctx)

    # 等待 create_task 创建的后台 fork task 完成
    if captured_tasks:
        await asyncio.gather(*captured_tasks, return_exceptions=True)

    assert executor.fork_calls == [("commit", "fork args")]
    assert ui.user_messages == []
    assert any("fork result text" in m for m in ui.system_messages)


# 验证 make_skill_handler Skill 不可用显示提示。
# loader.get 返回 None；断言 add_system_message 含 "已不可用"。
async def test_make_skill_handler_skill_unavailable_shows_message() -> None:
    loader = _FakeLoader(skills={}, catalog=[])
    executor = _FakeExecutor()
    handler = make_skill_handler("missing", loader, executor)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=None, ui=ui)

    await handler(ctx)

    assert any("已不可用" in m for m in ui.system_messages)
    assert executor.inline_calls == []


# 验证 make_skill_handler 工厂函数立即绑定 skill_name 避免闭包延迟。
# 在循环中构造 3 个 handler 依次调用；断言每个 handler 触发对应 Skill 的 execute_inline。
async def test_make_skill_handler_factory_binds_immediately() -> None:
    names = ("commit", "review", "lint")
    skills = {n: _make_skill(n) for n in names}
    loader = _FakeLoader(skills=skills, catalog=[(n, n) for n in names])
    executor = _FakeExecutor()
    handlers = [make_skill_handler(name, loader, executor) for name in names]

    for name, handler in zip(names, handlers, strict=True):
        ui = _FakeUI()
        ctx = _make_ctx(args="", agent=None, ui=ui)
        await handler(ctx)
        # 每个 handler 应只触发对应名称的 execute_inline
        assert executor.inline_calls[-1] == (name, "")


# ---------- _run_fork ----------


# 验证 _run_fork 正常返回结果作为系统消息。
# mock executor.execute_fork 返回 "result"；调 _run_fork；断言 add_system_message 含 "result"。
async def test_run_fork_success_returns_result() -> None:
    skill = _make_skill("commit", mode="fork")
    executor = _FakeExecutor(fork_return="result text")
    ui = _FakeUI()
    ctx = _make_ctx(args="args", agent=None, ui=ui)

    await _run_fork(skill, ctx, executor)

    assert any("result text" in m for m in ui.system_messages)


# 验证 _run_fork 异常显示错误消息。
# mock executor.execute_fork 抛异常；调 _run_fork；断言输出含 "fork 失败" 与异常信息。
async def test_run_fork_exception_shows_error() -> None:
    skill = _make_skill("commit", mode="fork")
    executor = _FakeExecutor()

    async def raise_fork(skill: SkillDef, args: str = "") -> str:
        raise RuntimeError("boom")

    executor.execute_fork = raise_fork  # type: ignore[method-assign]
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=None, ui=ui)

    await _run_fork(skill, ctx, executor)

    assert any("fork 失败" in m and "boom" in m for m in ui.system_messages)


# ---------- make_skill_register_callback ----------


# 验证 make_skill_register_callback 返回闭包。
# 构造闭包并调用；断言 register_skill_commands 被触发（registry 中有命令）。
def test_make_skill_register_callback_returns_closure() -> None:
    registry = CommandRegistry()
    loader = _FakeLoader(
        skills={"commit": _make_skill("commit")},
        catalog=[("commit", "提交")],
    )
    executor = _FakeExecutor()

    callback = make_skill_register_callback(registry, loader, executor)
    assert callable(callback)

    callback()
    assert registry.find("commit") is not None
    assert _REGISTERED_SKILL_NAMES == {"commit"}


# ---------- activate_skill (Agent 扩展) ----------


# 验证 Agent.activate_skill 把 SOP 存入 active_skills 字典。
# 用 object.__new__ 跳过复杂 __init__；手工设置 active_skills 后调 activate_skill 断言条目已写入。
def test_activate_skill_stores_sop_in_active_skills() -> None:
    from seacode.agent import Agent

    agent = object.__new__(Agent)
    agent.active_skills = {}

    agent.activate_skill("commit", "prompt body")

    assert agent.active_skills["commit"] == "prompt body"
