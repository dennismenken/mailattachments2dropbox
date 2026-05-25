"""Per-mail processing pipeline.

The pipeline is the only place that talks to all the other modules. It is
intentionally linear and stateless: each ``InboundMail`` goes through the same
steps and produces a structured audit trail.

Flow per mail:

  1. parse raw bytes  -> ParsedMail
  2. sender allow-list check
  3. resolve sentinel -> RoutingDecision or RoutingError
  4. filter attachments by extension whitelist
  5. ensure Dropbox folder and upload each remaining attachment
  6. dispatch lifecycle action (delete / move / keep)

Step 2 failure deletes (or keeps) the mail per ``MAIL_ON_REJECT``.
Steps 3 and 5 failures leave the mail untouched so the operator can
inspect it or so the next run can retry transient errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from .mail_parser import parse_mail
from .sentinel import RoutingError, extension_allowed, route, sender_allowed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .config import Mapping, Settings
    from .dropbox_client import DropboxClient
    from .mail_client import InboundMail


@dataclass(frozen=True)
class PipelineResult:
    uid: int
    uploaded: int
    skipped: int
    error: str | None = None


class MailPipeline:
    def __init__(
        self,
        settings: Settings,
        mapping: Mapping,
        dropbox: DropboxClient,
        on_success: Callable[[int], Awaitable[None]],
        on_reject: Callable[[int], Awaitable[None]],
    ) -> None:
        self._settings = settings
        self._mapping = mapping
        self._dropbox = dropbox
        self._on_success = on_success
        self._on_reject = on_reject
        self._log = structlog.stdlib.get_logger("mailattachments2dropbox.pipeline")

    async def process(self, mail: InboundMail) -> PipelineResult:
        uid = mail.uid
        try:
            parsed = parse_mail(mail.raw)
        except Exception as exc:
            self._log.exception("MAIL_PARSE_FAILED", uid=uid, error=str(exc))
            return PipelineResult(uid=uid, uploaded=0, skipped=0, error=f"parse_failed: {exc}")

        self._log.info(
            "MAIL_RECEIVED",
            uid=uid,
            message_id=parsed.message_id,
            sender=parsed.sender,
            subject=parsed.subject,
            attachments_total=len(parsed.attachments),
        )

        allowed, rule = sender_allowed(parsed.sender, self._settings.allowed_senders)
        if not allowed:
            self._log.warning(
                "REJECT_SENDER",
                uid=uid,
                sender=parsed.sender,
                allow_list=self._settings.allowed_senders,
            )
            try:
                await self._on_reject(uid)
            except Exception as exc:
                self._log.exception("REJECT_ACTION_FAILED", uid=uid, error=str(exc))
            return PipelineResult(uid=uid, uploaded=0, skipped=0, error="sender_rejected")
        self._log.info(
            "SENDER_CHECK",
            uid=uid,
            sender=parsed.sender,
            matched_rule=rule,
        )

        decision = route(parsed.body_text, self._mapping, self._settings)
        if isinstance(decision, RoutingError):
            self._log.error(
                "ROUTING_REJECTED",
                uid=uid,
                reason=decision.reason,
                matched_token=decision.matched_token,
            )
            # Deliberately do NOT touch the mail: operator must intervene.
            return PipelineResult(uid=uid, uploaded=0, skipped=0, error=decision.reason)

        self._log.info(
            "SENTINEL_PARSED",
            uid=uid,
            branch=decision.branch_key,
            subfolder=decision.subfolder_key,
            matched_token=decision.matched_token,
        )
        self._log.info(
            "ROUTE_RESOLVED",
            uid=uid,
            target=f"{self._settings.dropbox_root_path}/{decision.target_subpath}",
        )

        # Make sure the destination folder exists before any upload.
        try:
            self._dropbox.ensure_folder(decision.target_subpath)
        except Exception as exc:
            self._log.exception(
                "DROPBOX_ENSURE_FOLDER_FAILED",
                uid=uid,
                target=decision.target_subpath,
                error=str(exc),
            )
            return PipelineResult(
                uid=uid, uploaded=0, skipped=0, error=f"dropbox_ensure_folder: {exc}"
            )

        uploaded = 0
        skipped = 0
        for idx, attachment in enumerate(parsed.attachments):
            self._log.info(
                "ATTACHMENT_FOUND",
                uid=uid,
                idx=idx,
                filename=attachment.filename,
                mime=attachment.mime,
                size=len(attachment.content),
                source_path=list(attachment.source_path),
            )
            if not extension_allowed(attachment.filename, self._settings.allowed_extensions):
                self._log.info(
                    "ATTACHMENT_SKIPPED",
                    uid=uid,
                    idx=idx,
                    filename=attachment.filename,
                    reason="extension_not_allowed",
                    allowed=self._settings.allowed_extensions,
                )
                skipped += 1
                continue
            if not attachment.content:
                self._log.info(
                    "ATTACHMENT_SKIPPED",
                    uid=uid,
                    idx=idx,
                    filename=attachment.filename,
                    reason="empty_payload",
                )
                skipped += 1
                continue
            try:
                upload = self._dropbox.upload(
                    decision.target_subpath, attachment.filename, attachment.content
                )
            except Exception as exc:
                self._log.exception(
                    "DROPBOX_UPLOAD_FAILED",
                    uid=uid,
                    idx=idx,
                    filename=attachment.filename,
                    error=str(exc),
                )
                # Bail out for the whole mail: do NOT trigger lifecycle so the
                # next run retries. Mark which attachments did succeed.
                return PipelineResult(
                    uid=uid,
                    uploaded=uploaded,
                    skipped=skipped,
                    error=f"dropbox_upload: {exc}",
                )
            self._log.info(
                "DROPBOX_UPLOAD",
                uid=uid,
                idx=idx,
                filename=attachment.filename,
                requested_path=upload.requested_path,
                final_path=upload.final_path,
                renamed=upload.renamed,
                size=upload.size,
            )
            uploaded += 1

        try:
            await self._on_success(uid)
        except Exception as exc:
            self._log.exception("LIFECYCLE_ACTION_FAILED", uid=uid, error=str(exc))
            return PipelineResult(
                uid=uid, uploaded=uploaded, skipped=skipped, error=f"lifecycle: {exc}"
            )

        self._log.info(
            "MAIL_LIFECYCLE",
            uid=uid,
            action=self._settings.mail_on_success.value,
            uploaded=uploaded,
            skipped=skipped,
        )
        return PipelineResult(uid=uid, uploaded=uploaded, skipped=skipped)
