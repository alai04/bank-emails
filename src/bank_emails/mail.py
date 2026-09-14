"""Microsoft 365 mail access through python-o365."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from O365 import Account
from O365.utils.token import MemoryTokenBackend

from .config import Settings


class MailClientError(RuntimeError):
    """Raised when python-o365 cannot complete a mail operation."""


@dataclass(frozen=True)
class MailMessage:
    remote_id: str
    internet_message_id: str
    subject: str
    sender_address: str
    sender_name: str
    received_at: str
    has_attachments: bool
    body_content_type: str
    body_content: str
    source: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MailAttachment:
    attachment_id: str
    filename: str
    content_type: str
    size_bytes: int
    content: bytes | None
    sha256: str | None
    error: str | None = None


class O365MailClient:
    """Async facade over the synchronous python-o365 API."""

    def __init__(self, settings: Settings, *, account: Any | None = None) -> None:
        self.settings = settings
        self._account = account
        self._mailbox: Any | None = None
        self._folders: dict[str, Any] = {}

    def _get_account(self) -> Any:
        if self._account is not None:
            return self._account

        common = {
            "auth_flow_type": "credentials" if self.settings.auth_mode == "app" else "public",
            "tenant_id": self.settings.m365_tenant_id,
            "main_resource": (
                self.settings.m365_mailbox if self.settings.auth_mode == "app" else "me"
            ),
            "token_backend": MemoryTokenBackend(),
            "timeout": self.settings.llm_timeout,
            "request_retries": self.settings.max_retry,
            "timezone": "UTC",
        }
        if self.settings.auth_mode == "app":
            account = Account(
                (self.settings.m365_client_id, self.settings.m365_client_secret or ""),
                **common,
            )
            if not account.request_token(None):
                raise MailClientError("python-o365 application authentication failed")
        else:
            account = Account(
                self.settings.m365_client_id,
                **common,
            )
            self._seed_delegated_token(account)
        self._account = account
        return account

    def _seed_delegated_token(self, account: Any) -> None:
        scopes = account.protocol.get_scopes_for(["mailbox"])
        if "offline_access" not in scopes:
            scopes.append("offline_access")
        result = account.con.msal_client.acquire_token_by_refresh_token(
            self.settings.m365_refresh_token or "",
            scopes=scopes,
        )
        if not result or "access_token" not in result:
            description = result.get("error_description") if result else None
            error = result.get("error") if result else None
            raise MailClientError(
                f"python-o365 refresh-token authentication failed: {error or description or 'unknown error'}"
            )
        account.con.session = account.con.get_session()
        account.con.update_session_auth_header(access_token=result["access_token"])

    def _get_mailbox(self) -> Any:
        if self._mailbox is None:
            self._mailbox = self._get_account().mailbox()
        return self._mailbox

    def _get_folder(self, path: str | None = None) -> Any:
        normalized = (path or self.settings.m365_folder).strip() or "Inbox"
        if normalized in self._folders:
            return self._folders[normalized]

        folder = self._get_mailbox()
        for segment in (part.strip() for part in normalized.split("/") if part.strip()):
            if segment.casefold() == "inbox":
                folder = self._get_mailbox().inbox_folder()
            else:
                folder = folder.get_folder(folder_name=segment)
            if folder is None:
                raise MailClientError(f"mail folder not found: {normalized}")
        self._folders[normalized] = folder
        return folder

    async def fetch_messages(
        self,
        since: datetime,
        *,
        folder: str | None = None,
        sender_domains: tuple[str, ...] = (),
    ) -> list[MailMessage]:
        return await asyncio.to_thread(
            self._fetch_messages_sync,
            since,
            folder,
            tuple(sender_domains),
        )

    def _fetch_messages_sync(
        self,
        since: datetime,
        folder: str | None,
        sender_domains: tuple[str, ...],
    ) -> list[MailMessage]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        since = since.astimezone(timezone.utc)
        mailbox = self._get_mailbox()
        target = self._get_folder(folder)
        query = mailbox.q().greater_equal("receivedDateTime", since)
        messages = target.get_messages(
            limit=None,
            query=query,
            order_by="receivedDateTime asc",
        )
        domains = tuple(domain.casefold().lstrip("@") for domain in sender_domains)
        result: list[MailMessage] = []
        for message in messages:
            sender = message.sender
            address = str(getattr(sender, "address", "") or "")
            if domains and not self._sender_allowed(address, domains):
                continue
            received = message.received
            result.append(
                MailMessage(
                    remote_id=str(message.object_id),
                    internet_message_id=str(message.internet_message_id or message.object_id),
                    subject=str(message.subject or ""),
                    sender_address=address,
                    sender_name=str(getattr(sender, "name", "") or ""),
                    received_at=received.astimezone(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    has_attachments=bool(message.has_attachments),
                    body_content_type=str(message.body_type or "text"),
                    body_content=str(message.body or ""),
                    source=message,
                )
            )
        return result

    @staticmethod
    def _sender_allowed(address: str, domains: tuple[str, ...]) -> bool:
        domain = address.rsplit("@", 1)[-1].casefold()
        return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in domains)

    async def fetch_attachments(
        self,
        message: MailMessage,
        *,
        max_size_bytes: int | None = None,
    ) -> list[MailAttachment]:
        return await asyncio.to_thread(self._fetch_attachments_sync, message, max_size_bytes)

    def _fetch_attachments_sync(
        self,
        message: MailMessage,
        max_size_bytes: int | None,
    ) -> list[MailAttachment]:
        source = message.source
        if not message.has_attachments:
            return []
        if source is None:
            source = self._get_folder().get_message(object_id=message.remote_id)
        if source is None:
            raise MailClientError(f"message not found in python-o365: {message.remote_id}")
        if not source.attachments.download_attachments():
            return []

        result: list[MailAttachment] = []
        for attachment in source.attachments:
            size_bytes = int(attachment.size or 0)
            content_type = mimetypes.guess_type(attachment.name or "")[0] or (
                "application/octet-stream"
            )
            base = {
                "attachment_id": str(attachment.attachment_id or attachment.name or "attachment"),
                "filename": str(attachment.name or "attachment"),
                "content_type": content_type,
                "size_bytes": size_bytes,
            }
            if attachment.attachment_type != "file":
                result.append(
                    MailAttachment(
                        **base,
                        content=None,
                        sha256=None,
                        error="unsupported attachment type",
                    )
                )
                continue
            if max_size_bytes is not None and size_bytes > max_size_bytes:
                result.append(
                    MailAttachment(
                        **base,
                        content=None,
                        sha256=None,
                        error=f"attachment exceeds limit of {max_size_bytes} bytes",
                    )
                )
                continue
            if not attachment.content:
                result.append(
                    MailAttachment(
                        **base,
                        content=None,
                        sha256=None,
                        error="attachment content is empty",
                    )
                )
                continue
            try:
                content = base64.b64decode(attachment.content, validate=True)
            except (ValueError, TypeError) as exc:
                result.append(
                    MailAttachment(
                        **base,
                        content=None,
                        sha256=None,
                        error=f"invalid attachment content: {exc}",
                    )
                )
                continue
            result.append(
                MailAttachment(
                    **base,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        return result
