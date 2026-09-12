"""配置管理。

所有运行参数从 `.env` 文件或环境变量读取（环境变量优先），启动时一次性校验完毕。
校验失败时抛出 :class:`ConfigError`，错误信息中会指明出错的环境变量名，
便于直接定位到 `.env` 里缺失或写错的字段（设计文档 FR-1）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 对外展示与日志中必须脱敏的字段（设计文档 §11 `/api/v1/config`）。
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "m365_client_secret",
        "m365_refresh_token",
        "llm_api_key",
        "odoo_password",
        "api_token",
    }
)

LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigError(RuntimeError):
    """配置缺失或非法。消息中应包含具体变量名，供启动失败时直接展示。"""


class Settings(BaseSettings):
    """daemon 的全部运行配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Microsoft 365 邮箱 ----
    m365_tenant_id: str
    m365_client_id: str
    m365_client_secret: str | None = None
    m365_refresh_token: str | None = None
    m365_mailbox: str
    m365_folder: str = "Inbox"
    mail_lookback_days: int = Field(default=7, ge=1, le=365)
    mail_poll_interval: int = Field(default=300, ge=30)
    mail_sender_filter: str = ""
    max_attachment_mb: int = Field(default=20, ge=1)

    # ---- LLM ----
    llm_base_url: str
    llm_api_key: str
    llm_model_classify: str = "gpt-4o-mini"
    llm_model_extract: str = "gpt-4o"
    llm_confidence_threshold: float = Field(default=0.8, ge=0, le=1)
    llm_timeout: int = Field(default=60, ge=1)

    # ---- Odoo ----
    odoo_url: str
    odoo_db: str
    odoo_user: str
    odoo_password: str
    odoo_mapping_file: Path = Path("mapping.yaml")

    # ---- REST API ----
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_token: str

    # ---- 存储、日志与运行策略 ----
    db_path: Path = Path("./data/bank_emails.db")
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    log_dir: Path = Path("./logs")
    timezone: str = "Asia/Shanghai"
    max_retry: int = Field(default=3, ge=0)
    retain_raw_days: int = Field(default=90, ge=0)

    @property
    def sender_domains(self) -> tuple[str, ...]:
        """`MAIL_SENDER_FILTER` 解析为小写域名元组，空串表示不过滤。"""
        return tuple(
            part.strip().lstrip("@").lower()
            for part in self.mail_sender_filter.split(",")
            if part.strip()
        )

    @property
    def auth_mode(self) -> str:
        """返回 ``"app"``（应用认证）或 ``"delegated"``（委派认证）。"""
        return "app" if self.m365_client_secret else "delegated"

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("mail_sender_filter", mode="before")
    @classmethod
    def _normalize_sender_filter(cls, value: Any) -> Any:
        return value or ""

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if not self.m365_client_secret and not self.m365_refresh_token:
            raise ValueError(
                "M365_CLIENT_SECRET 与 M365_REFRESH_TOKEN 至少要设置一个"
                "（应用认证用前者，委派认证用后者）"
            )
        if self.m365_client_secret and self.m365_refresh_token:
            raise ValueError(
                "M365_CLIENT_SECRET 与 M365_REFRESH_TOKEN 只能设置一个，请二选一"
            )
        for field, value in (("LLM_BASE_URL", self.llm_base_url), ("ODOO_URL", self.odoo_url)):
            if not value.startswith(("http://", "https://")):
                raise ValueError(f"{field} 必须以 http:// 或 https:// 开头，当前为 {value!r}")
        if self.log_level not in LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL 必须是 {'/'.join(sorted(LOG_LEVELS))} 之一，当前为 {self.log_level!r}"
            )
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"TIMEZONE 不是有效的时区标识：{self.timezone!r}") from exc
        return self

    # ---- 对外展示 ----
    def public_dict(self) -> dict[str, Any]:
        """可安全暴露给 REST API 的配置快照：密钥只显示是否已配置。"""
        data = self.model_dump(mode="json")
        for name in SENSITIVE_FIELDS:
            data[name] = "***" if data.get(name) else None
        return data

    def ensure_dirs(self) -> None:
        """创建数据目录、日志目录与数据库所在目录。"""
        for path in (self.data_dir, self.log_dir, Path(self.db_path).parent):
            Path(path).mkdir(parents=True, exist_ok=True)


def _format_validation_error(exc: ValidationError) -> str:
    """把 pydantic 的错误转成"哪个变量错了"的中文清单。"""
    lines: list[str] = []
    for error in exc.errors():
        loc = error.get("loc") or ()
        if loc:
            name = ".".join(str(part) for part in loc).upper()
            lines.append(f"- {name}: {error.get('msg')}")
        else:
            lines.append(f"- {error.get('msg')}")
    return "配置校验失败：\n" + "\n".join(lines) if lines else "配置校验失败"


def load_settings(env_file: str | Path | None = ".env", **overrides: Any) -> Settings:
    """加载并校验配置；失败时抛出带变量名的 :class:`ConfigError`。"""
    try:
        return Settings(_env_file=env_file, **overrides)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
