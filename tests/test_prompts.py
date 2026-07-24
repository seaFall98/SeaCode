from __future__ import annotations

import platform

import pytest

from seacode.prompts import (
    _PLAN_MODE_EXIT_REMINDER,
    _PLAN_MODE_FULL_REMINDER,
    _PLAN_MODE_REENTRY_REMINDER,
    _PLAN_MODE_SPARSE_REMINDER,
    _REMINDER_INTERVAL,
    DOING_TASKS_SECTION,
    EXECUTING_ACTIONS_SECTION,
    IDENTITY_SECTION,
    SYSTEM_SECTION,
    TEXT_OUTPUT_SECTION,
    TONE_STYLE_SECTION,
    USING_TOOLS_SECTION,
    EnvironmentContext,
    PromptBuilder,
    PromptSection,
    build_environment_context,
    build_plan_mode_exit_reminder,
    build_plan_mode_reentry_reminder,
    build_plan_mode_reminder,
    build_system_prompt,
    detect_environment,
    environment_section,
)

# ---------------------------------------------------------------------------
# 段落常量与 PromptSection dataclass
# ---------------------------------------------------------------------------


# 验证八个固定段落常量的 priority 按间隔 10 递增且 content 非空。
# 检查 IDENTITY/SYSTEM/DOING_TASKS/EXECUTING_ACTIONS/USING_TOOLS/TONE_STYLE/TEXT_OUTPUT 七段。
def test_fixed_sections_have_increasing_priority_and_non_empty_content() -> None:
    sections = [
        IDENTITY_SECTION,
        SYSTEM_SECTION,
        DOING_TASKS_SECTION,
        EXECUTING_ACTIONS_SECTION,
        USING_TOOLS_SECTION,
        TONE_STYLE_SECTION,
        TEXT_OUTPUT_SECTION,
    ]
    priorities = [s.priority for s in sections]
    assert priorities == [0, 10, 20, 30, 40, 50, 60]
    for s in sections:
        assert s.content.strip() != ""
        assert s.name != ""


# 验证 PromptSection dataclass 字段与相等性。
# 两个相同字段的 PromptSection 应相等，content 不同的应不等。
def test_prompt_section_dataclass_fields_and_equality() -> None:
    a = PromptSection(name="A", priority=10, content="hello")
    b = PromptSection(name="A", priority=10, content="hello")
    c = PromptSection(name="A", priority=20, content="hello")
    assert a == b
    assert a != c
    assert a.name == "A"
    assert a.priority == 10
    assert a.content == "hello"


# 验证 IDENTITY 段落包含 SeaCode 身份与两条安全红线。
# 检查品牌名与 IMPORTANT 安全约束出现。
def test_identity_section_contains_seacode_brand_and_safety_lines() -> None:
    content = IDENTITY_SECTION.content
    assert "SeaCode" in content
    assert "security vulnerabilities" in content
    assert "NEVER generate or guess URLs" in content


# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------


# 验证 PromptBuilder.add 链式返回 self。
# 连续调用 add 后返回值应是同一个 builder 实例。
def test_prompt_builder_add_returns_self_for_chaining() -> None:
    b = PromptBuilder()
    result = b.add(PromptSection("A", 0, "a"))
    assert result is b
    result = b.add(PromptSection("B", 10, "b"))
    assert result is b


# 验证 PromptBuilder.build 按 priority 升序排序，乱序 add 后输出有序。
# 先 add priority=30 再 add priority=10，build 后 10 应在 30 之前。
def test_prompt_builder_build_sorts_by_priority() -> None:
    b = PromptBuilder()
    b.add(PromptSection("Late", 30, "Late content"))
    b.add(PromptSection("Early", 10, "Early content"))
    result = b.build()
    assert result == "Early content\n\nLate content"


# 验证 PromptBuilder.build 过滤空内容段落。
# 含空字符串与纯空白段落的应被过滤，保留非空段落。
def test_prompt_builder_build_filters_empty_sections() -> None:
    b = PromptBuilder()
    b.add(PromptSection("Empty", 0, ""))
    b.add(PromptSection("Whitespace", 5, "   \n\n  "))
    b.add(PromptSection("Real", 10, "real content"))
    result = b.build()
    assert result == "real content"


# 验证 PromptBuilder.build strip 首尾空白并以双换行分隔。
# 段落 content 含首尾空白时 build 应 strip，分隔符为双换行。
def test_prompt_builder_build_strips_and_separates_with_double_newline() -> None:
    b = PromptBuilder()
    b.add(PromptSection("A", 0, "  \nfirst\n  "))
    b.add(PromptSection("B", 10, "\nsecond\n"))
    result = b.build()
    assert result == "first\n\nsecond"


# 验证 PromptBuilder.build 无段落时返回空字符串。
# 空 builder 调用 build 应返回空串而非抛出异常。
def test_prompt_builder_build_empty_returns_empty_string() -> None:
    b = PromptBuilder()
    assert b.build() == ""


# ---------------------------------------------------------------------------
# detect_environment 与 environment_section
# ---------------------------------------------------------------------------


# 验证 detect_environment 在非 git 目录返回 is_git_repo=False。
# 使用临时目录或系统临时路径，断言 is_git_repo 为 False。
def test_detect_environment_returns_not_git_for_non_git_dir(tmp_path: object) -> None:
    env = detect_environment(str(tmp_path))
    assert isinstance(env, EnvironmentContext)
    assert env.is_git_repo is False
    assert env.git_branch == ""
    assert env.work_dir == str(tmp_path)
    assert env.os_name != ""
    assert env.arch != ""
    assert env.shell != ""
    assert env.date  # 非空


# 验证 detect_environment 在 git 仓库返回 is_git_repo=True 与分支名。
# 在临时目录初始化 git 仓库并提交，断言 is_git_repo=True 且 git_branch 非空。
def test_detect_environment_detects_git_repo_and_branch(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess

    repo_dir = str(tmp_path)
    # 初始化 git 仓库并切换到非 main 分支便于断言。
    monkeypatch.chdir(repo_dir)
    subprocess.run(
        ["git", "init", "-b", "feature-branch"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], check=True, capture_output=True
    )
    # 创建一个文件并提交以让 HEAD 指向一个有效分支。
    with open(os.path.join(repo_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("init\n")
    subprocess.run(["git", "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], check=True, capture_output=True)

    env = detect_environment(repo_dir)
    assert env.is_git_repo is True
    assert env.git_branch == "feature-branch"


# 验证 Windows 平台使用 COMSPEC 检测 shell。
# 用 monkeypatch 模拟 platform.system() 返回 Windows 与 COMSPEC 环境变量。
def test_detect_environment_windows_uses_comspec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\System32\\cmd.exe")
    monkeypatch.delenv("SHELL", raising=False)
    env = detect_environment(".")
    assert env.shell == "C:\\Windows\\System32\\cmd.exe"


# 验证 Unix 平台使用 SHELL 检测 shell。
# 用 monkeypatch 模拟 platform.system() 返回 Linux 与 SHELL 环境变量。
def test_detect_environment_unix_uses_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    env = detect_environment(".")
    assert env.shell == "/bin/zsh"


# 验证 environment_section 输出格式与条件字段。
# 注入假 EnvironmentContext，断言基本行存在且 git_branch/model 按条件出现。
def test_environment_section_formats_lines_with_conditional_fields() -> None:
    env = EnvironmentContext(
        work_dir="/tmp/project",
        os_name="Linux",
        arch="x86_64",
        shell="/bin/bash",
        is_git_repo=True,
        git_branch="main",
        model="test-model",
        date="2026-07-25",
    )
    section = environment_section("/tmp/project", env=env)
    assert section.name == "Environment"
    assert section.priority == 70
    content = section.content
    assert "# Environment" in content
    assert "Working directory: /tmp/project" in content
    assert "Platform: Linux/x86_64" in content
    assert "Shell: /bin/bash" in content
    assert "Is Git repo: True" in content
    assert "Git branch: main" in content
    assert "Model: test-model" in content


# 验证 environment_section 在非 git 仓库不输出 git_branch 行。
# 注入 is_git_repo=False 的 env，断言内容不含 Git branch 行。
def test_environment_section_omits_git_branch_when_not_repo() -> None:
    env = EnvironmentContext(
        work_dir="/tmp",
        os_name="Linux",
        arch="x86_64",
        shell="/bin/bash",
        is_git_repo=False,
        git_branch="",
        model="",
        date="2026-07-25",
    )
    section = environment_section("/tmp", env=env)
    assert "Git branch" not in section.content
    assert "Model" not in section.content


# 验证 environment_section 默认调用 detect_environment。
# 不传 env 时应调用 detect_environment(work_dir) 自动检测。
def test_environment_section_defaults_to_detect_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env = EnvironmentContext(
        work_dir="/custom",
        os_name="Linux",
        arch="x86_64",
        shell="/bin/bash",
        is_git_repo=False,
        git_branch="",
        model="",
        date="2026-07-25",
    )
    called_with: list[str] = []

    def fake_detect(work_dir: str) -> EnvironmentContext:
        called_with.append(work_dir)
        return fake_env

    monkeypatch.setattr("seacode.prompts.detect_environment", fake_detect)
    section = environment_section("/custom")
    assert called_with == ["/custom"]
    assert "Working directory: /custom" in section.content


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------


# 验证 build_system_prompt 无参调用返回七个固定段落与 Environment 段落。
# 断言输出含每个段落的标题或关键短语，且 Environment 段落出现。
def test_build_system_prompt_returns_all_sections_and_environment() -> None:
    prompt = build_system_prompt(work_dir="/tmp")
    assert "You are SeaCode" in prompt
    assert "# System" in prompt
    assert "# Doing tasks" in prompt
    assert "# Executing actions with care" in prompt
    assert "# Using your tools" in prompt
    assert "# Tone and style" in prompt
    assert "# Text output" in prompt
    assert "# Environment" in prompt
    assert "Working directory: /tmp" in prompt


# 验证 build_system_prompt 不同 work_dir 时 Environment 段落内容变化。
# work_dir=/a 与 work_dir=/b 的输出在 Working directory 行上不同。
def test_build_system_prompt_environment_changes_with_work_dir() -> None:
    prompt_a = build_system_prompt(work_dir="/path/a")
    prompt_b = build_system_prompt(work_dir="/path/b")
    assert "Working directory: /path/a" in prompt_a
    assert "Working directory: /path/b" in prompt_b
    assert prompt_a != prompt_b


# 验证 build_system_prompt 传入 custom_instructions 时注入对应段落。
# 传入非空 custom_instructions，断言 # Project Instructions 段落出现。
def test_build_system_prompt_injects_custom_instructions() -> None:
    prompt = build_system_prompt(
        work_dir="/tmp", custom_instructions="Always use type hints."
    )
    assert "# Project Instructions" in prompt
    assert "Always use type hints." in prompt


# 验证 build_system_prompt 不传 custom_instructions 时不出现该段落。
# 默认调用应不包含 # Project Instructions 标题。
def test_build_system_prompt_omits_custom_instructions_by_default() -> None:
    prompt = build_system_prompt(work_dir="/tmp")
    assert "# Project Instructions" not in prompt


# 验证 build_system_prompt 传入 skill_section 时作为段落注入。
# 传入非空 skill_section，断言其内容出现在结果中。
def test_build_system_prompt_injects_skill_section() -> None:
    prompt = build_system_prompt(
        work_dir="/tmp", skill_section="# Skills\n- Skill A"
    )
    assert "# Skills" in prompt
    assert "Skill A" in prompt


# 验证 build_system_prompt 传入 memory_section 时作为段落注入。
# 传入非空 memory_section，断言其内容出现在结果中。
def test_build_system_prompt_injects_memory_section() -> None:
    prompt = build_system_prompt(
        work_dir="/tmp", memory_section="# Memory\n- fact 1"
    )
    assert "# Memory" in prompt
    assert "fact 1" in prompt


# 验证 build_system_prompt 传入 hook_prompts 时末尾拼接 # Hook Injected Context。
# 传入非空 hook_prompts 列表，断言末尾出现该标题与 hook 内容。
def test_build_system_prompt_appends_hook_prompts_at_end() -> None:
    prompt = build_system_prompt(
        work_dir="/tmp", hook_prompts=["hook-one: do X", "hook-two: do Y"]
    )
    assert "# Hook Injected Context" in prompt
    assert "hook-one: do X" in prompt
    assert "hook-two: do Y" in prompt
    # hook 段落在所有 Builder 段落之后。
    assert prompt.index("# Hook Injected Context") > prompt.index("# Environment")


# 验证 build_system_prompt 传入空 hook_prompts 时不拼接 hook 段落。
# 传入空列表或 None 应不出现 # Hook Injected Context。
def test_build_system_prompt_omits_hook_section_when_empty() -> None:
    assert "# Hook Injected Context" not in build_system_prompt(
        work_dir="/tmp", hook_prompts=None
    )
    assert "# Hook Injected Context" not in build_system_prompt(
        work_dir="/tmp", hook_prompts=[]
    )


# 验证 build_system_prompt coordinator_mode=False 时不触发延迟 import。
# 调用 build_system_prompt 默认参数应不触发 seacode.teams.coordinator 导入。
def test_build_system_prompt_does_not_import_coordinator_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    # 确保模块未加载，并拦截 import 防止后续加载。
    sys.modules.pop("seacode.teams.coordinator", None)
    build_system_prompt(work_dir="/tmp")
    assert "seacode.teams.coordinator" not in sys.modules


# 验证 build_system_prompt coordinator_mode=True 时调用协调者提示词。
# 用 monkeypatch 拦截延迟 import，断言返回协调者专属提示词。
def test_build_system_prompt_coordinator_mode_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    fake_module = types.ModuleType("seacode.teams.coordinator")

    def fake_get(agent_catalog: object = None) -> str:
        return "COORDINATOR_PROMPT"

    fake_module.get_coordinator_system_prompt = fake_get  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "seacode.teams.coordinator", fake_module)

    result = build_system_prompt(
        work_dir="/tmp",
        coordinator_mode=True,
        agent_catalog=[("explorer", "explore codebase")],
    )
    assert result == "COORDINATOR_PROMPT"


# ---------------------------------------------------------------------------
# build_environment_context
# ---------------------------------------------------------------------------


# 验证 build_environment_context 输出基本字段。
# 调用 build_environment_context 应包含 work_dir、操作系统与当前时间三行。
def test_build_environment_context_includes_basic_fields() -> None:
    context = build_environment_context("/tmp/project")
    assert "Current working directory: /tmp/project" in context
    assert "Operating system:" in context
    assert "Current time:" in context


# 验证 build_environment_context 传入 agent_catalog/skill_catalog 时追加段落。
# 两个 catalog 非空时应追加空行分隔的段落内容。
def test_build_environment_context_appends_catalogs() -> None:
    context = build_environment_context(
        "/tmp",
        agent_catalog="Agent A: explore",
        skill_catalog="Skill A: read",
    )
    assert "Agent A: explore" in context
    assert "Skill A: read" in context


# ---------------------------------------------------------------------------
# Plan Mode 提示词模板
# ---------------------------------------------------------------------------


# 验证 build_plan_mode_reminder 第 1 轮返回完整版提醒。
# iteration=1 时返回 _PLAN_MODE_FULL_REMINDER 格式化后的文本。
def test_plan_mode_reminder_iteration_1_returns_full_reminder() -> None:
    result = build_plan_mode_reminder("/path/plan.md", False, 1)
    assert "Plan mode is active" in result
    assert "/path/plan.md" in result
    assert "No plan file exists yet" in result
    assert "5-phase" not in result  # 完整版不包含精简版的 5-phase 短语


# 验证 build_plan_mode_reminder 中间轮次返回精简版提醒。
# 公式：(iteration-1)//5 % 5 == 0 时完整版；iteration=6/7/8/9 时 (n-1)//5=1 返回精简版。
@pytest.mark.parametrize("iteration", [6, 7, 8, 9, 10, 15, 25])
def test_plan_mode_reminder_middle_iterations_return_sparse_reminder(
    iteration: int,
) -> None:
    result = build_plan_mode_reminder("/path/plan.md", False, iteration)
    assert "Plan mode still active" in result
    assert "/path/plan.md" in result
    assert "5-phase workflow" in result


# 验证 build_plan_mode_reminder 按 _REMINDER_INTERVAL 频率重发完整版。
# iteration=2-5 时 (n-1)//5=0 % 5==0 返回完整版；iteration=26 时 5%5==0 返回完整版。
@pytest.mark.parametrize("iteration", [2, 3, 4, 5, 26, 27])
def test_plan_mode_reminder_repeats_full_at_interval(iteration: int) -> None:
    result = build_plan_mode_reminder("/p.md", False, iteration)
    assert "Plan mode is active" in result
    assert "Plan mode still active" not in result


# 验证 build_plan_mode_reminder iteration=6 返回精简版。
# (6-1)//5 = 1, 1 % 5 != 0，应返回精简版。
def test_plan_mode_reminder_iteration_6_returns_sparse() -> None:
    result = build_plan_mode_reminder("/p.md", False, 6)
    assert "Plan mode still active" in result
    assert "Plan mode is active" not in result


# 验证 build_plan_mode_reminder plan_exists=True 时 plan_file_info 文案差异。
# plan_exists=True 时应提示已有 plan 文件可增量编辑。
def test_plan_mode_reminder_plan_exists_changes_file_info() -> None:
    result = build_plan_mode_reminder("/path/plan.md", True, 1)
    assert "A plan file already exists" in result
    assert "make incremental edits" in result


# 验证 build_plan_mode_exit_reminder plan_exists=True 时附带 plan 文件路径。
# plan_exists=True 时 extra 应含 plan_path，False 时 extra 为空。
def test_plan_mode_exit_reminder_with_existing_plan_includes_path() -> None:
    result = build_plan_mode_exit_reminder("/path/plan.md", True)
    assert "Exited Plan Mode" in result
    assert "/path/plan.md" in result


# 验证 build_plan_mode_exit_reminder plan_exists=False 时不附带 plan 路径。
# plan_exists=False 时返回的文本应不含 plan_path。
def test_plan_mode_exit_reminder_without_plan_omits_path() -> None:
    result = build_plan_mode_exit_reminder("/path/plan.md", False)
    assert "Exited Plan Mode" in result
    assert "/path/plan.md" not in result


# 验证 build_plan_mode_reentry_reminder plan_exists=False 返回空字符串。
# 没有 plan 文件时重入提醒应为空。
def test_plan_mode_reentry_reminder_returns_empty_when_no_plan() -> None:
    assert build_plan_mode_reentry_reminder("/path/plan.md", False) == ""


# 验证 build_plan_mode_reentry_reminder plan_exists=True 返回重入提醒。
# 有 plan 文件时重入提醒应含 plan_path 与 5-phase workflow。
def test_plan_mode_reentry_reminder_with_plan_returns_text() -> None:
    result = build_plan_mode_reentry_reminder("/path/plan.md", True)
    assert "re-entered plan mode" in result
    assert "/path/plan.md" in result
    assert "5-phase workflow" in result


# 验证 _REMINDER_INTERVAL 常量值为 5。
# 直接断言常量值以锁定频率控制参数。
def test_reminder_interval_constant_is_five() -> None:
    assert _REMINDER_INTERVAL == 5


# 验证 Plan Mode 模板常量包含预期占位符或文案。
# 检查四个常量的关键文案存在。
def test_plan_mode_template_constants_contain_expected_text() -> None:
    assert "Plan mode is active" in _PLAN_MODE_FULL_REMINDER
    assert "{plan_file_info}" in _PLAN_MODE_FULL_REMINDER
    assert "Plan mode still active" in _PLAN_MODE_SPARSE_REMINDER
    assert "{plan_path}" in _PLAN_MODE_SPARSE_REMINDER
    assert "Exited Plan Mode" in _PLAN_MODE_EXIT_REMINDER
    assert "{extra}" in _PLAN_MODE_EXIT_REMINDER
    assert "re-entered plan mode" in _PLAN_MODE_REENTRY_REMINDER


# ---------------------------------------------------------------------------
# 注入段落优先级覆盖
# ---------------------------------------------------------------------------


# 验证 build_system_prompt 条件段落优先级正确插入固定段落之间。
# custom_instructions(80)/skill_section(90)/memory_section(95) 应在 Environment(70) 之后。
def test_build_system_prompt_conditional_sections_after_environment() -> None:
    prompt = build_system_prompt(
        work_dir="/tmp",
        custom_instructions="instr",
        skill_section="# Skills\n- A",
        memory_section="# Memory\n- fact",
    )
    env_idx = prompt.index("# Environment")
    instr_idx = prompt.index("# Project Instructions")
    skill_idx = prompt.index("# Skills")
    memory_idx = prompt.index("# Memory")
    assert env_idx < instr_idx < skill_idx < memory_idx


# 验证 build_environment_context 不注入 active_skills 内容。
# 传入非空 active_skills 应不影响输出（保留参数守卫但不消费）。
def test_build_environment_context_ignores_active_skills() -> None:
    context_a = build_environment_context(
        "/tmp", active_skills={"skill1": "content1"}
    )
    context_b = build_environment_context("/tmp")
    assert context_a == context_b
    assert "skill1" not in context_a
