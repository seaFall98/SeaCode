"""Skill 安装器：URL 解析、GitHub Contents API 拉取、原子安装、安全限制。"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger(__name__)

# 安全限制常量：防止单文件过大、总量过大、文件数过多、递归过深。
MAX_FILE_SIZE = 1 << 20  # 1 MiB
MAX_TOTAL_SIZE = 8 << 20  # 8 MiB
MAX_FILE_COUNT = 64
MAX_RECURSION_DEPTH = 4
HTTP_TIMEOUT = 30.0

# GitHub Contents API 基址（含 /repos 前缀，构造 URL 时直接拼接 owner/repo/contents）。
_GITHUB_API = "https://api.github.com/repos"

# User-Agent 标识 SeaCode 身份。
_USER_AGENT = "seacode-install-skill"

# skill 名称只允许小写字母、数字、连字符、下划线，避免安装时落到非法目录名。
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]*$")


@dataclass
class SkillSource:
    """解析后的 skill 来源信息，最终统一走 GitHub Contents API 拉取。

    ``name`` 是 skill 安装目录名（取 subpath 末段），用于在 install_root 下定位最终目录，
    避免多 skill 仓库互相覆盖。``original`` 保留用户原始 URL，便于诊断。
    """

    owner: str
    repo: str
    ref: str  # 分支或 tag，默认 "main"
    subpath: str  # 仓库内 skill 目录的路径（无尾 /）
    name: str  # skill 名称（subpath 最后一段）
    original: str  # 用户原始 URL


@dataclass
class InstallReport:
    """安装完成后的汇报信息：名称、目标目录、文件数与累计字节。"""

    skill_name: str = ""
    target_dir: str = ""
    file_count: int = 0
    total_bytes: int = 0


# 解析三种 URL 格式：skills.sh / github.com/tree / raw.githubusercontent.com。
# 用 urlparse 解析 host，支持 www.skills.sh 与 skills.sh；不匹配任一格式抛 ValueError。
def parse_skill_url(raw: str) -> SkillSource:
    raw = raw.strip()
    u = urlparse(raw)
    if u.scheme not in ("http", "https"):
        raise ValueError("仅支持 http(s) URL")

    parts = [p for p in u.path.strip("/").split("/") if p]
    host = u.hostname or ""

    # skills.sh 格式：/<owner>/<repo>/<skill-name>
    if host in ("www.skills.sh", "skills.sh"):
        if len(parts) < 3:
            raise ValueError("skills.sh URL 必须为 /<owner>/<repo>/<skill-name>")
        return SkillSource(
            owner=parts[0],
            repo=parts[1],
            ref="main",
            subpath="skills/" + "/".join(parts[2:]),
            name=parts[-1],
            original=raw,
        )

    # github.com 格式：/<owner>/<repo>/tree/<ref>/<...subpath>
    if host == "github.com":
        if len(parts) < 5 or parts[2] != "tree":
            raise ValueError(
                "github.com URL 必须为 /<owner>/<repo>/tree/<ref>/<subpath>"
            )
        sub = "/".join(parts[4:])
        return SkillSource(
            owner=parts[0],
            repo=parts[1],
            ref=parts[3],
            subpath=sub,
            name=parts[-1],
            original=raw,
        )

    # raw.githubusercontent.com 格式：去掉尾部文件名，保留 skill 目录路径。
    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise ValueError("raw.githubusercontent.com URL 过短")
        sub_parts = parts[3:]
        if sub_parts and "." in sub_parts[-1]:
            sub_parts = sub_parts[:-1]
        if not sub_parts:
            raise ValueError("raw URL 缺少 skill 子路径")
        return SkillSource(
            owner=parts[0],
            repo=parts[1],
            ref=parts[2],
            subpath="/".join(sub_parts),
            name=sub_parts[-1],
            original=raw,
        )

    raise ValueError(f"不支持的 host {host!r}（请使用 skills.sh 或 github.com）")


# 校验 skill 名称：小写字母/数字/连字符/下划线，不能以 . 开头。
def _validate_skill_name(name: str) -> None:
    if not name:
        raise ValueError("skill 名称为空")
    if name.startswith("."):
        raise ValueError("skill 名称不能以 '.' 开头")
    if not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"skill 名称 {name!r} 含非法字符（仅允许 a-z 0-9 - _）"
        )


# 校验 entry name 不含路径穿越字符（安装阶段错误抛 RuntimeError）。
def _validate_entry_name(name: str) -> None:
    if not name or ".." in name or "/" in name or "\\" in name:
        raise RuntimeError(f"非法文件名: {name}")


# 校验 staging 目录含 manifest（SKILL.md 或 skill.yaml）。
def _has_skill_manifest(directory: Path) -> bool:
    return (directory / "SKILL.md").is_file() or (
        directory / "skill.yaml"
    ).is_file()


# GitHub Contents API 返回的单条条目。
@dataclass
class _ContentEntry:
    name: str
    path: str
    type: str  # "file" | "dir" | "symlink" | "submodule"
    download_url: str | None = None
    content: str | None = None
    encoding: str | None = None
    size: int = 0


# 把 API 返回的 JSON 解析为 _ContentEntry 列表（单文件 dict 响应包成单元素列表）。
def _parse_entries(data: list | dict) -> list[_ContentEntry]:
    items = data if isinstance(data, list) else [data]
    return [
        _ContentEntry(
            name=e.get("name", ""),
            path=e.get("path", ""),
            type=e.get("type", ""),
            download_url=e.get("download_url"),
            content=e.get("content"),
            encoding=e.get("encoding"),
            size=e.get("size", 0),
        )
        for e in items
    ]


# 调用 GitHub Contents API 列出指定路径下的条目。
# 复用调用方传入的 client，避免每次请求新建连接。
async def _list_contents(
    client: httpx.AsyncClient, src: SkillSource, subpath: str
) -> list[_ContentEntry]:
    url = f"{_GITHUB_API}/{src.owner}/{src.repo}/contents/{subpath}"
    params = {"ref": quote(src.ref, safe="")}
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    resp = await client.get(url, params=params, headers=headers)
    if resp.status_code == 403:
        raise RuntimeError(
            f"GitHub API 拒绝访问（可能限流）：{resp.text[:512].strip()}"
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub API 请求失败：status={resp.status_code} url={url}"
        )

    return _parse_entries(resp.json())


# 下载单个文件内容。优先用内联 base64，回退到 download_url；
# 下载前用 entry.size 检查单文件大小限制，避免下载过大文件。
async def _fetch_blob(
    client: httpx.AsyncClient, entry: _ContentEntry
) -> bytes:
    if entry.size > MAX_FILE_SIZE:
        raise RuntimeError(
            f"文件 {entry.path} 超过单文件大小限制：{entry.size} 字节（上限 {MAX_FILE_SIZE}）"
        )
    # 内联 base64：小文件时 API 直接返回内容，省一次请求。
    if entry.encoding == "base64" and entry.content:
        clean = entry.content.replace("\n", "")
        return base64.b64decode(clean)
    # 回退到 download_url。
    if not entry.download_url:
        raise RuntimeError(f"no download_url for {entry.path}")
    headers = {"User-Agent": _USER_AGENT}
    resp = await client.get(entry.download_url, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(
            f"下载 {entry.download_url} 失败：status {resp.status_code}"
        )
    return resp.content


# 递归遍历 GitHub 目录，下载所有文件到 local_dir。
# file 类型在当前 depth 内联下载，不再 depth+1，避免平坦目录误触深度限制。
async def _walk_and_download(
    client: httpx.AsyncClient,
    src: SkillSource,
    subpath: str,
    local_dir: Path,
    report: InstallReport,
    depth: int,
) -> None:
    if depth > MAX_RECURSION_DEPTH:
        raise RuntimeError(f"递归深度超过 {MAX_RECURSION_DEPTH}")

    entries = await _list_contents(client, src, subpath)
    for entry in entries:
        if report.file_count >= MAX_FILE_COUNT:
            raise RuntimeError(f"文件数超过限制 {MAX_FILE_COUNT}")

        _validate_entry_name(entry.name)

        target = local_dir / entry.name
        if entry.type == "file":
            # file 在当前 depth 直接下载，不递归、不消耗 depth。
            data = await _fetch_blob(client, entry)
            if report.total_bytes + len(data) > MAX_TOTAL_SIZE:
                raise RuntimeError(f"累计大小超过限制 {MAX_TOTAL_SIZE} 字节")
            target.write_bytes(data)
            report.file_count += 1
            report.total_bytes += len(data)
        elif entry.type == "dir":
            target.mkdir(parents=True, exist_ok=True)
            await _walk_and_download(
                client, src, entry.path, target, report, depth + 1
            )
        # symlink / submodule 直接跳过，避免安全风险。


# 从 GitHub 拉取 Skill 包并原子安装到 install_root/<name>/。
# staging 与最终目录同处 install_root 文件系统，保证 rename 原子；
# 失败时清理 staging 目录，不留下残缺文件。
async def install_skill(
    src: SkillSource,
    install_root: str | Path | None = None,
) -> InstallReport:
    _validate_skill_name(src.name)

    root = Path(install_root) if install_root is not None else _user_skills_root()
    root.mkdir(parents=True, exist_ok=True)

    # staging 与 final 同处 root 文件系统，rename 不会跨文件系统失败。
    staging = Path(tempfile.mkdtemp(prefix=f".install-{src.name}-", dir=str(root)))
    try:
        report = InstallReport(skill_name=src.name)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            await _walk_and_download(
                client, src, src.subpath, staging, report, depth=0
            )

        if not _has_skill_manifest(staging):
            raise RuntimeError("Skill 包缺少 SKILL.md 或 skill.yaml manifest")

        # 原子替换：先删除旧安装（如果存在），再 rename。
        final = root / src.name
        if final.exists():
            shutil.rmtree(final)
        staging.rename(final)
        report.target_dir = str(final)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# 返回用户级 ~/.seacode/skills 目录，不存在则自动创建。
def _user_skills_root() -> Path:
    root = Path.home() / ".seacode" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root
