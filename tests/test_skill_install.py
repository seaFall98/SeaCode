"""Skill 安装器单元测试：覆盖 parse_skill_url、install_skill、安全限制与原子安装。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seacode.skills import (
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

# ---------- 辅助函数 ----------


# 构造 httpx 响应 mock：支持 status_code / json / headers / text / content。
def _make_response(
    status_code: int = 200,
    json_data: Any = None,
    headers: dict[str, str] | None = None,
    text: str = "",
    content: bytes = b"",
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers if headers is not None else {}
    resp.text = text
    resp.content = content
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


# 构造 httpx.AsyncClient 实例 mock：get 按序返回 responses，支持 async with。
def _make_client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(side_effect=list(responses))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# base64 编码 helper：把 bytes 编码为 GitHub Contents API content 字段格式。
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# 构造 skills.sh 解析结果，便于 install_skill 测试快速复用。
# 3 段 URL：owner/repo/skill-name；skill-name 即安装目录名。
def _skills_sh_parsed() -> SkillSource:
    return parse_skill_url("https://skills.sh/user/repo/commit-skill")


# 构造一个可直接内联解码的文件 entry（含 content/encoding/size），便于下载测试复用。
def _file_entry(name: str, data: bytes, path: str | None = None) -> dict:
    return {
        "name": name,
        "type": "file",
        "path": path if path is not None else name,
        "content": _b64(data),
        "encoding": "base64",
        "size": len(data),
    }


# ---------- 包重导出 ----------


# 验证 seacode.skills 包重导出 install 模块全部公开符号（含 InstallReport 与 SkillSource）。
# 顶层已 from seacode.skills 导入这些符号；断言非 None 即验证重导出成功。
def test_skills_reexports_install_symbols() -> None:
    assert SkillSource is not None
    assert parse_skill_url is not None
    assert install_skill is not None
    assert InstallReport is not None
    assert MAX_FILE_SIZE is not None
    assert MAX_TOTAL_SIZE is not None
    assert MAX_FILE_COUNT is not None
    assert MAX_RECURSION_DEPTH is not None
    assert HTTP_TIMEOUT is not None


# ---------- 模块常量 ----------


# 验证 install 模块安全限制常量值符合设计。
# 断言五项常量值：1MiB / 8MiB / 64 / 4 / 30.0。
def test_module_constants_values() -> None:
    assert MAX_FILE_SIZE == 1 << 20  # 1 MiB = 1048576
    assert MAX_TOTAL_SIZE == 8 << 20  # 8 MiB = 8388608
    assert MAX_FILE_COUNT == 64
    assert MAX_RECURSION_DEPTH == 4
    assert HTTP_TIMEOUT == 30.0


# ---------- parse_skill_url ----------


# 验证 parse_skill_url 解析 skills.sh 格式 URL（3 段 /<owner>/<repo>/<skill-name>）。
# 传入 skills.sh URL，断言 owner/repo/ref/subpath/name/original 字段正确。
def test_parse_skill_url_skills_sh_format() -> None:
    result = parse_skill_url("https://skills.sh/user/repo/commit-skill")
    assert result.owner == "user"
    assert result.repo == "repo"
    assert result.ref == "main"
    assert result.subpath == "skills/commit-skill"
    assert result.name == "commit-skill"
    assert result.original == "https://skills.sh/user/repo/commit-skill"


# 验证 parse_skill_url 解析 www.skills.sh 格式 URL（host 带 www 前缀）。
# 传入 www.skills.sh URL，断言解析结果与 skills.sh 一致。
def test_parse_skill_url_www_skills_sh_format() -> None:
    result = parse_skill_url("https://www.skills.sh/user/repo/commit-skill")
    assert result.owner == "user"
    assert result.repo == "repo"
    assert result.ref == "main"
    assert result.subpath == "skills/commit-skill"
    assert result.name == "commit-skill"


# 验证 parse_skill_url 解析 github.com/tree 格式 URL。
# 传入 github tree URL，断言 owner/repo/ref/subpath/name 字段正确。
def test_parse_skill_url_github_tree_format() -> None:
    result = parse_skill_url("https://github.com/user/repo/tree/main/skills/commit")
    assert result.owner == "user"
    assert result.repo == "repo"
    assert result.ref == "main"
    assert result.subpath == "skills/commit"
    assert result.name == "commit"


# 验证 parse_skill_url 解析 raw.githubusercontent.com 格式 URL。
# 传入 github raw URL（末段是文件名），断言 subpath 取所在目录、name 取目录末段。
def test_parse_skill_url_github_raw_format() -> None:
    result = parse_skill_url(
        "https://raw.githubusercontent.com/user/repo/main/skills/commit/SKILL.md"
    )
    assert result.owner == "user"
    assert result.repo == "repo"
    assert result.ref == "main"
    assert result.subpath == "skills/commit"
    assert result.name == "commit"


# 验证 parse_skill_url 对不支持的 host 抛 ValueError。
# 传入 example.com URL，断言抛 ValueError 含"不支持的 host"。
def test_parse_skill_url_invalid_raises() -> None:
    with pytest.raises(ValueError, match="不支持的 host"):
        parse_skill_url("https://example.com/skill")


# 验证 parse_skill_url 对非 http(s) 协议抛 ValueError。
# 传入 ftp URL，断言抛 ValueError 含"仅支持 http(s) URL"。
def test_parse_skill_url_invalid_scheme_raises() -> None:
    with pytest.raises(ValueError, match="仅支持 http"):
        parse_skill_url("ftp://skills.sh/user/repo/name")


# 验证 parse_skill_url 对 skills.sh 2 段 URL 抛 ValueError（必须 3 段）。
# 传入 2 段 skills.sh URL，断言抛 ValueError。
def test_parse_skill_url_skills_sh_two_segments_raises() -> None:
    with pytest.raises(ValueError):
        parse_skill_url("https://skills.sh/user/commit-skill")


# 验证 parse_skill_url 同时支持 http 与 https 协议。
# 分别用 http:// 和 https:// 解析 skills.sh URL，断言均成功且结果一致。
def test_parse_skill_url_http_https_both_supported() -> None:
    r1 = parse_skill_url("http://skills.sh/user/repo/name")
    r2 = parse_skill_url("https://skills.sh/user/repo/name")
    assert r1.owner == "user" and r1.repo == "repo" and r1.name == "name"
    assert r2.owner == "user" and r2.repo == "repo" and r2.name == "name"
    assert r1.ref == "main"
    assert r2.ref == "main"


# 验证 parse_skill_url 容忍末尾斜杠。
# 传入末尾带斜杠的 skills.sh URL，断言解析成功。
def test_parse_skill_url_trailing_slash_tolerated() -> None:
    result = parse_skill_url("https://skills.sh/user/repo/name/")
    assert result.owner == "user"
    assert result.repo == "repo"
    assert result.name == "name"


# 验证 parse_skill_url 不再校验 skill 名称（名称校验移至 install_skill 入口）。
# 传入含大写字母的 skill-name，断言 parse_skill_url 不抛异常；name 原样保留。
def test_parse_skill_url_does_not_validate_name() -> None:
    result = parse_skill_url("https://skills.sh/user/repo/BadName")
    assert result.name == "BadName"
    assert result.original == "https://skills.sh/user/repo/BadName"


# ---------- install_skill 名称校验 ----------


# 验证 install_skill 在入口处校验 skill 名称非法字符抛 ValueError。
# 用 parse_skill_url 解析含大写字母的 URL 得到 SkillSource，再调 install_skill 触发校验。
async def test_install_skill_invalid_name_raises(tmp_path: Path) -> None:
    src = parse_skill_url("https://skills.sh/user/repo/BadName")
    with pytest.raises(ValueError, match="非法字符"):
        await install_skill(src, tmp_path)


# ---------- install_skill 成功路径 ----------


# 验证 install_skill 原子安装成功并返回 InstallReport。
# mock httpx 返回目录列表（内联 base64 content），断言 report 字段与文件落盘。
async def test_install_skill_atomic_success(tmp_path: Path) -> None:
    body = b"---\nname: commit\n---\nbody"
    dir_resp = _make_response(json_data=[_file_entry("SKILL.md", body)])
    client = _make_client([dir_resp])
    with patch("httpx.AsyncClient", return_value=client):
        result = await install_skill(_skills_sh_parsed(), tmp_path)
    assert isinstance(result, InstallReport)
    assert result.skill_name == "commit-skill"
    assert result.target_dir == str(tmp_path / "commit-skill")
    assert result.file_count == 1
    target = tmp_path / "commit-skill"
    assert (target / "SKILL.md").exists()
    assert (target / "SKILL.md").read_bytes() == body


# 验证 install_skill 在 manifest 含 skill.yaml 时接受安装。
# mock 拉取 skill.yaml + prompt.md，断言安装成功且两文件落盘。
async def test_install_skill_accepts_skill_yaml_manifest(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[
            _file_entry("skill.yaml", b"name: commit"),
            _file_entry("prompt.md", b"body"),
        ]
    )
    client = _make_client([dir_resp])
    with patch("httpx.AsyncClient", return_value=client):
        await install_skill(_skills_sh_parsed(), tmp_path)
    target = tmp_path / "commit-skill"
    assert (target / "skill.yaml").exists()
    assert (target / "prompt.md").exists()


# 验证 install_skill 递归拉取子目录文件。
# mock 根目录含 SKILL.md + sub 子目录，断言安装后 target/sub/extra.md 存在。
async def test_install_skill_recurses_subdirectories(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[
            _file_entry("SKILL.md", b"body"),
            {"name": "sub", "type": "dir", "path": "sub"},
        ]
    )
    subdir_resp = _make_response(
        json_data=[_file_entry("extra.md", b"extra", path="sub/extra.md")]
    )
    client = _make_client([dir_resp, subdir_resp])
    with patch("httpx.AsyncClient", return_value=client):
        await install_skill(_skills_sh_parsed(), tmp_path)
    target = tmp_path / "commit-skill"
    assert (target / "SKILL.md").exists()
    assert (target / "sub" / "extra.md").exists()
    assert (target / "sub" / "extra.md").read_bytes() == b"extra"


# 验证 install_skill 在目标目录已存在时替换为新版本。
# 预创建 target 含 old.txt，mock 新 SKILL.md，断言 old.txt 消失且 SKILL.md 是新内容。
async def test_install_skill_replaces_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "commit-skill"
    target.mkdir()
    (target / "old.txt").write_text("old content")
    dir_resp = _make_response(json_data=[_file_entry("SKILL.md", b"new body")])
    client = _make_client([dir_resp])
    with patch("httpx.AsyncClient", return_value=client):
        await install_skill(_skills_sh_parsed(), tmp_path)
    assert (target / "SKILL.md").exists()
    assert (target / "SKILL.md").read_bytes() == b"new body"
    assert not (target / "old.txt").exists()


# 验证 install_skill 请求设置正确的 User-Agent 头与超时。
# mock httpx，断言 timeout=HTTP_TIMEOUT 且请求头含 User-Agent: seacode-install-skill。
async def test_install_skill_sets_user_agent_and_timeout(tmp_path: Path) -> None:
    dir_resp = _make_response(json_data=[_file_entry("SKILL.md", b"body")])
    client = _make_client([dir_resp])
    with patch("httpx.AsyncClient", return_value=client):
        await install_skill(_skills_sh_parsed(), tmp_path)
    # 超时设置：httpx.AsyncClient(timeout=HTTP_TIMEOUT)