"""Mail fetch and parse orchestration for milestone M1."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .mail import MailAttachment, MailMessage, O365MailClient
from .parser import PARSE_FAILED, PARSE_OK, PARSE_SKIPPED, parse_attachment, parse_body
from .store import Store

logger = logging.getLogger("bank_emails.sync")

OVERLAP = timedelta(minutes=5)


class SyncAlreadyRunning(RuntimeError):
    """Raised when a manual and scheduled fetch overlap."""


@dataclass(frozen=True)
class SyncResult:
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    parsed: int = 0
    needs_review: int = 0
    failed: int = 0
    watermark: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class MailSyncService:
    """Fetch new python-o365 messages, persist them, and parse their contents."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        client: O365MailClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self._run_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    def _since(self) -> datetime:
        state = self.store.runtime_state()
        watermark = state.get("watermark")
        if not watermark:
            return datetime.now(timezone.utc) - timedelta(days=self.settings.mail_lookback_days)
        try:
            parsed = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("invalid watermark %r; falling back to lookback window", watermark)
            return datetime.now(timezone.utc) - timedelta(days=self.settings.mail_lookback_days)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) - OVERLAP

    async def run_once(self) -> SyncResult:
        if self._run_lock.locked():
            raise SyncAlreadyRunning("a mail fetch is already running")
        async with self._run_lock:
            state = self.store.runtime_state()
            previous_watermark = state.get("watermark")
            watermark = previous_watermark
            fetched = stored = duplicates = parsed = needs_review = failed = 0
            errors: list[str] = []

            try:
                messages = await self.client.fetch_messages(
                    self._since(),
                    sender_domains=self.settings.sender_domains,
                )
                fetched = len(messages)
                for message in messages:
                    try:
                        email_id, created = await self._store_message(message)
                    except Exception as exc:
                        failed += 1
                        errors.append(f"{message.internet_message_id}: {exc}")
                        continue

                    received_at = _normalize_provider_timestamp(message.received_at)
                    if not watermark or received_at > watermark:
                        watermark = received_at

                    if not created:
                        duplicates += 1
                        continue
                    stored += 1
                    try:
                        review_error = await self._parse_message(email_id, message)
                    except Exception as exc:
                        failed += 1
                        error = f"{type(exc).__name__}: {exc}"
                        errors.append(f"{message.internet_message_id}: {error}")
                        self.store.finish_email_parse(
                            email_id,
                            status="NEEDS_REVIEW",
                            body_text=parse_body(message.body_content_type, message.body_content),
                            body_sha256=_sha256_text(message.body_content),
                            error=error,
                        )
                    else:
                        if review_error:
                            needs_review += 1
                        else:
                            parsed += 1

                self.store.record_sync_success(watermark)
                return SyncResult(
                    fetched=fetched,
                    stored=stored,
                    duplicates=duplicates,
                    parsed=parsed,
                    needs_review=needs_review,
                    failed=failed,
                    watermark=watermark,
                    errors=errors,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.store.record_sync_failure(error)
                raise

    async def _store_message(self, message: MailMessage) -> tuple[int, bool]:
        body_text = parse_body(message.body_content_type, message.body_content)
        return self.store.upsert_fetched_email(
            mailbox=self.settings.m365_mailbox,
            internet_message_id=message.internet_message_id,
            graph_id=message.remote_id,
            subject=message.subject,
            sender_address=message.sender_address,
            sender_name=message.sender_name,
            received_at=_normalize_provider_timestamp(message.received_at),
            folder=self.settings.m365_folder,
            has_attachments=message.has_attachments,
            body_text=body_text,
            body_sha256=_sha256_text(message.body_content),
        )

    async def _parse_message(self, email_id: int, message: MailMessage) -> str | None:
        body_text = parse_body(message.body_content_type, message.body_content)
        max_bytes = self.settings.max_attachment_mb * 1024 * 1024
        provider_attachments = await self.client.fetch_attachments(
            message,
            max_size_bytes=max_bytes,
        )
        records: list[dict[str, object]] = []
        errors: list[str] = []

        for attachment in provider_attachments:
            record = self._parse_attachment(email_id, attachment)
            records.append(record)
            if record["parse_status"] != PARSE_OK:
                errors.append(f"{record['filename']}: {record['parse_error']}")

        self.store.replace_attachments(email_id, records)
        error = "; ".join(errors) if errors else None
        status = "NEEDS_REVIEW" if error else "PARSED"
        self.store.finish_email_parse(
            email_id,
            status=status,
            body_text=body_text,
            body_sha256=_sha256_text(message.body_content),
            error=error,
        )
        return error

    def _parse_attachment(self, email_id: int, attachment: MailAttachment) -> dict[str, object]:
        record: dict[str, object] = {
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "size_bytes": attachment.size_bytes,
            "sha256": attachment.sha256,
            "storage_path": None,
            "extracted_text": None,
            "parse_status": PARSE_SKIPPED,
            "parse_error": attachment.error,
        }
        if attachment.error:
            return record
        if attachment.content is None:
            record["parse_status"] = PARSE_FAILED
            record["parse_error"] = "attachment content is empty"
            return record

        storage_path = self._write_attachment(email_id, attachment)
        result = parse_attachment(
            attachment.filename,
            attachment.content_type,
            attachment.content,
        )
        record.update(
            {
                "storage_path": str(storage_path),
                "extracted_text": result.text,
                "parse_status": result.status,
                "parse_error": result.error,
            }
        )
        return record

    def _write_attachment(self, email_id: int, attachment: MailAttachment) -> Path:
        assert attachment.content is not None
        assert attachment.sha256 is not None
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(attachment.filename).name)
        safe_name = safe_name[:120] or "attachment"
        path = (
            Path(self.settings.data_dir)
            / "attachments"
            / str(email_id)
            / f"{attachment.sha256[:16]}_{safe_name}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(attachment.content)
        return path

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except SyncAlreadyRunning:
                pass
            except Exception:
                logger.exception("scheduled mail fetch failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.mail_poll_interval,
                )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop_event.set()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_provider_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
