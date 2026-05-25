"""Entrypoint.

Loads config and mapping, wires the IMAP client, Dropbox client and pipeline,
and runs the mail-fetch loop until SIGTERM/SIGINT.
"""

from __future__ import annotations

import asyncio
import signal
import sys

import structlog

from .config import Settings, load_mapping, validate_defaults
from .dropbox_client import DropboxClient
from .logging_setup import configure_logging
from .mail_client import ImapMailClient, make_lifecycle_actions
from .pipeline import MailPipeline


async def _run() -> int:
    settings = Settings()
    log = configure_logging(settings)

    try:
        mapping = load_mapping(settings.mapping_file)
        validate_defaults(settings, mapping)
    except Exception as exc:
        log.exception("STARTUP_FAILED", phase="config", error=str(exc))
        return 2

    dropbox = DropboxClient(settings)
    try:
        account_name = dropbox.check()
    except Exception as exc:
        log.exception("STARTUP_FAILED", phase="dropbox_auth", error=str(exc))
        return 2
    log.info("DROPBOX_READY", account=account_name)

    imap = ImapMailClient(settings)
    try:
        await imap.connect()
    except Exception as exc:
        log.exception("STARTUP_FAILED", phase="imap_connect", error=str(exc))
        return 2

    on_success, on_reject = make_lifecycle_actions(imap, settings)
    pipeline = MailPipeline(
        settings=settings,
        mapping=mapping,
        dropbox=dropbox,
        on_success=on_success,
        on_reject=on_reject,
    )

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        log.info("SHUTDOWN_REQUESTED")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Non-asyncio-friendly platform; fall back to the classic handler.
            signal.signal(sig, lambda *_: _request_stop())

    log.info("WORKER_STARTED")
    stream_task = asyncio.create_task(_consume_stream(imap, pipeline, log))
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        await asyncio.wait({stream_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stream_task.cancel()
        try:
            await stream_task
        except (asyncio.CancelledError, Exception):
            pass
        await imap.close()
        log.info("WORKER_STOPPED")
    return 0


async def _consume_stream(
    imap: ImapMailClient,
    pipeline: MailPipeline,
    log: structlog.stdlib.BoundLogger,
) -> None:
    async for batch in imap.stream():
        for mail in batch:
            try:
                await pipeline.process(mail)
            except Exception as exc:
                log.exception("PIPELINE_UNHANDLED_ERROR", uid=mail.uid, error=str(exc))


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
