"""SeaCode Skill 技能包子包：解析、加载、执行、安装。"""

from __future__ import annotations

from seacode.skills.executor import SkillExecutor
from seacode.skills.install import (
    HTTP_TIMEOUT,
    MAX_FILE_COUNT,
    MAX_FILE_SIZE,
    MAX_RECURSION_DEPTH,
    MAX_TOTAL_SIZE,
    InstallReport,
    SkillSource,
    install_skill,
    parse_skill_url,
)
from seacode.skills.loader import SkillLoader
from seacode.skills.parser import (
    SkillDef,
    parse_frontmatter,
    parse_skill_directory,
    parse_skill_file,
    substitute_arguments,
)

__all__ = [
    "HTTP_TIMEOUT",
    "MAX_FILE_COUNT",
    "MAX_FILE_SIZE",
    "MAX_RECURSION_DEPTH",
    "MAX_TOTAL_SIZE",
    "InstallReport",
    "SkillDef",
    "SkillExecutor",
    "SkillLoader",
    "SkillSource",
    "install_skill",
    "parse_frontmatter",
    "parse_skill_directory",
    "parse_skill_file",
    "parse_skill_url",
    "substitute_arguments",
]
