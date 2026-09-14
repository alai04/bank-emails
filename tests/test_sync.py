"""Mail fetch and parse orchestration tests."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bank_emails.config import Settings
from bank_emails.mail import MailAttachment, MailMessage
from bank_emails.store import Store
from bank_emails.sync import MailSyncService


class FakeMailClient:
    def __init__(self, messages: list[MailMessage], attachments: list[MailAttachment]) -> None:
        self.messages = messages
        self.attachments = attachments
        self.since: datetime | None = None
        self.sender_domains: tuple[str, ...] = ()
        self.fail: Exception | None = None

    async def fetch_messages(self, since, *, sender_domains=()):  # type: ignore[no-untyped-def]
        if self.fail:
            raise self.fail
        self.since = since
        self.sender_domains = tuple(sender_domains)
        return self.messages

    async def fetch_attachments(self, message, *, max_size_bytes=None):  # type: ignore[no-untyped-def]
        assert message.remote_id
        assert max_size_bytes == 20 * 1024 * 1024
        return self.attachments


def _message(*, attachment: bool = True) -> MailMessage:
    return MailMessage(
        remote_id="message-1",
        internet_message_id="<trade@example.com>",
        subject="Trade Confirmation",
        sender_address="confirm@maybank.com",
        sender_name="Maybank",
        received_at="2026-09-01T02:00:00Z",
        has_attachments=attachment,
        body_content_type="html",
        body_content="<table><tr><td>Symbol</td><td>600887.SH</td></tr></table>",
    )


def test_run_once_stores_message_attachment_and_watermark(
    settings: Settings,
    store: Store,
) -> None:
    csv_bytes = "代码,数量\n600887.SH,500000\n".encode()
    client = FakeMailClient(
        [_message()],
        [
            MailAttachment(
                attachment_id="a1",
                filename="trades.csv",
                content_type="text/csv",
                size_bytes=len(csv_bytes),
                content=csv_bytes,
                sha256=hashlib.sha256(csv_bytes).hexdigest(),
            )
        ],
    )
    service = MailSyncService(settings, store, client=client)  # type: ignore[arg-type]

    result = asyncio.run(service.run_once())

    assert result.fetched == 1
    assert result.stored == 1
    assert result.parsed == 1
    assert result.needs_review == 0
    assert result.watermark == "2026-09-01T02:00:00Z"
    assert store.runtime_state()["watermark"] == "2026-09-01T02:00:00Z"
    email = store.list_emails().items[0]
    assert email["status"] == "PARSED"
    assert "600887.SH" in email["body_text"] if "body_text" in email else True
    detail = store.get_email(email["id"])
    assert detail and "Symbol\t600887.SH" in (detail["body_text"] or "")

    attachments = store.list_attachments(email["id"], with_text=True)
    assert attachments[0]["parse_status"] == "OK"
    assert "代码\t数量" in (attachments[0]["extracted_text"] or "")
    assert Path(attachments[0]["storage_path"]).read_bytes() == csv_bytes
    events = store.list_email_events(email["id"])
    assert [event["to_status"] for event in events] == ["FETCHED", "PARSED"]


def test_second_run_skips_duplicate_and_uses_overlap_window(
    settings: Settings,
    store: Store,
) -> None:
    client = FakeMailClient([_message(attachment=False)], [])
    service = MailSyncService(settings, store, client=client)  # type: ignore[arg-type]

    first = asyncio.run(service.run_once())
    store.set_runtime_state("watermark", "2026-09-01T01:00:00Z")
    second = asyncio.run(service.run_once())

    assert first.stored == 1
    assert second.stored == 0
    assert second.duplicates == 1
    assert store.runtime_state()["watermark"] == "2026-09-01T02:00:00Z"
    assert client.since is not None
    expected = datetime(2026, 9, 1, 0, 55, tzinfo=timezone.utc)
    assert abs(client.since - expected) < timedelta(seconds=1)
    assert store.counts()["emails"] == 1


def test_unsupported_attachment_enters_review(
    settings: Settings,
    store: Store,
) -> None:
    client = FakeMailClient(
        [_message()],
        [
            MailAttachment(
                attachment_id="image-1",
                filename="scan.png",
                content_type="image/png",
                size_bytes=10,
                content=b"\x89PNG\r\n",
                sha256="a" * 64,
            )
        ],
    )
    service = MailSyncService(settings, store, client=client)  # type: ignore[arg-type]

    result = asyncio.run(service.run_once())

    assert result.needs_review == 1
    email = store.list_emails().items[0]
    assert email["status"] == "NEEDS_REVIEW"
    assert "image requires multimodal extraction" in (email["last_error"] or "")


def test_fetch_failure_updates_runtime_state(
    settings: Settings,
    store: Store,
) -> None:
    client = FakeMailClient([], [])
    client.fail = RuntimeError("O365 unavailable")
    service = MailSyncService(settings, store, client=client)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="O365 unavailable"):
        asyncio.run(service.run_once())

    state = store.runtime_state()
    assert state["consecutive_failures"] == "1"
    assert "O365 unavailable" in (state["last_error"] or "")
