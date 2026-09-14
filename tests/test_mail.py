"""python-o365 client integration tests with mocked O365 objects."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bank_emails.mail as mail_module
from bank_emails.config import Settings
from bank_emails.mail import MailClientError, O365MailClient


class FakeAttachments:
    def __init__(self, attachments: list[SimpleNamespace]) -> None:
        self.items = attachments
        self.download_calls = 0

    def download_attachments(self) -> bool:
        self.download_calls += 1
        return True

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.items)


class FakeFolder:
    def __init__(self, name: str, messages: list[SimpleNamespace] | None = None) -> None:
        self.name = name
        self.messages = messages or []
        self.children: dict[str, FakeFolder] = {}
        self.message_calls: list[dict] = []

    def get_folder(self, *, folder_name: str) -> FakeFolder | None:
        return self.children.get(folder_name)

    def get_messages(self, **kwargs):  # type: ignore[no-untyped-def]
        self.message_calls.append(kwargs)
        return iter(self.messages)

    def get_message(self, *, object_id: str):  # type: ignore[no-untyped-def]
        for message in self.messages:
            if message.object_id == object_id:
                return message
        return None


class FakeQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def greater_equal(self, attribute: str, value: object) -> object:
        self.calls.append((attribute, value))
        return object()


class FakeMailbox(FakeFolder):
    def __init__(self, messages: list[SimpleNamespace] | None = None) -> None:
        super().__init__("MailBox", messages)
        self.query = FakeQuery()
        self.inbox = FakeFolder("Inbox", messages)

    def q(self) -> FakeQuery:
        return self.query

    def inbox_folder(self) -> FakeFolder:
        return self.inbox


class FakeAccount:
    def __init__(self, mailbox: FakeMailbox) -> None:
        self.fake_mailbox = mailbox

    def mailbox(self) -> FakeMailbox:
        return self.fake_mailbox


def _o365_message(
    *,
    remote_id: str = "message-1",
    address: str = "confirm@maybank.com",
    has_attachments: bool = True,
) -> tuple[SimpleNamespace, FakeAttachments]:
    attachments = FakeAttachments(
        [
            SimpleNamespace(
                attachment_id="attachment-1",
                name="trades.csv",
                attachment_type="file",
                size=40,
                content=base64.b64encode(b"symbol,quantity\n600887.SH,500000\n").decode("ascii"),
            )
        ]
    )
    message = SimpleNamespace(
        object_id=remote_id,
        internet_message_id=f"<{remote_id}@example.com>",
        subject="Trade Confirmation",
        sender=SimpleNamespace(address=address, name="Maybank"),
        received=datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc),
        has_attachments=has_attachments,
        body_type="html",
        body="<table><tr><td>Symbol</td><td>600887.SH</td></tr></table>",
        attachments=attachments,
    )
    return message, attachments


def test_fetch_messages_uses_o365_folder_query_and_pagination(
    settings: Settings,
) -> None:
    allowed, _ = _o365_message()
    denied, _ = _o365_message(remote_id="noise", address="news@unrelated.com")
    mailbox = FakeMailbox([allowed, denied])
    client = O365MailClient(settings, account=FakeAccount(mailbox))

    messages = asyncio.run(
        client.fetch_messages(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            sender_domains=("maybank.com",),
        )
    )

    assert [message.remote_id for message in messages] == ["message-1"]
    assert messages[0].internet_message_id == "<message-1@example.com>"
    assert messages[0].source is allowed
    assert mailbox.inbox.message_calls[0]["limit"] is None
    assert mailbox.inbox.message_calls[0]["order_by"] == "receivedDateTime asc"
    assert mailbox.query.calls == [
        ("receivedDateTime", datetime(2026, 9, 1, tzinfo=timezone.utc))
    ]


def test_nested_folder_is_resolved_through_o365(settings: Settings) -> None:
    settings = settings.model_copy(update={"m365_folder": "Inbox/Trades/HK"})
    mailbox = FakeMailbox()
    trades = FakeFolder("Trades")
    hk = FakeFolder("HK")
    trades.children["HK"] = hk
    mailbox.inbox.children["Trades"] = trades
    client = O365MailClient(settings, account=FakeAccount(mailbox))

    folder = client._get_folder(None)

    assert folder is hk


def test_attachments_are_downloaded_by_python_o365(settings: Settings) -> None:
    message, attachment_collection = _o365_message()
    mailbox = FakeMailbox([message])
    client = O365MailClient(settings, account=FakeAccount(mailbox))
    mail_message = asyncio.run(client.fetch_messages(datetime.now(timezone.utc)))[0]

    attachments = asyncio.run(
        client.fetch_attachments(mail_message, max_size_bytes=100)
    )

    assert attachment_collection.download_calls == 1
    assert attachments[0].filename == "trades.csv"
    assert attachments[0].content == b"symbol,quantity\n600887.SH,500000\n"
    assert attachments[0].sha256


def test_oversized_and_item_attachments_are_marked_without_content(
    settings: Settings,
) -> None:
    message, _ = _o365_message()
    message.attachments.items = [
        SimpleNamespace(
            attachment_id="large",
            name="large.pdf",
            attachment_type="file",
            size=1000,
            content=base64.b64encode(b"pdf").decode("ascii"),
        ),
        SimpleNamespace(
            attachment_id="item",
            name="forwarded.eml",
            attachment_type="item",
            size=10,
            content=None,
        ),
    ]
    client = O365MailClient(settings, account=FakeAccount(FakeMailbox([message])))
    mail_message = asyncio.run(client.fetch_messages(datetime.now(timezone.utc)))[0]

    attachments = asyncio.run(
        client.fetch_attachments(mail_message, max_size_bytes=100)
    )

    assert attachments[0].content is None
    assert "exceeds limit" in (attachments[0].error or "")
    assert attachments[1].content is None
    assert attachments[1].error == "unsupported attachment type"


def test_application_auth_is_delegated_to_o365_account(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    mailbox = FakeMailbox()

    class SpyAccount:
        def __init__(self, credentials, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured["credentials"] = credentials
            captured["kwargs"] = kwargs

        def request_token(self, authorization_url):  # type: ignore[no-untyped-def]
            captured["authorization_url"] = authorization_url
            return True

        def mailbox(self) -> FakeMailbox:
            return mailbox

    monkeypatch.setattr(mail_module, "Account", SpyAccount)
    client = O365MailClient(settings)

    client._get_mailbox()

    assert captured["credentials"] == ("client-id", "client-secret")
    assert captured["authorization_url"] is None
    assert captured["kwargs"]["auth_flow_type"] == "credentials"
    assert captured["kwargs"]["tenant_id"] == "tenant-id"
    assert captured["kwargs"]["main_resource"] == "ops@example.com"


def test_delegated_auth_seeds_o365_session_from_refresh_token(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("M365_CLIENT_SECRET")
    monkeypatch.setenv("M365_REFRESH_TOKEN", "refresh-token")
    settings = Settings(_env_file=None)
    captured: dict = {}

    class FakeConnection:
        class FakeMsal:
            @staticmethod
            def acquire_token_by_refresh_token(refresh_token: str, *, scopes: list[str]):  # type: ignore[no-untyped-def]
                captured["refresh_token"] = refresh_token
                captured["scopes"] = scopes
                return {"access_token": "access-token"}

        msal_client = FakeMsal()

        @staticmethod
        def get_session():  # type: ignore[no-untyped-def]
            return SimpleNamespace(headers={})

        @staticmethod
        def update_session_auth_header(*, access_token: str) -> None:
            captured["access_token"] = access_token

    class FakeProtocol:
        @staticmethod
        def get_scopes_for(scopes: list[str]) -> list[str]:
            captured["requested_scopes"] = scopes
            return ["https://graph.microsoft.com/Mail.Read", "offline_access"]

    class SpyAccount:
        def __init__(self, credentials, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured["credentials"] = credentials
            captured["kwargs"] = kwargs
            self.protocol = FakeProtocol()
            self.con = FakeConnection()

    monkeypatch.setattr(mail_module, "Account", SpyAccount)
    client = O365MailClient(settings)

    account = client._get_account()

    assert captured["credentials"] == "client-id"
    assert captured["kwargs"]["auth_flow_type"] == "public"
    assert captured["refresh_token"] == "refresh-token"
    assert captured["requested_scopes"] == ["mailbox"]
    assert "offline_access" in captured["scopes"]
    assert captured["access_token"] == "access-token"
    assert account.con.session is not None


def test_refresh_failure_is_wrapped(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("M365_CLIENT_SECRET")
    monkeypatch.setenv("M365_REFRESH_TOKEN", "invalid")
    settings = Settings(_env_file=None)

    class FakeConnection:
        class FakeMsal:
            @staticmethod
            def acquire_token_by_refresh_token(refresh_token: str, *, scopes: list[str]):  # type: ignore[no-untyped-def]
                return {"error": "invalid_grant", "error_description": "expired"}

        msal_client = FakeMsal()

    class FakeProtocol:
        @staticmethod
        def get_scopes_for(scopes: list[str]) -> list[str]:
            return ["Mail.Read"]

    class SpyAccount:
        def __init__(self, credentials, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self.protocol = FakeProtocol()
            self.con = FakeConnection()

    monkeypatch.setattr(mail_module, "Account", SpyAccount)

    with pytest.raises(MailClientError, match="invalid_grant"):
        O365MailClient(settings)._get_account()
