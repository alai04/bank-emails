"""SQLite 表结构与连接管理。

表结构对应设计文档 §7，仅做两处实现层面的调整：

1. 所有 `CREATE` 语句加 `IF NOT EXISTS`，让 `init_db()` 可以重复调用；
2. 把设计文档"索引建议"里提到的索引一并建出来。

金额列统一使用 `REAL` 存储、Python 侧用 `Decimal` 读取（见设计文档 §7 说明）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: 表结构版本，写入 `PRAGMA user_version`，供后续迁移判断。
SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- 邮件主表
CREATE TABLE IF NOT EXISTS emails (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox             TEXT    NOT NULL,            -- 邮箱标识
    internet_message_id TEXT    NOT NULL,            -- Graph internetMessageId
    graph_id            TEXT,                        -- Graph 消息 id（可变，仅调试用）
    subject             TEXT,
    sender_address      TEXT,
    sender_name         TEXT,
    received_at         TEXT    NOT NULL,            -- ISO8601 UTC
    folder              TEXT,
    has_attachments     INTEGER NOT NULL DEFAULT 0,
    body_text           TEXT,                        -- 解析后的纯文本（可裁剪）
    body_sha256         TEXT,
    status              TEXT    NOT NULL DEFAULT 'PENDING',
    retry_count         INTEGER NOT NULL DEFAULT 0,
    next_retry_at       TEXT,
    last_error          TEXT,
    is_confirmation     INTEGER,                     -- 分类结果：1/0/NULL
    issuer_name         TEXT,
    issuer_type         TEXT,                        -- broker | bank | other
    llm_confidence      REAL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (mailbox, internet_message_id)
);

-- 附件
CREATE TABLE IF NOT EXISTS attachments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id       INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    filename       TEXT,
    content_type   TEXT,
    size_bytes     INTEGER,
    sha256         TEXT,
    storage_path   TEXT,                             -- 落盘路径，可为空
    extracted_text TEXT,                             -- 解析后的可读文本
    parse_status   TEXT NOT NULL DEFAULT 'PENDING',
    parse_error    TEXT
);

-- 交易明细（一封邮件可含多份 advice、多笔成交、多个品种）
-- 交易字段只有 18 列，其余为单据属性、处理状态等非交易数据。
CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id          INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL,        -- 邮件内序号，从 1 开始

    -- ===== 交易数据（18 列）=====
    issuer_name       TEXT,
    account_name      TEXT,
    account_no        TEXT,
    trade_date        TEXT,                    -- YYYY-MM-DD
    trade_time        TEXT,                    -- HH:MM:SS，统一 UTC
    settle_date       TEXT,                    -- 交割日 / Value Date
    symbol            TEXT,
    symbol_name       TEXT,
    isin              TEXT,
    market            TEXT,
    side              TEXT,                    -- BUY | SELL | OTHER
    quantity          REAL,
    avg_price         REAL,
    gross_amount      REAL,
    commission        REAL,                    -- 除税费以外的所有费用加总
    tax               REAL,                    -- 所有税费加总
    net_amount        REAL,
    broker_ref        TEXT,

    -- ===== 单据属性（非交易数据）=====
    doc_type          TEXT,
    activity_type     TEXT NOT NULL DEFAULT 'EQUITY_TRADE',
    is_preliminary    INTEGER NOT NULL DEFAULT 0,
    is_amendment      INTEGER NOT NULL DEFAULT 0,
    statement_ref     TEXT,
    extra_json        TEXT,

    -- ===== 处理与推送状态（非交易数据）=====
    source_type       TEXT,
    source_ref        TEXT,
    confidence        REAL,
    valid             INTEGER NOT NULL DEFAULT 0,
    validation_errors TEXT,
    external_ref      TEXT NOT NULL,
    dedupe_key        TEXT NOT NULL,
    odoo_model        TEXT,
    odoo_id           INTEGER,
    push_status       TEXT NOT NULL DEFAULT 'PENDING',
    push_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (email_id, seq)
);

-- 状态轨迹
CREATE TABLE IF NOT EXISTS email_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id    INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    message     TEXT,
    created_at  TEXT NOT NULL
);

-- LLM 调用审计与成本
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id          INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    purpose           TEXT NOT NULL,                 -- classify | extract
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    latency_ms        INTEGER,
    success           INTEGER NOT NULL,
    error             TEXT,
    created_at        TEXT NOT NULL
);

-- 运行态与水位线
CREATE TABLE IF NOT EXISTS runtime_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_dedupe_key  ON transactions(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_tx_push_status        ON transactions(push_status);
CREATE INDEX IF NOT EXISTS idx_tx_symbol_date        ON transactions(symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_emails_status_retry   ON emails(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_emails_received_at    ON emails(received_at);
CREATE INDEX IF NOT EXISTS idx_attachments_email     ON attachments(email_id);
CREATE INDEX IF NOT EXISTS idx_email_events_email    ON email_events(email_id);
"""

#: 设计文档 §7 的表清单，供测试与运维自检使用。
TABLES: tuple[str, ...] = (
    "emails",
    "attachments",
    "transactions",
    "email_events",
    "llm_calls",
    "runtime_state",
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开数据库连接并设置运行时 PRAGMA。

    `journal_mode=WAL` 让 REST API 与后台流水线可以并发读写同一份库。
    `check_same_thread=False`：API 请求在 FastAPI 线程池里执行，与主线程共用连接；
    写操作由 :class:`~bank_emails.store.Store` 的锁串行化，读操作交给 SQLite 自身的并发控制。
    """
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    """建表建索引，可重复执行。"""
    connection.executescript(SCHEMA_SQL)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def open_database(db_path: str | Path) -> sqlite3.Connection:
    """打开连接并确保表结构已就绪。"""
    connection = connect(db_path)
    init_db(connection)
    return connection
