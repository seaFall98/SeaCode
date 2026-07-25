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
    ParsedSkillUrl,
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
def _skills_sh_parsed() -> ParsedSkillUrl:
    return parse_skill_url("https://skills.sh/user/commit-skill")


# ---------- 包重导出 ----------


# 验证 seacode.skills 包重导出 install 模块全部公开符号。
# 顶层已 from seacode.skills 导入这些符号；断言非 None 即验证重导出成功。
def test_skills_reexports_install_symbols() -> None:
    assert ParsedSkillUrl is not None
    assert parse_skill_url is not None
    assert install_skill is not None
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


# 验证 parse_skill_url 解析 skills.sh 格式 URL。
# 传入 skills.sh URL，断言 owner/repo/branch/path/url_type 字段正确。
def test_parse_skill_url_skills_sh_format() -> None:
    result = parse_skill_url("https://skills.sh/user/commit-skill")
    assert result.owner == "user"
    assert result.repo == "commit-skill"
    assert result.branch == "main"
    assert result.path == ""
    assert result.url_type == "skills.sh"


# 验证 parse_skill_url 解析 github.com/tree 格式 URL。
# 传入 github tree URL，断言 owner/repo/branch/path/url_type 字段正确。
def test_parse_skill_url_github_tree_format() -> None:
    result = parse_skill_url("https://github.com/user/repo/tree/main/skills/commit")
    assert result.owner == "user"
    assert result.repo == "repo"
    assert result.branch == "main"
    assert result.path == "skills/commit"
    assert result.url_type == "github_tree"


# 验证 parse_skill_url 解析 raw.githubusercontent.com 格式 URL。
# 传入 github raw URL，断言 owner/repo/branch/path/url_type 字段正确。
def test_parse_skill_url_github_raw_format() -> None:
    result = parse_skill_url(
        "https://raw.githubusercontent.com/user/repo/main/skills/commit/SKILL.md"
    )
    assert result.owner == "user"
    assert result.repo == "repo"
    assert result.branch == "main"
    assert result.path == "skills/commit/SKILL.md"
    assert result.url_type == "github_raw"


# 验证 parse_skill_url 对不支持的 URL 抛 ValueError。
# 传入 example.com URL，断言抛 ValueError。
def test_parse_skill_url_invalid_raises() -> None:
    with pytest.raises(ValueError, match="不支持的 Skill URL 格式"):
        parse_skill_url("https://example.com/skill")


# 验证 parse_skill_url 同时支持 http 与 https 协议。
# 分别用 http:// 和 https:// 解析 skills.sh URL，断言均成功且结果一致。
def test_parse_skill_url_http_https_both_supported() -> None:
    r1 = parse_skill_url("http://skills.sh/user/name")
    r2 = parse_skill_url("https://skills.sh/user/name")
    assert r1.owner == "user" and r1.repo == "name"
    assert r2.owner == "user" and r2.repo == "name"
    assert r1.url_type == "skills.sh"
    assert r2.url_type == "skills.sh"


# 验证 parse_skill_url 容忍末尾斜杠。
# 传入末尾带斜杠的 skills.sh URL，断言解析成功。
def test_parse_skill_url_trailing_slash_tolerated() -> None:
    result = parse_skill_url("https://skills.sh/user/name/")
    assert result.owner == "user"
    assert result.repo == "name"
    assert result.url_type == "skills.sh"


# ---------- install_skill 成功路径 ----------


# 验证 install_skill 原子安装成功。
# mock httpx 返回目录列表 + SKILL.md base64 内容，断言 target_dir 存在且含 SKILL.md。
async def test_install_skill_atomic_success(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[{"name": "SKILL.md", "type": "file", "path": "SKILL.md"}]
    )
    file_resp = _make_response(
        json_data={
            "name": "SKILL.md",
            "content": _b64(b"---\nname: commit\n---\nbody"),
            "encoding": "base64",
        }
    )
    client = _make_client([dir_resp, file_resp])
    target = tmp_path / "commit-skill"
    with patch("httpx.AsyncClient", return_value=client):
        result = await install_skill(_skills_sh_parsed(), target)
    assert result == target
    assert (target / "SKILL.md").exists()
    assert (target / "SKILL.md").read_bytes() == b"---\nname: commit\n---\nbody"


# 验证 install_skill 在 manifest 含 skill.yaml 时接受安装。
# mock 拉取 skill.yaml + prompt.md，断言安装成功且两文件落盘。
async def test_install_skill_accepts_skill_yaml_manifest(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[
            {"name": "skill.yaml", "type": "file", "path": "skill.yaml"},
            {"name": "prompt.md", "type": "file", "path": "prompt.md"},
        ]
    )
    yaml_resp = _make_response(
        json_data={"name": "skill.yaml", "content": _b64(b"name: commit"), "encoding": "base64"}
    )
    prompt_resp = _make_response(
        json_data={"name": "prompt.md", "content": _b64(b"body"), "encoding": "base64"}
    )
    client = _make_client([dir_resp, yaml_resp, prompt_resp])
    target = tmp_path / "commit-skill"
    with patch("httpx.AsyncClient", return_value=client):
        await install_skill(_skills_sh_parsed(), target)
    assert (target / "skill.yaml").exists()
    assert (target / "prompt.md").exists()


# 验证 install_skill 递归拉取子目录文件。
# mock 根目录含 SKILL.md + sub 子目录，断言安装后 target/sub/extra.md 存在。
async def test_install_skill_recurses_subdirectories(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[
            {"name": "SKILL.md", "type": "file", "path": "SKILL.md"},
            {"name": "sub", "type": "dir", "path": "sub"},
        ]
    )
    skill_resp = _make_response(
        json_data={"name": "SKILL.md", "content": _b64(b"body"), "encoding": "base64"}
    )
    subdir_resp = _make_response(
        json_data=[{"name": "extra.md", "type": "file", "path": "sub/extra.md"}]
    )
    extra_resp = _make_response(
        json_data={"name": "extra.md", "content": _b64(b"extra"), "encoding": "base64"}
    )
    client = _make_client([dir_resp, skill_resp, subdir_resp, extra_resp])
    target = tmp_path / "commit-skill"
    with patch("httpx.AsyncClient", return_value=client):
        await install_skill(_skills_sh_parsed(), target)
    assert (target / "SKILL.md").exists()
    assert (target / "sub" / "extra.md").exists()
    assert (target / "sub" / "extra.md").read_bytes() == b"extra"


# 验证 install_skill 在目标目录已存在时替换为新版本。
# 预创建 target 含 old.txt，mock 新 SKILL.md，断言 old.txt 消失且 SKILL.md 是新内容。
async def test_install_skill_replaces_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "commit-skill"
    target.mkdir()
    (target / "old.txt").write_text("old content")
    dir_resp = _make_response(
        json_data=[{"name": "SKILL.md", "type": "file", "path": "SKILL.md"}]
    )
    file_resp = _make_response(
        json_data={"name": "SKILL.md", "content": _b64(b"new body"), "encoding": "base64"}
    )
    client = _make_client([dir_resp, file_resp])
    with patch("httpx.AsyncClient", return_value=client):
        await install_skill(_skills_sh_parsed(), target)
    assert (target / "SKILL.md").exists()
    assert (target / "SKILL.md").read_bytes() == b"new body"
    assert not (target / "old.txt").exists()


# 验证 install_skill 请求设置正确的 User-Agent 头与超时。
# mock httpx，断言 timeout=HTTP_TIMEOUT 且请求头含 User-Agent: seacode-install-skill。
async def test_install_skill_sets_user_agent_and_timeout(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[{"name": "SKILL.md", "type": "file", "path": "SKILL.md"}]
    )
    file_resp = _make_response(
        json_data={"name": "SKILL.md", "content": _b64(b"body"), "encoding": "base64"}
    )
    client = _make_client([dir_resp, file_resp])
    target = tmp_path / "commit-skill"
    with patch("httpx.AsyncClient", return_value=client) as mock_cls:
        await install_skill(_skills_sh_parsed(), target)
    # 超时设置：httpx.AsyncClient(timeout=HTTP_TIMEOUT) 调用参数。
    assert mock_cls.call_args.kwargs["timeout"] == HTTP_TIMEOUT
    # User-Agent 头：client.get 请求头含 seacode-install-skill。
    headers = client.get.call_args.kwargs["headers"]
    assert headers["User-Agent"] == "seacode-install-skill"


# ---------- install_skill 失败与清理 ----------


# 验证 install_skill 安装失败时清理 staging 目录。
# mock httpx 抛异常 + mock tempfile.mkdtemp 返回已知路径，断言 staging 被清理。
async def test_install_skill_cleans_staging_on_failure(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    client = MagicMock()
    client.get = AsyncMock(side_effect=RuntimeError("network error"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=client):
        with patch(
            "seacode.skills.install.tempfile.mkdtemp", return_value=str(staging)
        ):
            with pytest.raises(RuntimeError, match="network error"):
                await install_skill(_skills_sh_parsed(), tmp_path / "target")
    assert not staging.exists()


# 验证 install_skill 在 manifest 缺失时拒绝安装。
# mock 拉取 readme.md（无 SKILL.md 或 skill.yaml），断言抛 ValueError。
async def test_install_skill_rejects_missing_manifest(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[{"name": "readme.md", "type": "file", "path": "readme.md"}]
    )
    file_resp = _make_response(
        json_data={"name": "readme.md", "content": _b64(b"readme"), "encoding": "base64"}
    )
    client = _make_client([dir_resp, file_resp])
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="manifest"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")


# 验证 install_skill 在 GitHub API 限流时抛 ValueError。
# mock 403 响应 + X-RateLimit-Remaining: 0，断言抛 ValueError 含"GitHub API 限流"。
async def test_install_skill_raises_on_rate_limit(tmp_path: Path) -> None:
    resp = _make_response(
        status_code=403,
        headers={"X-RateLimit-Remaining": "0"},
        text="rate limit exceeded",
    )
    client = _make_client([resp])
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="GitHub API 限流"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")


# ---------- install_skill 安全限制 ----------


# 验证 install_skill 单文件超过 1 MiB 触发限制。
# mock 文件内容 > MAX_FILE_SIZE，断言抛 ValueError 含"超过单文件大小限制"。
async def test_install_skill_single_file_too_large(tmp_path: Path) -> None:
    big_b64 = _b64(b"\x00" * (MAX_FILE_SIZE + 1))
    dir_resp = _make_response(
        json_data=[{"name": "big", "type": "file", "path": "big"}]
    )
    file_resp = _make_response(
        json_data={"name": "big", "content": big_b64, "encoding": "base64"}
    )
    client = _make_client([dir_resp, file_resp])
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="超过单文件大小限制"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")


# 验证 install_skill 累计大小超过 8 MiB 触发限制。
# mock 9 个 1 MiB 文件（累计 > MAX_TOTAL_SIZE），断言抛 ValueError。
async def test_install_skill_total_size_too_large(tmp_path: Path) -> None:
    large_b64 = _b64(b"\x00" * MAX_FILE_SIZE)
    entries = [
        {"name": f"f{i}", "type": "file", "path": f"f{i}"} for i in range(9)
    ]
    dir_resp = _make_response(json_data=entries)
    file_resp = _make_response(
        json_data={"name": "f", "content": large_b64, "encoding": "base64"}
    )
    # 1 目录列表 + 9 文件内容；第 9 个文件累计 9437184 > 8388608 触发限制。
    client = _make_client([dir_resp] + [file_resp] * 9)
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="累计大小超过限制"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")


# 验证 install_skill 文件数超过 64 触发限制。
# mock 65 个文件条目，断言抛 ValueError 含"文件数超过限制"。
async def test_install_skill_file_count_too_many(tmp_path: Path) -> None:
    entries = [
        {"name": f"f{i}", "type": "file", "path": f"f{i}"} for i in range(65)
    ]
    dir_resp = _make_response(json_data=entries)
    file_resp = _make_response(
        json_data={"name": "f", "content": _b64(b"x"), "encoding": "base64"}
    )
    # 1 目录列表 + 65 文件内容；第 65 个文件 file_count=65 > 64 触发限制。
    client = _make_client([dir_resp] + [file_resp] * 65)
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="文件数超过限制"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")


# 验证 install_skill 递归深度超过 4 触发限制。
# mock 5 层嵌套子目录（depth 0-4 各返回目录，depth=5 抛限制），断言抛 ValueError。
async def test_install_skill_depth_too_deep(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[{"name": "d", "type": "dir", "path": "d"}]
    )
    # depth 0-4 共 5 次 GET 返回目录；depth=5 在 GET 前抛递归深度限制。
    client = _make_client([dir_resp] * 5)
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="递归深度"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")


# 验证 install_skill 检测 entry name 含 .. 的路径穿越。
# mock 目录条目 name 含 ".."，断言抛 ValueError 含"非法文件名"。
async def test_install_skill_rejects_dotdot_path_traversal(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[{"name": "../etc", "type": "file", "path": "../etc"}]
    )
    client = _make_client([dir_resp])
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="非法文件名"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")


# 验证 install_skill 检测 entry name 含 / 的路径穿越。
# mock 目录条目 name 含 "/"，断言抛 ValueError 含"非法文件名"。
async def test_install_skill_rejects_slash_path_traversal(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[{"name": "a/b", "type": "file", "path": "a/b"}]
    )
    client = _make_client([dir_resp])
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="非法文件名"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")


# 验证 install_skill 检测 entry name 含反斜杠的路径穿越。
# mock 目录条目 name 含 "\\"，断言抛 ValueError 含"非法文件名"。
async def test_install_skill_rejects_backslash_path_traversal(tmp_path: Path) -> None:
    dir_resp = _make_response(
        json_data=[{"name": "a\\b", "type": "file", "path": "a\\b"}]
    )
    client = _make_client([dir_resp])
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(ValueError, match="非法文件名"):
            await install_skill(_skills_sh_parsed(), tmp_path / "commit-skill")
