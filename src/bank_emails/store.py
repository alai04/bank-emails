"""数据访问层。

REST API 目前只读（除运行态水位线外），后台流水线落地后复用同一套读写方法，
保证 SQL 只出现在这一层。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

#: 邮件状态取值（设计文档附录 B）。
EMAIL_STATUSES: tuple[str, ...] = (
    "PENDING",
    "FETCHED",
    "PARSED",
    "CLASSIFIED",
    "EXTRACTED",
    "VALIDATED",
    "SKIPPED",
    "PUSHED",
    "FAILED",
    "NEEDS_REVIEW",
)

#: 交易推送状态取值。
PUSH_STATUSES: tuple[str, ...] = ("PENDING", "VALIDATED", "PUSHED", "FAILED", "NEEDS_REVIEW")

#: 视为"需要人工介入"的邮件状态。
REVIEW_STATUSES: tuple[str, ...] = ("FAILED", "NEEDS_REVIEW")

#: 需要把 JSON 文本解析成对象的列。
JSON_COLUMNS: tuple[str, ...] = ("extra_json", "validation_errors")


def utcnow_iso() -> str:
    """当前时间的 ISO8601 UTC 字符串（秒级，带 Z）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _range_clauses(
    column: str, date_from: str | None, date_to: str | None
) -> tuple[list[str], list[Any]]:
    """时间区间过滤条件；`column` 由调用方给出，值走参数绑定。"""
    clauses: list[str] = []
    params: list[Any] = []
    if date_from:
        clauses.append(f"{column} >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{column} <= ?")
        params.append(date_to)
    return clauses, params


def _where(clauses: Sequence[str]) -> str:
    return f"WHERE {' AND '.join(clauses)}" if clauses else ""


@dataclass(frozen=True)
class Page:
    """分页结果，对应 REST API 的 `{items, page, page_size, total}` 信封。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    """sqlite3.Row → dict，并把 JSON 文本列解析成对象。"""
    data = dict(row)
    for column in JSON_COLUMNS:
        if isinstance(data.get(column), str):
            try:
                data[column] = json.loads(data[column])
            except json.JSONDecodeError:
                pass
    return data


class Store:
    """基于 SQLite 的读写封装。

    连接是跨线程共享的（API 线程池 + 后台流水线），写操作统一用 `write_lock` 串行化，
    避免两个线程同时 `BEGIN` 造成 "database is locked"。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.write_lock = threading.RLock()

    # ------------------------------------------------------------------ 运行态
    def runtime_state(self) -> dict[str, str]:
        rows = self.connection.execute("SELECT key, value FROM runtime_state").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_runtime_state(self, key: str, value: str | None) -> None:
        with self.write_lock:
            self.connection.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utcnow_iso()),
            )
            self.connection.commit()

    def _set_runtime_states(self, values: dict[str, str | None]) -> None:
        now = utcnow_iso()
        with self.write_lock:
            self.connection.executemany(
                """
                INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                ((key, value, now) for key, value in values.items()),
            )
            self.connection.commit()

    def check_health(self) -> dict[str, Any]:
        """就绪探针使用：能否查询、能否写入。"""
        result: dict[str, Any] = {"db_ok": False, "writable": False, "tables": 0}
        with self.write_lock:
            try:
                self.connection.execute("SELECT 1").fetchone()
                result["db_ok"] = True
                result["tables"] = self.connection.execute(
                    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table'"
                ).fetchone()["n"]
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "INSERT INTO runtime_state (key, value, updated_at)"
                    " VALUES ('__healthcheck__', '1', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (utcnow_iso(),),
                )
                self.connection.execute("DELETE FROM runtime_state WHERE key = '__healthcheck__'")
                self.connection.rollback()
                result["writable"] = True
            except sqlite3.Error as exc:  # pragma: no cover - 仅在磁盘/权限异常时触发
                result["error"] = str(exc)
        return result

    # -------------------------------------------------------------------- 统计
    def counts(self) -> dict[str, int]:
        """各表记录数与关键汇总，供 /status 与 /stats 使用。"""
        result: dict[str, int] = {}
        for table in ("emails", "attachments", "transactions", "llm_calls"):
            result[table] = self._scalar(f"SELECT COUNT(*) FROM {table}", ()) or 0
        result["confirmations"] = (
            self._scalar("SELECT COUNT(*) FROM emails WHERE is_confirmation = 1", ()) or 0
        )
        result["pushed"] = (
            self._scalar("SELECT COUNT(*) FROM transactions WHERE push_status = 'PUSHED'", ()) or 0
        )
        return result

    def email_counts_by_status(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS n FROM emails GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def transaction_counts_by_push_status(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT push_status, COUNT(*) AS n FROM transactions GROUP BY push_status"
        ).fetchall()
        return {row["push_status"]: row["n"] for row in rows}

    def stats(self, date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
        """区间统计：邮件/交易数量、状态分布、按机构分布、LLM 用量与费用。"""
        email_clauses, email_params = _range_clauses("created_at", date_from, date_to)
        tx_clauses, tx_params = _range_clauses("t.created_at", date_from, date_to)
        llm_clauses, llm_params = _range_clauses("l.created_at", date_from, date_to)

        confirmation_clauses = [*email_clauses, "is_confirmation = 1"]
        trade_clauses = [*tx_clauses, "t.activity_type = 'EQUITY_TRADE'"]
        issuer_clauses = [*trade_clauses]

        return {
            "range": {"from": date_from, "to": date_to},
            "emails": {
                "total": self._scalar(f"SELECT COUNT(*) FROM emails {_where(email_clauses)}", email_params) or 0,
                "confirmations": self._scalar(
                    f"SELECT COUNT(*) FROM emails {_where(confirmation_clauses)}", email_params
                )
                or 0,
                "by_status": {
                    row["status"]: row["n"]
                    for row in self.connection.execute(
                        f"SELECT status, COUNT(*) AS n FROM emails {_where(email_clauses)} GROUP BY status",
                        email_params,
                    ).fetchall()
                },
                "by_issuer": {
                    row["issuer_name"]: row["n"]
                    for row in self.connection.execute(
                        f"SELECT COALESCE(issuer_name, 'UNKNOWN') AS issuer_name, COUNT(*) AS n "
                        f"FROM emails {_where(email_clauses)} GROUP BY issuer_name",
                        email_params,
                    ).fetchall()
                },
            },
            "transactions": {
                "total": self._scalar(
                    f"SELECT COUNT(*) FROM transactions t {_where(tx_clauses)}", tx_params
                )
                or 0,
                "trades": self._scalar(
                    f"SELECT COUNT(*) FROM transactions t {_where(trade_clauses)}", tx_params
                )
                or 0,
                "by_push_status": {
                    row["push_status"]: row["n"]
                    for row in self.connection.execute(
                        f"SELECT push_status, COUNT(*) AS n FROM transactions t "
                        f"{_where(tx_clauses)} GROUP BY push_status",
                        tx_params,
                    ).fetchall()
                },
                "by_issuer": {
                    row["issuer_name"]: row["n"]
                    for row in self.connection.execute(
                        f"SELECT COALESCE(t.issuer_name, 'UNKNOWN') AS issuer_name, COUNT(*) AS n "
                        f"FROM transactions t {_where(issuer_clauses)} GROUP BY issuer_name",
                        tx_params,
                    ).fetchall()
                },
                "net_amount_total": self._scalar(
                    f"SELECT COALESCE(SUM(t.net_amount), 0) FROM transactions t {_where(tx_clauses)}",
                    tx_params,
                )
                or 0,
            },
            "llm": {
                "calls": self._scalar(
                    f"SELECT COUNT(*) FROM llm_calls l {_where(llm_clauses)}", llm_params
                )
                or 0,
                "prompt_tokens": self._scalar(
                    f"SELECT COALESCE(SUM(l.prompt_tokens), 0) FROM llm_calls l {_where(llm_clauses)}",
                    llm_params,
                )
                or 0,
                "completion_tokens": self._scalar(
                    f"SELECT COALESCE(SUM(l.completion_tokens), 0) FROM llm_calls l {_where(llm_clauses)}",
                    llm_params,
                )
                or 0,
            },
        }

    def review_queue(self, limit: int = 50) -> list[dict[str, Any]]:
        """需要人工介入的记录：邮件级失败/待复核 + 交易级推送失败。"""
        placeholders = ",".join("?" for _ in REVIEW_STATUSES)
        emails = self.connection.execute(
            f"""
            SELECT id, 'email' AS kind, subject, sender_address, received_at,
                   status AS review_status, last_error AS reason
            FROM emails WHERE status IN ({placeholders})
            ORDER BY received_at DESC LIMIT ?
            """,
            (*REVIEW_STATUSES, limit),
        ).fetchall()
        transactions = self.connection.execute(
            """
            SELECT id, 'transaction' AS kind, symbol AS subject, issuer_name AS sender_address,
                   trade_date AS received_at, push_status AS review_status, push_error AS reason
            FROM transactions WHERE push_status IN ('FAILED', 'NEEDS_REVIEW')
            ORDER BY trade_date DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_decode(row) for row in (*emails, *transactions)]

    # -------------------------------------------------------------------- 邮件
    def list_emails(
        self,
        *,
        status: str | None = None,
        sender: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Page:
        clauses, params = _range_clauses("received_at", date_from, date_to)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if sender:
            clauses.append("(sender_address LIKE ? OR sender_name LIKE ?)")
            params.extend([f"%{sender}%", f"%{sender}%"])
        sql_where = _where(clauses)

        total = self._scalar(f"SELECT COUNT(*) FROM emails {sql_where}", params) or 0
        rows = self.connection.execute(
            f"""
            SELECT id, mailbox, subject, sender_address, sender_name, received_at, folder,
                   has_attachments, status, retry_count, next_retry_at, last_error,
                   is_confirmation, issuer_name, issuer_type, llm_confidence, created_at, updated_at
            FROM emails {sql_where}
            ORDER BY received_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return Page(items=[_decode(row) for row in rows], total=total, page=page, page_size=page_size)

    def get_email(self, email_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
        return _decode(row) if row else None

    def list_email_events(self, email_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM email_events WHERE email_id = ? ORDER BY id", (email_id,)
        ).fetchall()
        return [_decode(row) for row in rows]

    def list_attachments(self, email_id: int, *, with_text: bool = False) -> list[dict[str, Any]]:
        columns = (
            "*"
            if with_text
            else "id, email_id, filename, content_type, size_bytes, sha256, parse_status, parse_error"
        )
        rows = self.connection.execute(
            f"SELECT {columns} FROM attachments WHERE email_id = ? ORDER BY id", (email_id,)
        ).fetchall()
        return [_decode(row) for row in rows]

    def get_attachment(self, email_id: int, attachment_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM attachments WHERE email_id = ? AND id = ?", (email_id, attachment_id)
        ).fetchone()
        return _decode(row) if row else None

    def upsert_fetched_email(
        self,
        *,
        mailbox: str,
        internet_message_id: str,
        graph_id: str,
        subject: str,
        sender_address: str,
        sender_name: str,
        received_at: str,
        folder: str,
        has_attachments: bool,
        body_text: str,
        body_sha256: str,
    ) -> tuple[int, bool]:
        """Insert a fetched email once; return ``(email_id, created)``."""
        with self.write_lock:
            existing = self.connection.execute(
                "SELECT id FROM emails WHERE mailbox = ? AND internet_message_id = ?",
                (mailbox, internet_message_id),
            ).fetchone()
            if existing:
                return int(existing["id"]), False

            now = utcnow_iso()
            cursor = self.connection.execute(
                """
                INSERT INTO emails (
                    mailbox, internet_message_id, graph_id, subject, sender_address, sender_name,
                    received_at, folder, has_attachments, body_text, body_sha256, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FETCHED', ?, ?)
                """,
                (
                    mailbox,
                    internet_message_id,
                    graph_id,
                    subject,
                    sender_address,
                    sender_name,
                    received_at,
                    folder,
                    int(has_attachments),
                    body_text,
                    body_sha256,
                    now,
                    now,
                ),
            )
            email_id = int(cursor.lastrowid)
            self.connection.execute(
                """
                INSERT INTO email_events (email_id, stage, from_status, to_status, message, created_at)
                VALUES (?, 'FETCH', NULL, 'FETCHED', ?, ?)
                """,
                (email_id, None, now),
            )
            self.connection.commit()
            return email_id, True

    def replace_attachments(self, email_id: int, attachments: Sequence[dict[str, Any]]) -> None:
        """Replace attachment metadata and extracted text for a newly fetched email."""
        with self.write_lock:
            self.connection.execute("DELETE FROM attachments WHERE email_id = ?", (email_id,))
            self.connection.executemany(
                """
                INSERT INTO attachments (
                    email_id, filename, content_type, size_bytes, sha256, storage_path,
                    extracted_text, parse_status, parse_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        email_id,
                        item.get("filename"),
                        item.get("content_type"),
                        item.get("size_bytes"),
                        item.get("sha256"),
                        item.get("storage_path"),
                        item.get("extracted_text"),
                        item.get("parse_status", "PENDING"),
                        item.get("parse_error"),
                    )
                    for item in attachments
                ),
            )
            self.connection.commit()

    def finish_email_parse(
        self,
        email_id: int,
        *,
        status: str,
        body_text: str,
        body_sha256: str,
        error: str | None = None,
    ) -> None:
        if status not in {"PARSED", "NEEDS_REVIEW"}:
            raise ValueError(f"invalid post-parse status: {status}")
        with self.write_lock:
            current = self.connection.execute(
                "SELECT status FROM emails WHERE id = ?", (email_id,)
            ).fetchone()
            if not current:
                raise KeyError(f"email {email_id} does not exist")
            now = utcnow_iso()
            self.connection.execute(
                """
                UPDATE emails
                SET status = ?, body_text = ?, body_sha256 = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, body_text, body_sha256, error, now, email_id),
            )
            self.connection.execute(
                """
                INSERT INTO email_events (email_id, stage, from_status, to_status, message, created_at)
                VALUES (?, 'PARSE', ?, ?, ?, ?)
                """,
                (email_id, current["status"], status, error, now),
            )
            self.connection.commit()

    def record_sync_success(self, watermark: str | None) -> None:
        values = {
            "last_success_at": utcnow_iso(),
            "last_error": None,
            "consecutive_failures": "0",
        }
        if watermark:
            values["watermark"] = watermark
        self._set_runtime_states(values)

    def record_sync_failure(self, error: str) -> int:
        state = self.runtime_state()
        failures = int(state.get("consecutive_failures") or 0) + 1
        self._set_runtime_states(
            {
                "last_error": error,
                "last_failure_at": utcnow_iso(),
                "consecutive_failures": str(failures),
            }
        )
        return failures

    # -------------------------------------------------------------------- 交易
    def list_transactions(
        self,
        *,
        push_status: str | None = None,
        symbol: str | None = None,
        issuer_name: str | None = None,
        email_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Page:
        clauses, params = _range_clauses("trade_date", date_from, date_to)
        if push_status:
            clauses.append("push_status = ?")
            params.append(push_status)
        if symbol:
            clauses.append("(symbol LIKE ? OR isin LIKE ?)")
            params.extend([f"%{symbol}%", f"%{symbol}%"])
        if issuer_name:
            clauses.append("issuer_name LIKE ?")
            params.append(f"%{issuer_name}%")
        if email_id is not None:
            clauses.append("email_id = ?")
            params.append(email_id)
        sql_where = _where(clauses)

        total = self._scalar(f"SELECT COUNT(*) FROM transactions {sql_where}", params) or 0
        rows = self.connection.execute(
            f"""
            SELECT id, email_id, seq, issuer_name, account_name, account_no, trade_date, trade_time,
                   settle_date, symbol, symbol_name, isin, market, side, quantity, avg_price,
                   gross_amount, commission, tax, net_amount, broker_ref, doc_type, activity_type,
                   is_preliminary, is_amendment, statement_ref, source_type, source_ref, confidence,
                   valid, validation_errors, external_ref, dedupe_key, odoo_model, odoo_id,
                   push_status, push_error, created_at, updated_at
            FROM transactions {sql_where}
            ORDER BY trade_date DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return Page(items=[_decode(row) for row in rows], total=total, page=page, page_size=page_size)

    def list_email_transactions(self, email_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM transactions WHERE email_id = ? ORDER BY seq", (email_id,)
        ).fetchall()
        return [_decode(row) for row in rows]

    def get_transaction(self, transaction_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
        ).fetchone()
        return _decode(row) if row else None

    # -------------------------------------------------------------------- 工具
    def _scalar(self, sql: str, params: Sequence[Any]) -> Any:
        row = self.connection.execute(sql, params).fetchone()
        return row[0] if row else None
