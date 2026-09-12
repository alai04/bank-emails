"""REST API 测试（设计文档 §11）。"""

from __future__ import annotations

import sqlite3
from typing import Callable

from fastapi.testclient import TestClient

from bank_emails.store import Store


def test_healthz_needs_no_token(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_writable_database(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["db_ok"] is True
    assert body["writable"] is True
    assert body["tables"] >= 6


def test_every_response_carries_a_trace_id(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers.get("X-Trace-Id")


def test_protected_endpoints_require_a_token(client: TestClient) -> None:
    response = client.get("/api/v1/status")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "UNAUTHORIZED"
    assert error["trace_id"]


def test_wrong_token_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/status", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_status_shape(
    client: TestClient,
    auth_headers: dict[str, str],
    store: Store,
    make_email: Callable[..., int],
    make_transaction: Callable[..., int],
) -> None:
    email_id = make_email(status="PENDING", is_confirmation=1, issuer_name="Maybank")
    make_email(status="NEEDS_REVIEW", last_error="数量×价格≠金额")
    make_transaction(email_id, push_status="PUSHED")
    store.set_runtime_state("watermark", "2026-09-01T02:30:00Z")
    store.set_runtime_state("retry_queue_depth", "2")

    body = client.get("/api/v1/status", headers=auth_headers).json()
    assert body["status"] == "running"
    assert body["watermark"] == "2026-09-01T02:30:00Z"
    assert body["queue"]["pending"] == 1
    assert body["queue"]["retry"] == 2
    assert body["queue"]["needs_review"] == 1
    assert body["emails_by_status"] == {"PENDING": 1, "NEEDS_REVIEW": 1}
    assert body["transactions_by_push_status"] == {"PUSHED": 1}
    assert body["totals"]["emails"] == 2
    assert body["totals"]["pushed"] == 1
    assert body["totals"]["confirmations"] == 1
    assert body["consecutive_failures"] == 0
    assert body["started_at"]


def test_config_endpoint_masks_secrets(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/v1/config", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["api_token"] == "***"
    assert body["odoo_password"] == "***"
    assert body["llm_api_key"] == "***"
    assert body["m365_client_secret"] == "***"
    raw = response.text
    for secret in ("test-token", "odoo-password", "sk-test", "client-secret"):
        assert secret not in raw


def test_email_list_is_paginated_and_filtered(
    client: TestClient,
    auth_headers: dict[str, str],
    make_email: Callable[..., int],
) -> None:
    make_email(subject="A", received_at="2026-09-01T01:00:00Z")
    make_email(subject="B", received_at="2026-09-02T01:00:00Z")
    make_email(
        subject="C",
        received_at="2026-09-03T01:00:00Z",
        sender_address="alerts@hsbc.com",
        status="PUSHED",
    )

    first = client.get("/api/v1/emails?page_size=2", headers=auth_headers).json()
    assert first["total"] == 3
    assert first["page"] == 1
    assert first["page_size"] == 2
    assert [item["subject"] for item in first["items"]] == ["C", "B"]

    second = client.get("/api/v1/emails?page=2&page_size=2", headers=auth_headers).json()
    assert [item["subject"] for item in second["items"]] == ["A"]

    by_status = client.get("/api/v1/emails?status=PUSHED", headers=auth_headers).json()
    assert by_status["total"] == 1

    by_sender = client.get("/api/v1/emails?sender=hsbc", headers=auth_headers).json()
    assert by_sender["total"] == 1
    assert by_sender["items"][0]["subject"] == "C"

    by_range = client.get(
        "/api/v1/emails?from=2026-09-02T00:00:00Z&to=2026-09-02T23:59:59Z", headers=auth_headers
    ).json()
    assert [item["subject"] for item in by_range["items"]] == ["B"]


def test_email_list_rejects_unknown_status(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/v1/emails?status=NOPE", headers=auth_headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_STATUS"


def test_email_list_rejects_bad_page_size(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/v1/emails?page_size=500", headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_email_detail_includes_relations(
    client: TestClient,
    auth_headers: dict[str, str],
    connection: sqlite3.Connection,
    make_email: Callable[..., int],
    make_transaction: Callable[..., int],
) -> None:
    email_id = make_email()
    make_transaction(email_id, seq=1)
    connection.execute(
        "INSERT INTO attachments (email_id, filename, content_type, parse_status)"
        " VALUES (?, 'confirmation.pdf', 'application/pdf', 'OK')",
        (email_id,),
    )
    connection.execute(
        "INSERT INTO email_events (email_id, stage, from_status, to_status, created_at)"
        " VALUES (?, 'CLASSIFY', 'PARSED', 'CLASSIFIED', '2026-09-01T02:01:00Z')",
        (email_id,),
    )
    connection.commit()

    body = client.get(f"/api/v1/emails/{email_id}", headers=auth_headers).json()
    assert body["email"]["id"] == email_id
    assert body["attachments"][0]["filename"] == "confirmation.pdf"
    assert body["events"][0]["stage"] == "CLASSIFY"
    assert body["transactions"][0]["side"] == "BUY"
    # 交易字段按设计文档精简为 18 列
    assert "settle_currency" not in body["transactions"][0]
    assert body["transactions"][0]["commission"] == 595.95


def test_email_detail_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/v1/emails/999", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EMAIL_NOT_FOUND"


def test_attachment_detail(
    client: TestClient,
    auth_headers: dict[str, str],
    connection: sqlite3.Connection,
    make_email: Callable[..., int],
) -> None:
    email_id = make_email()
    cursor = connection.execute(
        "INSERT INTO attachments (email_id, filename, parse_status) VALUES (?, 'a.pdf', 'OK')",
        (email_id,),
    )
    connection.commit()
    attachment_id = int(cursor.lastrowid)

    ok = client.get(f"/api/v1/emails/{email_id}/attachments/{attachment_id}", headers=auth_headers)
    assert ok.status_code == 200
    assert ok.json()["filename"] == "a.pdf"

    missing = client.get(f"/api/v1/emails/{email_id}/attachments/999", headers=auth_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ATTACHMENT_NOT_FOUND"


def test_transaction_list_and_filters(
    client: TestClient,
    auth_headers: dict[str, str],
    make_email: Callable[..., int],
    make_transaction: Callable[..., int],
) -> None:
    email_id = make_email()
    make_transaction(email_id, seq=1, symbol="600887.SH", issuer_name="华泰金融控股（香港）")
    make_transaction(
        email_id,
        seq=2,
        symbol="002270.SZ",
        issuer_name="广发证券（香港）",
        push_status="FAILED",
        trade_date="2026-08-26",
    )

    all_items = client.get("/api/v1/transactions", headers=auth_headers).json()
    assert all_items["total"] == 2

    failed = client.get("/api/v1/transactions?push_status=FAILED", headers=auth_headers).json()
    assert failed["total"] == 1
    assert failed["items"][0]["symbol"] == "002270.SZ"

    by_symbol = client.get("/api/v1/transactions?symbol=600887", headers=auth_headers).json()
    assert by_symbol["total"] == 1

    by_issuer = client.get(
        "/api/v1/transactions?issuer_name=%E5%B9%BF%E5%8F%91", headers=auth_headers
    ).json()
    assert by_issuer["total"] == 1

    by_range = client.get(
        "/api/v1/transactions?from=2026-08-01&to=2026-08-31", headers=auth_headers
    ).json()
    assert by_range["total"] == 1
    assert by_range["items"][0]["trade_date"] == "2026-08-26"

    bad = client.get("/api/v1/transactions?push_status=NOPE", headers=auth_headers)
    assert bad.status_code == 400


def test_transaction_detail_and_404(
    client: TestClient,
    auth_headers: dict[str, str],
    make_email: Callable[..., int],
    make_transaction: Callable[..., int],
) -> None:
    email_id = make_email()
    transaction_id = make_transaction(email_id, seq=1)
    ok = client.get(f"/api/v1/transactions/{transaction_id}", headers=auth_headers)
    assert ok.status_code == 200
    assert ok.json()["net_amount"] == 385463.95
    assert ok.json()["tax"] == 385.0

    missing = client.get("/api/v1/transactions/999", headers=auth_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"


def test_review_queue_lists_failed_records(
    client: TestClient,
    auth_headers: dict[str, str],
    make_email: Callable[..., int],
    make_transaction: Callable[..., int],
) -> None:
    email_id = make_email(status="NEEDS_REVIEW", last_error="校验不通过")
    make_transaction(email_id, seq=1, push_status="FAILED", push_error="Odoo 超时")

    body = client.get("/api/v1/review", headers=auth_headers).json()
    assert body["total"] == 2
    kinds = {item["kind"] for item in body["items"]}
    assert kinds == {"email", "transaction"}


def test_stats_aggregates_by_range(
    client: TestClient,
    auth_headers: dict[str, str],
    make_email: Callable[..., int],
    make_transaction: Callable[..., int],
) -> None:
    email_id = make_email(is_confirmation=1, issuer_name="Maybank")
    make_transaction(email_id, seq=1, issuer_name="Maybank", net_amount=385463.95)
    make_transaction(
        email_id,
        seq=2,
        issuer_name="HSBC",
        net_amount=100.0,
        activity_type="CASH_MOVEMENT",
        dedupe_key="deposit-1",
    )

    body = client.get("/api/v1/stats", headers=auth_headers).json()
    assert body["emails"]["total"] == 1
    assert body["emails"]["confirmations"] == 1
    assert body["transactions"]["total"] == 2
    assert body["transactions"]["trades"] == 1
    assert body["transactions"]["by_issuer"] == {"Maybank": 1}
    assert body["transactions"]["net_amount_total"] == 385563.95
    assert body["llm"]["calls"] == 0

    outside = client.get(
        "/api/v1/stats?from=2030-01-01T00:00:00Z", headers=auth_headers
    ).json()
    assert outside["emails"]["total"] == 0
    assert outside["transactions"]["total"] == 0


def test_not_implemented_endpoints_are_explicit(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    for method, path in (
        ("post", "/api/v1/jobs/fetch"),
        ("post", "/api/v1/emails/1/reprocess"),
        ("post", "/api/v1/transactions/1/push"),
        ("post", "/api/v1/review/1"),
    ):
        response = getattr(client, method)(path, headers=auth_headers)
        assert response.status_code == 501, path
        assert response.json()["error"]["code"] == "NOT_IMPLEMENTED", path
