from __future__ import annotations

from email.message import EmailMessage

from mailattachments2dropbox.mail_parser import parse_mail

from .conftest import build_email


def test_simple_mail_with_pdf():
    raw = build_email(
        sender="alice@example.com",
        subject="Invoice",
        body=":::in:::paypal",
        attachments=[("invoice.pdf", b"PDFDATA", "application/pdf")],
    )
    parsed = parse_mail(raw)
    assert parsed.sender == "alice@example.com"
    assert parsed.subject == "Invoice"
    assert parsed.body_text.startswith(":::in:::paypal")
    assert len(parsed.attachments) == 1
    a = parsed.attachments[0]
    assert a.filename == "invoice.pdf"
    assert a.content == b"PDFDATA"
    assert a.mime == "application/pdf"
    assert a.source_path == ("outer",)


def test_nested_forwarded_mail():
    inner = EmailMessage()
    inner["From"] = "supplier@partner.de"
    inner["Subject"] = "Original invoice"
    inner.set_content("original mail body")
    inner.add_attachment(
        b"INVOICEBYTES", maintype="application", subtype="pdf", filename="invoice.pdf"
    )

    raw = build_email(
        sender="forwarder@example.com",
        subject="Fwd: Original invoice",
        body=":::in:::paypal\nQuoted text follows",
        inner_messages=[inner],
    )
    parsed = parse_mail(raw)
    # We expect the inner attachment to be surfaced.
    pdf_attachments = [a for a in parsed.attachments if a.filename == "invoice.pdf"]
    assert len(pdf_attachments) == 1
    assert pdf_attachments[0].source_path[0] == "outer"
    assert pdf_attachments[0].content == b"INVOICEBYTES"
    # Body of the OUTER mail must still carry the sentinel.
    assert ":::in:::paypal" in parsed.body_text


def test_multiple_inner_messages_with_multiple_attachments():
    inner_messages = []
    for i in range(3):
        inner = EmailMessage()
        inner["From"] = f"supplier{i}@partner.de"
        inner["Subject"] = f"Original invoice {i}"
        inner.set_content("body")
        inner.add_attachment(
            f"data-{i}".encode(),
            maintype="application",
            subtype="pdf",
            filename=f"invoice-{i}.pdf",
        )
        inner_messages.append(inner)

    raw = build_email(
        body=":::in:::paypal",
        inner_messages=inner_messages,
    )
    parsed = parse_mail(raw)
    names = sorted(a.filename for a in parsed.attachments)
    assert names == ["invoice-0.pdf", "invoice-1.pdf", "invoice-2.pdf"]


def test_html_only_body_is_stripped_for_sentinel():
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "x@y"
    msg["Subject"] = "html only"
    msg.set_content("<p>:::in:::kasse</p><p>kontext</p>", subtype="html")
    parsed = parse_mail(msg.as_bytes())
    assert ":::in:::kasse" in parsed.body_text


def test_mail_with_no_attachments():
    raw = build_email(body=":::in")
    parsed = parse_mail(raw)
    assert parsed.attachments == []
