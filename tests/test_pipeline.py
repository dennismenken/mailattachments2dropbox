from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import pytest

from mailattachments2dropbox.dropbox_client import UploadResult
from mailattachments2dropbox.mail_client import InboundMail
from mailattachments2dropbox.pipeline import MailPipeline

from .conftest import build_email, make_settings


class FakeDropbox:
    """In-memory stand-in for DropboxClient used by pipeline tests."""

    def __init__(self) -> None:
        self.folders: list[str] = []
        self.uploads: list[tuple[str, str, bytes]] = []

    def ensure_folder(self, subpath: str) -> None:
        self.folders.append(subpath)

    def upload(self, subpath: str, filename: str, content: bytes) -> UploadResult:
        self.uploads.append((subpath, filename, content))
        path = f"/root/{subpath}/{filename}"
        return UploadResult(requested_path=path, final_path=path, renamed=False, size=len(content))


class LifecycleRecorder:
    def __init__(self) -> None:
        self.success_calls: list[int] = []
        self.reject_calls: list[int] = []

    async def on_success(self, uid: int) -> None:
        self.success_calls.append(uid)

    async def on_reject(self, uid: int) -> None:
        self.reject_calls.append(uid)


def _make_pipeline(
    mapping: Any, **settings_overrides: Any
) -> tuple[MailPipeline, FakeDropbox, LifecycleRecorder]:
    settings = make_settings(**settings_overrides)
    dropbox = FakeDropbox()
    rec = LifecycleRecorder()
    pipeline = MailPipeline(
        settings=settings,
        mapping=mapping,
        dropbox=dropbox,  # type: ignore[arg-type]
        on_success=rec.on_success,
        on_reject=rec.on_reject,
    )
    return pipeline, dropbox, rec


@pytest.mark.asyncio
async def test_pipeline_uploads_attachment_and_triggers_success(mapping):
    pipeline, dropbox, rec = _make_pipeline(mapping)
    raw = build_email(
        sender="alice@example.com",
        body=":::in:::paypal",
        attachments=[("invoice.pdf", b"PDF", "application/pdf")],
    )
    result = await pipeline.process(InboundMail(uid=11, raw=raw))
    assert result.error is None
    assert result.uploaded == 1
    assert result.skipped == 0
    assert dropbox.uploads == [("Inbox/PayPal", "invoice.pdf", b"PDF")]
    assert rec.success_calls == [11]
    assert rec.reject_calls == []


@pytest.mark.asyncio
async def test_pipeline_rejects_unknown_sender(mapping):
    pipeline, dropbox, rec = _make_pipeline(mapping, allowed_senders=["*@example.com"])
    raw = build_email(
        sender="evil@attacker.com",
        body=":::in:::paypal",
        attachments=[("a.pdf", b"X", "application/pdf")],
    )
    result = await pipeline.process(InboundMail(uid=7, raw=raw))
    assert result.error == "sender_rejected"
    assert dropbox.uploads == []
    assert rec.reject_calls == [7]
    assert rec.success_calls == []


@pytest.mark.asyncio
async def test_pipeline_leaves_mail_on_routing_error(mapping):
    pipeline, dropbox, rec = _make_pipeline(mapping)
    raw = build_email(
        body=":::in:::unknown_key",  # subfolder not in mapping
        attachments=[("a.pdf", b"X", "application/pdf")],
    )
    result = await pipeline.process(InboundMail(uid=22, raw=raw))
    assert "unknown_key" in (result.error or "")
    assert dropbox.uploads == []
    assert rec.success_calls == []
    assert rec.reject_calls == []  # not a sender rejection; nothing happens to the mail


@pytest.mark.asyncio
async def test_pipeline_filters_non_pdf_attachments_by_default(mapping):
    pipeline, dropbox, _rec = _make_pipeline(mapping)
    raw = build_email(
        body=":::in:::paypal",
        attachments=[
            ("invoice.pdf", b"PDF", "application/pdf"),
            ("signature.png", b"PNG", "image/png"),
        ],
    )
    result = await pipeline.process(InboundMail(uid=42, raw=raw))
    assert result.uploaded == 1
    assert result.skipped == 1
    assert dropbox.uploads == [("Inbox/PayPal", "invoice.pdf", b"PDF")]


@pytest.mark.asyncio
async def test_pipeline_default_routing_when_no_sentinel(mapping):
    pipeline, dropbox, _rec = _make_pipeline(mapping)
    raw = build_email(body="nothing here", attachments=[("a.pdf", b"X", "application/pdf")])
    result = await pipeline.process(InboundMail(uid=1, raw=raw))
    assert result.uploaded == 1
    assert dropbox.uploads[0][0] == "Inbox/Auto-Assignment"


@pytest.mark.asyncio
async def test_pipeline_uploads_attachments_from_nested_forward(mapping):
    inner = EmailMessage()
    inner["From"] = "supplier@x.de"
    inner["Subject"] = "Invoice"
    inner.set_content("body")
    inner.add_attachment(b"INNERPDF", maintype="application", subtype="pdf", filename="inner.pdf")
    raw = build_email(body=":::in:::kasse", inner_messages=[inner])
    # kasse maps to "Cash" in the test mapping.
    pipeline, dropbox, _rec = _make_pipeline(mapping)
    result = await pipeline.process(InboundMail(uid=9, raw=raw))
    assert result.uploaded == 1
    assert dropbox.uploads == [("Inbox/Cash", "inner.pdf", b"INNERPDF")]
