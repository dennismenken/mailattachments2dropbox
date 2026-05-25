"""Shared test fixtures.

Provides a baseline Settings and Mapping that test cases can tweak by passing
kwargs to ``make_settings`` / by mutating the returned Mapping.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import pytest

from mailattachments2dropbox.config import (
    BranchConfig,
    Mapping,
    RejectAction,
    Settings,
    SuccessAction,
)


def make_settings(**overrides: Any) -> Settings:
    """Build a Settings instance bypassing environment variables.

    Pydantic accepts kwargs directly when we are explicit about every
    required field.
    """
    base: dict[str, Any] = {
        "imap_host": "imap.test",
        "imap_user": "test@example.com",
        "imap_password": "pw",
        "dropbox_app_key": "k",
        "dropbox_app_secret": "s",
        "dropbox_refresh_token": "r",
        "allowed_senders": [],
        "allowed_extensions": ["pdf"],
        "default_branch": "in",
        "default_subfolder_key": "auto",
        "mail_on_success": SuccessAction.DELETE,
        "mail_on_reject": RejectAction.DELETE,
    }
    base.update(overrides)
    return Settings.model_validate(base)


def make_mapping() -> Mapping:
    return Mapping(
        branches={
            "in": BranchConfig(
                folder="Inbox",
                default_subfolder="auto",
                subfolders={
                    "auto": "Auto-Assignment",
                    "kasse": "Cash",
                    "pp": "PayPal",
                    "paypal": "PayPal",
                },
            ),
            "out": BranchConfig(
                folder="Outbox",
                default_subfolder="",
                subfolders={},
            ),
        }
    )


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def mapping() -> Mapping:
    return make_mapping()


def build_email(
    *,
    sender: str = "user@example.com",
    subject: str = "Test",
    body: str = "Hello",
    attachments: list[tuple[str, bytes, str]] | None = None,
    inner_messages: list[EmailMessage] | None = None,
) -> bytes:
    """Construct an RFC 5322 mail in bytes form.

    ``attachments`` is a list of (filename, bytes, mime) tuples.
    ``inner_messages`` is a list of fully-built ``EmailMessage`` objects that
    are attached as ``message/rfc822`` parts (the Gmail forward-as-attachment
    case).
    """
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "buchhaltung@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<test-id@example.com>"
    msg.set_content(body)

    for filename, payload, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)

    for inner in inner_messages or []:
        msg.add_attachment(inner, filename=inner.get_filename() or "forwarded.eml")

    return msg.as_bytes()
