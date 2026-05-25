"""Parse raw RFC 5322 email bytes into a structured representation.

The walker descends into ``message/rfc822`` parts so that attachments inside
forwarded mails (Gmail-style ``Forward as attachment``) are surfaced flat in
the result. ``source_path`` records the ancestor trail for audit logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import cast


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    mime: str
    source_path: tuple[
        str, ...
    ]  # ancestor mail labels, e.g. ("outer",) or ("outer", "forwarded.eml")

    @property
    def extension(self) -> str:
        if "." not in self.filename:
            return ""
        return self.filename.rsplit(".", 1)[1].lower()


@dataclass(frozen=True)
class ParsedMail:
    sender: str
    subject: str
    message_id: str
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    """Strip HTML tags to plain text. Good enough to find a body sentinel."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip > 0:
            self._skip -= 1
        if tag in ("p", "br", "div", "li", "tr"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _is_attachment(part: Message) -> bool:
    if part.is_multipart():
        return False
    disp = part.get_content_disposition()
    if disp == "attachment":
        return True
    # Some clients (older Outlook, scanners) attach invoices with disposition=inline
    # but still set a filename. Treat those as attachments anyway.
    return bool(part.get_filename())


def _decode_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        return b""
    if isinstance(payload, bytes):
        return payload
    # get_payload(decode=True) can return Any when the underlying type system
    # has not narrowed the part; coerce defensively for our usage.
    return cast("bytes", payload)


def _walk(
    part: Message,
    trail: tuple[str, ...],
    attachments: list[Attachment],
) -> None:
    ctype = part.get_content_type()

    if ctype == "message/rfc822":
        payload = part.get_payload()
        inner: Message | None
        if isinstance(payload, list):
            first = payload[0] if payload else None
            inner = first if isinstance(first, Message) else None
        elif isinstance(payload, Message):
            inner = payload
        else:
            inner = None
        if inner is None:
            return
        label = part.get_filename() or f"message_{len(trail)}.eml"
        _walk(inner, (*trail, label), attachments)
        return

    if part.is_multipart():
        if isinstance(part, EmailMessage):
            for child_part in part.iter_parts():
                _walk(child_part, trail, attachments)
        else:
            multi_payload = part.get_payload()
            if isinstance(multi_payload, list):
                for entry in multi_payload:
                    if isinstance(entry, Message):
                        _walk(entry, trail, attachments)
        return

    if not _is_attachment(part):
        return

    filename = part.get_filename() or "unnamed"
    content = _decode_bytes(part)
    attachments.append(
        Attachment(
            filename=filename,
            content=content,
            mime=ctype,
            source_path=trail,
        )
    )


def _extract_body_text(msg: EmailMessage) -> str:
    """Return the best plain-text representation of the outer mail body."""
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    ctype = body.get_content_type()
    if ctype == "text/plain":
        try:
            content = body.get_content()
        except Exception:
            payload = _decode_bytes(body)
            return payload.decode(body.get_content_charset() or "utf-8", errors="replace")
        return cast("str", content)
    if ctype == "text/html":
        try:
            html_raw = body.get_content()
            html = cast("str", html_raw)
        except Exception:
            html = _decode_bytes(body).decode(
                body.get_content_charset() or "utf-8", errors="replace"
            )
        return _html_to_text(html)
    return ""


def parse_mail(raw: bytes) -> ParsedMail:
    """Parse raw email bytes into a ``ParsedMail`` with flattened attachments."""
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)

    sender = (parseaddr(msg.get("From", ""))[1] or "").lower()
    subject = msg.get("Subject", "") or ""
    message_id = msg.get("Message-ID", "") or ""
    body_text = _extract_body_text(msg)

    attachments: list[Attachment] = []
    if msg.is_multipart():
        for child in msg.iter_parts():
            _walk(child, ("outer",), attachments)
    elif _is_attachment(msg):
        # Edge case: single-part mail whose body itself is an attachment.
        attachments.append(
            Attachment(
                filename=msg.get_filename() or "unnamed",
                content=_decode_bytes(msg),
                mime=msg.get_content_type(),
                source_path=("outer",),
            )
        )

    return ParsedMail(
        sender=sender,
        subject=subject,
        message_id=message_id,
        body_text=body_text,
        attachments=attachments,
    )
