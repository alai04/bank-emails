"""公共测试夹具。

所有测试都使用临时目录下的 SQLite 文件，避免污染仓库里的 data/。
"""

from __future__ import annotations

import sqlite3
from itertools import count
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from bank_emails.api import create_app
from bank_emails.config import Settings
from bank_emails.db import open_database
from bank_emails.store import Store, utcnow_iso

#: 一套能通过校验的最小配置。
MINIMAL_ENV: dict[str, str] = {
    "M365_TENANT_ID": "tenant-id",
    "M365_CLIENT_ID": "client-id",
    "M365_CLIENT_SECRET": "client-secret",
    "M365_MAILBOX": "ops@example.com",
    "LLM_BASE_URL": "https://llm.example.com/v1",
    "LLM_API_KEY": "sk-test",
    "ODOO_URL": "https://odoo.example.com",
    "ODOO_DB": "odoo",
    "ODOO_USER": "svc-bank-emails",
    "ODOO_PASSWORD": "odoo-password",
    "API_TOKEN": "test-token",
}

API_TOKEN = MINIMAL_ENV["API_TOKEN"]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """设置最小可用环境变量，并把存储路径指向临时目录。"""
    values = dict(MINIMAL_ENV)
    values.update(
        {
            "DB_PATH": str(tmp_path / "bank_emails.db"),
            "DATA_DIR": str(tmp_path / "data"),
            "LOG_DIR": str(tmp_path / "logs"),
        }
    )
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture
def settings(env: dict[str, str], tmp_path: Path) -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def connection(settings: Settings) -> sqlite3.Connection:
    conn = open_database(settings.db_path)
    yield conn
    conn.close()


@pytest.fixture
def store(connection: sqlite3.Connection) -> Store:
    return Store(connection)


@pytest.fixture
def client(settings: Settings, store: Store) -> TestClient:
    return TestClient(create_app(settings, store))


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def make_email(connection: sqlite3.Connection) -> Callable[..., int]:
    """插入一封邮件，返回其 id。"""
    counter = count(1)

    def _make(**overrides: Any) -> int:
        data: dict[str, Any] = {
            "mailbox": "ops@example.com",
            "internet_message_id": f"<message-{next(counter)}@example.com>",
            "subject": "Trade Confirmation",
            "sender_address": "no-reply@maybank.com",
            "sender_name": "Maybank Securities",
            "received_at": "2026-09-01T02:00:00Z",
            "folder": "Inbox",
            "has_attachments": 1,
            "status": "PENDING",
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        }
        data.update(overrides)
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        cursor = connection.execute(
            f"INSERT INTO emails ({columns}) VALUES ({placeholders})", tuple(data.values())
        )
        connection.commit()
        return int(cursor.lastrowid)

    return _make


@pytest.fixture
def make_transaction(connection: sqlite3.Connection) -> Callable[..., int]:
    """插入一笔交易，返回其 id。"""

    def _make(email_id: int, **overrides: Any) -> int:
        seq = overrides.pop("seq", 1)
        data: dict[str, Any] = {
            "email_id": email_id,
            "seq": seq,
            "issuer_name": "Maybank Securities Pte Ltd",
            "account_name": "IKARIA GROUP (HK) LIMITED",
            "account_no": "0114460",
            "trade_date": "2026-02-25",
            "trade_time": "06:00:00",
            "settle_date": "2026-02-27",
            "symbol": "MYQ0215OO002",
            "symbol_name": "SOLARVEST HOLDINGS BERHAD",
            "isin": "MYQ0215OO002",
            "market": "BURSA",
            "side": "BUY",
            "quantity": 165000,
            "avg_price": 2.3302,
            "gross_amount": 384483.00,
            "commission": 595.95,
            "tax": 385.00,
            "net_amount": 385463.95,
            "broker_ref": "00005525071CASG1v1",
            "doc_type": "CONFIRMATION",
            "activity_type": "EQUITY_TRADE",
            "external_ref": f"ext-{email_id}-{seq}",
            "dedupe_key": f"dedupe-{email_id}-{seq}",
            "push_status": "PENDING",
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        }
        data.update(overrides)
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        cursor = connection.execute(
            f"INSERT INTO transactions ({columns}) VALUES ({placeholders})", tuple(data.values())
        )
        connection.commit()
        return int(cursor.lastrowid)

    return _make
