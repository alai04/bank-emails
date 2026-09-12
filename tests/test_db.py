"""数据库表结构测试（设计文档 §7）。"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from bank_emails.db import SCHEMA_VERSION, TABLES, connect, init_db, open_database

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "product-design.md"

#: 设计文档 §9.1 规定的 18 个交易字段。
TRADE_COLUMNS: tuple[str, ...] = (
    "issuer_name",
    "account_name",
    "account_no",
    "trade_date",
    "trade_time",
    "settle_date",
    "symbol",
    "symbol_name",
    "isin",
    "market",
    "side",
    "quantity",
    "avg_price",
    "gross_amount",
    "commission",
    "tax",
    "net_amount",
    "broker_ref",
)

#: 交易表里保留的非交易字段（单据属性 + 处理状态）。
NON_TRADE_COLUMNS: tuple[str, ...] = (
    "doc_type",
    "activity_type",
    "is_preliminary",
    "is_amendment",
    "statement_ref",
    "extra_json",
    "external_ref",
    "dedupe_key",
    "push_status",
)


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


def test_all_tables_are_created(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {row["name"] for row in rows}
    assert set(TABLES) <= names


def test_transactions_keep_the_18_trade_columns(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "transactions")
    assert set(TRADE_COLUMNS) <= set(columns)
    # 交易字段必须连续排在主键之后，方便人工核对
    assert columns[3 : 3 + len(TRADE_COLUMNS)] == list(TRADE_COLUMNS)


def test_transactions_keep_supporting_columns(connection: sqlite3.Connection) -> None:
    columns = set(_columns(connection, "transactions"))
    assert set(NON_TRADE_COLUMNS) <= columns
    assert {"id", "email_id", "seq"} <= columns


def test_removed_columns_are_gone(connection: sqlite3.Connection) -> None:
    """精简后不应再有这些字段（见设计文档 §9.1）。"""
    removed = {
        "trade_currency",
        "settle_currency",
        "settlement_amount",
        "exchange_rate",
        "fee_detail_json",
        "fill_detail_json",
        "stamp_duty",
        "fees_total",
        "cash_amount_signed",
        "order_no",
        "invoice_no",
        "sedol",
        "share_class",
        "symbol_name_raw",
        "settlement_direction",
        "portfolio_no",
        "shareholder_account_no",
        "settlement_account_no",
    }
    assert not removed & set(_columns(connection, "transactions"))


def test_unique_index_on_dedupe_key(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
    ).fetchall()
    unique = {row["name"] for row in indexes if "UNIQUE" in (row["sql"] or "").upper()}
    assert "idx_tx_dedupe_key" in unique


def test_pragmas(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_schema_version_is_recorded(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_init_db_is_idempotent(settings) -> None:  # type: ignore[no-untyped-def]
    conn = connect(settings.db_path)
    init_db(conn)
    init_db(conn)
    tables = conn.execute("SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table'").fetchone()
    assert tables["n"] >= len(TABLES)
    conn.close()


def test_open_database_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "db.sqlite"
    conn = open_database(path)
    assert path.exists()
    conn.close()


def test_page_row_factory_returns_dict_like_rows(connection: sqlite3.Connection) -> None:
    assert connection.row_factory is sqlite3.Row


def test_duplicate_dedupe_key_is_rejected(
    connection: sqlite3.Connection, make_email, make_transaction
) -> None:  # type: ignore[no-untyped-def]
    email_id = make_email()
    make_transaction(email_id, seq=1, dedupe_key="same-key")
    with pytest.raises(sqlite3.IntegrityError):
        make_transaction(email_id, seq=2, dedupe_key="same-key")


def test_cascade_delete_removes_children(
    connection: sqlite3.Connection, make_email, make_transaction
) -> None:  # type: ignore[no-untyped-def]
    email_id = make_email()
    make_transaction(email_id, seq=1)
    connection.execute(
        "INSERT INTO attachments (email_id, filename, parse_status) VALUES (?, 'a.pdf', 'PENDING')",
        (email_id,),
    )
    connection.execute(
        "INSERT INTO email_events (email_id, stage, to_status, created_at)"
        " VALUES (?, 'FETCH', 'FETCHED', '2026-09-01T00:00:00Z')",
        (email_id,),
    )
    connection.execute("DELETE FROM emails WHERE id = ?", (email_id,))
    connection.commit()
    for table in ("transactions", "attachments", "email_events"):
        remaining = connection.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE email_id = ?", (email_id,)
        ).fetchone()["n"]
        assert remaining == 0, table


def _doc_transaction_columns() -> list[str]:
    """从设计文档 §7 的 SQL 代码块里读出 transactions 的列名。"""
    text = DOC_PATH.read_text(encoding="utf-8")
    sql_blocks = re.findall(r"```sql\n(.*?)```", text, re.S)
    assert sql_blocks, "设计文档里找不到 sql 代码块"
    for block in sql_blocks:
        match = re.search(
            r"CREATE TABLE (?:IF NOT EXISTS )?transactions \((.*?)\n\);", block, re.S
        )
        if match:
            columns: list[str] = []
            for line in match.group(1).splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                column = re.match(r"([a-z_]+)\s+(INTEGER|TEXT|REAL)", stripped)
                if column:
                    columns.append(column.group(1))
            return columns
    raise AssertionError("设计文档里找不到 transactions 表定义")


def test_schema_matches_design_document(connection: sqlite3.Connection) -> None:
    """代码里的表结构必须与设计文档 §7 保持一致。"""
    assert _columns(connection, "transactions") == _doc_transaction_columns()


def test_design_document_has_18_trade_columns() -> None:
    """设计文档本身也不能悄悄多出交易字段。"""
    documented = _doc_transaction_columns()
    trade_columns = documented[3 : 3 + len(TRADE_COLUMNS)]
    assert trade_columns == list(TRADE_COLUMNS)
