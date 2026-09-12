"""配置管理的测试（设计文档 FR-1）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bank_emails.config import SENSITIVE_FIELDS, ConfigError, Settings, load_settings

from conftest import MINIMAL_ENV


def test_defaults(settings: Settings) -> None:
    assert settings.mail_poll_interval == 300
    assert settings.mail_lookback_days == 7
    assert settings.llm_confidence_threshold == 0.8
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8080
    assert settings.timezone == "Asia/Shanghai"
    assert settings.max_retry == 3
    assert settings.auth_mode == "app"
    assert settings.sender_domains == ()


def test_password_list_is_parsed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_SENDER_FILTER", "Maybank.com, @hsbc.com ,,")
    settings = Settings(_env_file=None)
    assert settings.sender_domains == ("maybank.com", "hsbc.com")


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings(_env_file=None).log_level == "DEBUG"


def test_delegated_auth_is_accepted(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.delenv("M365_CLIENT_SECRET")
    monkeypatch.setenv("M365_REFRESH_TOKEN", "refresh-token")
    settings = Settings(_env_file=None)
    assert settings.auth_mode == "delegated"


def test_missing_llm_key_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(None)
    assert "LLM_API_KEY" in str(excinfo.value)


def test_m365_requires_one_credential(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.delenv("M365_CLIENT_SECRET")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(None)
    message = str(excinfo.value)
    assert "M365_CLIENT_SECRET" in message and "M365_REFRESH_TOKEN" in message


def test_m365_rejects_both_credentials(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setenv("M365_REFRESH_TOKEN", "refresh-token")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(None)
    assert "只能设置一个" in str(excinfo.value)


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("MAIL_POLL_INTERVAL", "10", "MAIL_POLL_INTERVAL"),
        ("MAIL_LOOKBACK_DAYS", "0", "MAIL_LOOKBACK_DAYS"),
        ("LLM_CONFIDENCE_THRESHOLD", "1.5", "LLM_CONFIDENCE_THRESHOLD"),
        ("API_PORT", "70000", "API_PORT"),
        ("LLM_BASE_URL", "llm.example.com", "LLM_BASE_URL"),
        ("ODOO_URL", "ftp://odoo", "ODOO_URL"),
        ("TIMEZONE", "Mars/Olympus", "TIMEZONE"),
        ("LOG_LEVEL", "LOUD", "LOG_LEVEL"),
    ],
)
def test_invalid_values_name_the_variable(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    variable: str,
    value: str,
    expected: str,
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ConfigError) as excinfo:
        load_settings(None)
    assert expected in str(excinfo.value)


def test_env_file_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in MINIMAL_ENV:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in MINIMAL_ENV.items()), encoding="utf-8"
    )
    settings = load_settings(env_file)
    assert settings.m365_mailbox == MINIMAL_ENV["M365_MAILBOX"]


def test_public_dict_masks_every_secret(settings: Settings) -> None:
    public = settings.public_dict()
    configured = {name for name in SENSITIVE_FIELDS if public[name] is not None}
    # 夹具里设置了 client secret、LLM key、Odoo 密码与 API token
    assert configured == {"m365_client_secret", "llm_api_key", "odoo_password", "api_token"}
    for name in configured:
        assert public[name] == "***"
    assert public["m365_refresh_token"] is None
    assert "client-secret" not in str(public)
    assert "odoo-password" not in str(public)
    assert "sk-test" not in str(public)
    assert "test-token" not in str(public)
    # 非敏感项保持原值，便于排查
    assert public["m365_mailbox"] == MINIMAL_ENV["M365_MAILBOX"]
    assert public["llm_model_extract"] == "gpt-4o"


def test_unset_secret_is_reported_as_none(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.delenv("M365_CLIENT_SECRET")
    monkeypatch.setenv("M365_REFRESH_TOKEN", "refresh-token")
    public = Settings(_env_file=None).public_dict()
    assert public["m365_client_secret"] is None
    assert public["m365_refresh_token"] == "***"


def test_ensure_dirs_creates_directories(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "nested" / "db.sqlite",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        **MINIMAL_ENV,
    )
    settings.ensure_dirs()
    assert (tmp_path / "nested").is_dir()
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "logs").is_dir()
