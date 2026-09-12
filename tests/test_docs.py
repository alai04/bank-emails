"""文档与代码的一致性测试。

`.env.example` 与 README 是使用者最先看到的东西，最容易在改代码时忘记同步，
所以这里用测试把它们钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bank_emails.api import VERSION
from bank_emails.config import SENSITIVE_FIELDS, Settings

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
README = ROOT / "README.md"
API_SOURCE = ROOT / "src" / "bank_emails" / "api.py"

MENTIONED_RE = re.compile(r"^\s*#?\s*([A-Z0-9_]+)=", re.MULTILINE)
ACTIVE_RE = re.compile(r"^([A-Z0-9_]+)=", re.MULTILINE)
ROUTE_RE = re.compile(r"@(router|api)\.(get|post|put|patch|delete)\(\s*\"([^\"]+)\"")


def _settings_env_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_example_exists_and_is_not_ignored() -> None:
    assert ENV_EXAMPLE.is_file(), "缺少 .env.example"
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!.env.example" in gitignore
    assert "\n.env\n" in gitignore


def test_env_example_documents_every_setting() -> None:
    mentioned = set(MENTIONED_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
    assert _settings_env_names() <= mentioned, (
        f".env.example 缺少这些变量：{sorted(_settings_env_names() - mentioned)}"
    )
    assert mentioned - _settings_env_names() == set(), (
        f".env.example 里有 Settings 不认识的变量：{sorted(mentioned - _settings_env_names())}"
    )


def test_env_example_is_valid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """示例文件本身必须能通过校验，否则使用者复制后第一步就会报错。"""
    for name in _settings_env_names():
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert settings.auth_mode == "app"
    # 委派认证在示例里是注释掉的，避免两个凭据同时生效导致启动失败
    assert settings.m365_refresh_token is None
    assert settings.api_port == 8080


def test_env_example_uses_placeholders_for_secrets() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values = dict(re.findall(r"^\s*#?\s*([A-Z0-9_]+)=(.*)$", text, re.MULTILINE))
    for name in SENSITIVE_FIELDS:
        env_name = name.upper()
        assert env_name in values, f".env.example 缺少 {env_name}"
        assert values[env_name].startswith("change-me"), f"{env_name} 不是占位值"


def _documented_routes() -> set[str]:
    """从 api.py 里解析出所有路由路径，`api` 路由补上 /api/v1 前缀。"""
    source = API_SOURCE.read_text(encoding="utf-8")
    routes: set[str] = set()
    for group, _method, path in ROUTE_RE.findall(source):
        routes.add(path if group == "router" else f"/api/v1{path}")
    return routes


def test_readme_documents_every_route() -> None:
    readme = README.read_text(encoding="utf-8")
    missing = sorted(path for path in _documented_routes() if path not in readme)
    assert not missing, f"README 未记录这些接口：{missing}"


def test_readme_covers_quick_start_commands() -> None:
    readme = README.read_text(encoding="utf-8")
    for command in ("uv sync", "cp .env.example .env", "uv run bank-emails --check-config",
                    "uv run pytest"):
        assert command in readme, f"README 缺少命令：{command}"


def test_readme_links_project_documents() -> None:
    readme = README.read_text(encoding="utf-8")
    for target in ("docs/product-design.md", ".env.example", "AGENTS.md"):
        assert target in readme, f"README 缺少链接：{target}"


def test_readme_version_matches_code() -> None:
    """README 里标注的版本号必须与代码一致。"""
    readme = README.read_text(encoding="utf-8")
    assert f"v{VERSION}" in readme, f"README 未标注当前版本 v{VERSION}"
