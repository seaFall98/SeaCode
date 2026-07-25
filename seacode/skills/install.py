"""Skill 安装器：URL 解析、GitHub Contents API 拉取、原子安装、安全限制。"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# 安全限制常量：防止单文件过大、总量过大、文件数过多、递归过深。
MAX_FILE_SIZE = 1 << 20  # 1 MiB
MAX_TOTAL_SIZE = 8 << 20  # 8 MiB
MAX_FILE_COUNT = 64
MAX_RECURSION_DEPTH = 4
HTTP_TIMEOUT = 30.0

# GitHub Contents API 基址。
_GITHUB_API = "https://api.github.com/repos"

# User-Agent 标识 SeaCode 身份。
_USER_AGENT = "seacode-install-skill"

# 三种 URL 格式正则。
_SKILLS_SH_RE = re.compile(r"https?://skills\.sh/([^/]+)/([^/]+)/?$")
_GITHUB_TREE_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)"
)
_GITHUB_RAW_RE = re.compile(
    r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)"
)


@dataclass
class ParsedSkillUrl:
    """解析后的 Skill URL：owner/repo/branch/path 与 url_type。"""

    owner: str
    repo: str
    branch: str
    path: str
    url_type: str  # "skills.sh" | "github_tree" | "github_raw"


# 解析三种 URL 格式：skills.sh / github.com/tree / raw.githubusercontent.com。
# 不匹配任一格式抛 ValueError。
def parse_skill_url(url: str) -> ParsedSkillUrl:
    m = _SKILLS_SH_RE.match(url)
    if m:
        return ParsedSkillUrl(
            owner=m.group(1),
            repo=m.group(2),
            branch="main",
            path="",
            url_type="skills.sh",
        )

    m = _GITHUB_TREE_RE.match(url)
    if m:
        return ParsedSkillUrl(
            owner=m.group(1),
            repo=m.group(2),
            branch=m.group(3),
            path=m.group(4),
            url_type="github_tree",
        )

    m = _GITHUB_RAW_RE.match(url)
    if m:
        return ParsedSkillUrl(
            owner=m.group(1),
            repo=m.group(2),
            branch=m.group(3),
            path=m.group(4),
            url_type="github_raw",
        )

    raise ValueError(f"不支持的 Skill URL 格式: {url}")


# 通过 GitHub Contents API 递归拉取文件到 staging 目录。
# 响应是 list 时为目录，响应是 dict 时为文件；安全限制全程校验。
async def _fetch_contents(
    parsed: ParsedSkillUrl,
    path: str,
    staging: Path,
    depth: int,
    total_size: list[int],
    file_count: list[int],
) -> None:
    if depth > MAX_RECURSION_DEPTH:
        raise ValueError(f"递归深度超过 {MAX_RECURSION_DEPTH}")

    url = f"{_GITHUB_API}/{parsed.owner}/{parsed.repo}/contents/{path}"
    params = {"ref": parsed.branch}
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)

    if resp.status_code in (403, 429):
        remaining = resp.headers.get("X-RateLimit-Remaining", "")
        if remaining == "0":
            raise ValueError("GitHub API 限流，请稍后重试")
    if resp.status_code != 200:
        raise ValueError(
            f"GitHub API 请求失败: status={resp.status_code} body={resp.text[:200]}"
        )

    data = resp.json()

    # 单文件响应（dict）：下载 content。
    if isinstance(data, dict):
        entry_name = data.get("name", "")
        _validate_entry_name(entry_name)
        content_field = data.get("content", "")
        encoding = data.get("encoding", "")
        if encoding == "base64" and content_field:
            content_bytes = base64.b64decode(content_field)
        else:
            # 没有 base64 content 时用 raw_url 下载。
            raw_url = data.get("download_url")
            if not raw_url:
                raise ValueError(f"无法获取 {entry_name} 的下载链接")
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                raw_resp = await client.get(
                    raw_url, headers={"User-Agent": _USER_AGENT}
                )
            if raw_resp.status_code != 200:
                raise ValueError(
                    f"下载 {entry_name} 失败: status={raw_resp.status_code}"
                )
            content_bytes = raw_resp.content

        if len(content_bytes) > MAX_FILE_SIZE:
            raise ValueError(
                f"文件 {entry_name} 超过单文件大小限制 {MAX_FILE_SIZE} 字节"
            )
        total_size[0] += len(content_bytes)
        if total_size[0] > MAX_TOTAL_SIZE:
            raise ValueError(f"累计大小超过限制 {MAX_TOTAL_SIZE} 字节")
        file_count[0] += 1
        if file_count[0] > MAX_FILE_COUNT:
            raise ValueError(f"文件数超过限制 {MAX_FILE_COUNT}")

        target = staging / entry_name
        target.write_bytes(content_bytes)
        return

    # 目录响应（list）：递归拉取每个 entry。
    if not isinstance(data, list):
        raise ValueError(f"GitHub API 返回未知格式: {type(data).__name__}")

    for entry in data:
        entry_name = entry.get("name", "")
        _validate_entry_name(entry_name)
        entry_type = entry.get("type", "")
        entry_path = entry.get("path", "")
        if not entry_path:
            continue

        if entry_type == "dir":
            sub_dir = staging / entry_name
            sub_dir.mkdir(parents=True, exist_ok=True)
            await _fetch_contents(
                parsed, entry_path, sub_dir, depth + 1, total_size, file_count
            )
        elif entry_type == "file":
            await _fetch_contents(
                parsed, entry_path, staging, depth + 1, total_size, file_count
            )


# 校验 entry name 不含路径穿越字符。
def _validate_entry_name(name: str) -> None:
    if not name or ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"非法文件名: {name}")


# 校验 staging 目录含 manifest（SKILL.md 或 skill.yaml）。
def _validate_manifest(staging: Path) -> bool:
    return (staging / "SKILL.md").exists() or (staging / "skill.yaml").exists()


# 从 GitHub 拉取 Skill 包并原子安装到 target_dir。
# 失败时清理 staging 目录，不留下残缺文件；成功时替换旧 target_dir。
async def install_skill(parsed: ParsedSkillUrl, target_dir: Path) -> Path:
    staging = Path(tempfile.mkdtemp(prefix="seacode-skill-"))
    try:
        # raw URL 模式：path 是单个文件，但安装时仍按目录处理。
        # 把所在目录作为 path 拉取，确保 SKILL.md 等一起拉下来。
        fetch_path = parsed.path
        if parsed.url_type == "github_raw":
            # 取所在目录。
            parts = parsed.path.rsplit("/", 1)
            fetch_path = parts[0] if len(parts) > 1 else ""

        total_size = [0]
        file_count = [0]
        await _fetch_contents(
            parsed, fetch_path, staging, depth=0, total_size=total_size, file_count=file_count
        )

        if not _validate_manifest(staging):
            raise ValueError("Skill 包缺少 SKILL.md 或 skill.yaml manifest")

        # 原子安装：替换旧 target_dir。
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        staging.rename(target_dir)
        return target_dir
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
