"""子 Agent 定义加载器：三级搜索、热重载与内置集合加载。

加载优先级：项目级 ``<work_dir>/.seacode/agents/`` > 用户级
``~/.seacode/agents/`` > 内置级 ``seacode.agents.builtins`` 包内 ``.md``。
``not in seen`` 实现先到先得；项目级同名定义覆盖用户级与内置级。

``get(name)`` 支持热重载：项目级与用户级 ``.md`` 从 ``file_path`` 重读解析，
失败回退缓存；内置 ``.md`` 的 ``file_path`` 为 None，直接返回缓存。
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

from seacode.agents.parser import AgentDef, AgentParseError, parse_agent_file

logger = logging.getLogger(__name__)


class AgentLoader:
    """三级搜索子 Agent 定义；构造时立即调用 ``load_all``。"""

    def __init__(self, work_dir: Path, enable_verification: bool = False) -> None:
        self._work_dir = work_dir
        self._enable_verification = enable_verification
        self._agents: dict[str, AgentDef] = {}
        # 已加载 agent_type 集合；先到先得实现优先级覆盖。
        self._seen: set[str] = set()
        self.load_all()

    # 依次扫描项目级、用户级、内置级；每级用 _scan_dir 处理 *.md。
    def load_all(self) -> None:
        self._scan_dir(self._work_dir / ".seacode" / "agents", source="project")
        self._scan_dir(Path.home() / ".seacode" / "agents", source="user")
        self._load_builtins()

    # 扫描目录下所有 .md 文件并解析；解析失败 warning 跳过，不阻断其它文件。
    def _scan_dir(self, dir_path: Path, source: str) -> None:
        if not dir_path.is_dir():
            return
        for path in sorted(dir_path.glob("*.md")):
            try:
                agent_def = parse_agent_file(path, source=source)
            except AgentParseError as e:
                logger.warning("skip agent file %s: %s", path, e)
                continue
            if agent_def.agent_type not in self._seen:
                self._agents[agent_def.agent_type] = agent_def
                self._seen.add(agent_def.agent_type)

    # 通过 importlib.resources 加载内置 .md；Verification 受 enable_verification 控制。
    def _load_builtins(self) -> None:
        try:
            pkg_root = resources.files("seacode.agents.builtins")
        except (ModuleNotFoundError, AttributeError) as e:
            logger.warning("failed to locate seacode.agents.builtins package: %s", e)
            return

        for entry in pkg_root.iterdir():
            if not entry.name.endswith(".md"):
                continue
            try:
                with entry.open("r", encoding="utf-8") as f:
                    text = f.read()
                # 复用 parse_frontmatter + _validate_agent_meta，但不读磁盘。
                from seacode.agents.parser import _validate_agent_meta, parse_frontmatter

                meta, body = parse_frontmatter(text)
                validated = _validate_agent_meta(meta)
                agent_def = AgentDef(
                    agent_type=validated["name"],
                    when_to_use=validated["description"],
                    system_prompt=body,
                    tools=validated.get("tools", []),
                    disallowed_tools=validated.get("disallowedTools", []),
                    model=validated.get("model", "inherit"),
                    max_turns=validated.get("maxTurns", 200),
                    permission_mode=validated.get("permissionMode", "default"),
                    background=validated.get("background", False),
                    isolation=validated.get("isolation", ""),
                    # 内置 .md 的 file_path 置 None：不参与热重载，避免触发磁盘读取。
                    file_path=None,
                    source="builtin",
                )
            except AgentParseError as e:
                logger.warning("skip builtin agent %s: %s", entry.name, e)
                continue
            except OSError as e:
                logger.warning("failed to read builtin agent %s: %s", entry.name, e)
                continue

            if agent_def.agent_type == "Verification" and not self._enable_verification:
                continue
            if agent_def.agent_type not in self._seen:
                self._agents[agent_def.agent_type] = agent_def
                self._seen.add(agent_def.agent_type)

    # 取出 AgentDef；项目级与用户级从 file_path 热重载，失败回退缓存。
    def get(self, name: str) -> AgentDef | None:
        agent_def = self._agents.get(name)
        if agent_def is None:
            return None
        if agent_def.file_path is not None:
            try:
                return parse_agent_file(agent_def.file_path, source=agent_def.source)
            except AgentParseError as e:
                logger.warning(
                    "hot reload failed for %s, fallback to cache: %s", name, e
                )
                return agent_def
        return agent_def

    # 返回 [(agent_type, when_to_use)] 列表；按加载顺序保留。
    def list_agents(self) -> list[tuple[str, str]]:
        return [
            (agent_def.agent_type, agent_def.when_to_use)
            for agent_def in self._agents.values()
        ]
