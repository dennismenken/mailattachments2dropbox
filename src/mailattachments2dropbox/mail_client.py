"""Async IMAP client.

Uses ``aioimaplib`` so we can sit on an IMAP IDLE channel and react to new
mail within seconds without hammering the server. If the IDLE socket drops or
the server refuses IDLE, we fall back to a polling loop at
``IMAP_POLL_INTERVAL_SECONDS``.

This module owns the connection, the message-fetch logic and the per-mail
mutations (delete / move / mark seen). The pipeline only sees a stream of
``InboundMail`` instances and a small set of action callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from aioimaplib import IMAP4, IMAP4_SSL, Abort, AioImapException

from .config import Settings

logger = logging.getLogger("mailattachments2dropbox.mail_client")

# Default aioimaplib op timeout. Increase a bit, large mails (>10 MB) on slow
# connections need more than the default.
_OP_TIMEOUT = 60

# Window IDLE waits before refreshing the connection. RFC 2177 recommends
# breaking IDLE every 29 minutes to keep middleboxes from dropping the socket.
_IDLE_REFRESH_SECONDS = 25 * 60


@dataclass(frozen=True)
class InboundMail:
    uid: int
    raw: bytes


_RFC822_RE = re.compile(rb"BODY\[\]\s*\{(\d+)\}", re.IGNORECASE)


class ImapMailClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._imap: IMAP4 | None = None
        self._shutdown = asyncio.Event()

    # --- Connection lifecycle ------------------------------------------------

    async def connect(self) -> None:
        if self._settings.imap_use_tls:
            self._imap = IMAP4_SSL(
                host=self._settings.imap_host,
                port=self._settings.imap_port,
                timeout=_OP_TIMEOUT,
            )
        else:
            self._imap = IMAP4(
                host=self._settings.imap_host,
                port=self._settings.imap_port,
                timeout=_OP_TIMEOUT,
            )
        await self._imap.wait_hello_from_server()
        resp = await self._imap.login(self._settings.imap_user, self._settings.imap_password)
        if resp.result != "OK":
            raise RuntimeError(f"IMAP LOGIN failed: {resp.result} {resp.lines!r}")
        resp = await self._imap.select(self._settings.imap_folder)
        if resp.result != "OK":
            raise RuntimeError(
                f"IMAP SELECT '{self._settings.imap_folder}' failed: {resp.result} {resp.lines!r}"
            )
        logger.info(
            "imap_connected",
            extra={
                "host": self._settings.imap_host,
                "user": self._settings.imap_user,
                "folder": self._settings.imap_folder,
            },
        )

    async def close(self) -> None:
        self._shutdown.set()
        if self._imap is None:
            return
        try:
            if self._imap.has_pending_idle():
                self._imap.idle_done()
            await self._imap.logout()
        except (TimeoutError, AioImapException, Abort, OSError):
            pass
        self._imap = None

    # --- Reading -------------------------------------------------------------

    async def fetch_unseen(self) -> list[InboundMail]:
        """Return all currently unseen mails in the selected folder."""
        assert self._imap is not None
        resp = await self._imap.uid_search("UNSEEN")
        if resp.result != "OK":
            raise RuntimeError(f"UID SEARCH failed: {resp.lines!r}")
        if not resp.lines:
            return []
        first = resp.lines[0] if isinstance(resp.lines[0], bytes) else resp.lines[0].encode()
        if not first.strip():
            return []
        uids = [int(u) for u in first.split()]
        out: list[InboundMail] = []
        for uid in uids:
            raw = await self._fetch_raw(uid)
            if raw is not None:
                out.append(InboundMail(uid=uid, raw=raw))
        return out

    async def _fetch_raw(self, uid: int) -> bytes | None:
        """Fetch one mail's full RFC 822 source without marking it as Seen."""
        assert self._imap is not None
        resp = await self._imap.uid("fetch", str(uid), "BODY.PEEK[]")
        if resp.result != "OK":
            logger.warning("imap_fetch_failed", extra={"uid": uid, "result": resp.result})
            return None
        # aioimaplib returns lines like:
        # [b'1 FETCH (UID 42 BODY[] {12345}', b'<12345 bytes>', b')', ...]
        # We need to locate the literal payload (the bytes line right after the size marker).
        lines = resp.lines
        for idx, line in enumerate(lines):
            if not isinstance(line, (bytes, bytearray)):
                continue
            match = _RFC822_RE.search(line)
            if not match:
                continue
            size = int(match.group(1))
            if idx + 1 >= len(lines):
                return None
            payload = lines[idx + 1]
            if isinstance(payload, str):
                payload = payload.encode("utf-8", errors="replace")
            # Some servers add trailing newline; trim to declared size.
            if size and len(payload) > size:
                payload = payload[:size]
            return bytes(payload)
        return None

    # --- IDLE / Polling driver ----------------------------------------------

    async def stream(self) -> AsyncIterator[list[InboundMail]]:
        """Yield batches of new mails forever.

        First yield is the current backlog. Subsequent yields come either from
        IMAP IDLE notifications or from a polling loop, depending on settings.
        """
        # Always drain backlog first so a restart picks up missed mails.
        backlog = await self.fetch_unseen()
        if backlog:
            yield backlog

        if self._settings.imap_use_idle:
            async for batch in self._idle_loop():
                yield batch
        else:
            async for batch in self._poll_loop():
                yield batch

    async def _poll_loop(self) -> AsyncIterator[list[InboundMail]]:
        interval = self._settings.imap_poll_interval_seconds
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                # If we get here, shutdown was signalled.
                return
            except TimeoutError:
                pass
            try:
                mails = await self.fetch_unseen()
            except (AioImapException, Abort, OSError) as exc:
                logger.warning("imap_poll_error", extra={"error": str(exc)})
                await self._reconnect()
                continue
            if mails:
                yield mails

    async def _idle_loop(self) -> AsyncIterator[list[InboundMail]]:
        assert self._imap is not None
        while not self._shutdown.is_set():
            try:
                idle = await self._imap.idle_start(timeout=_IDLE_REFRESH_SECONDS)
                # Wait for any server push or for the IDLE refresh window to elapse.
                try:
                    push = await asyncio.wait_for(
                        self._imap.wait_server_push(),
                        timeout=_IDLE_REFRESH_SECONDS + 30,
                    )
                except TimeoutError:
                    push = None
                if self._imap is not None and self._imap.has_pending_idle():
                    self._imap.idle_done()
                try:
                    await asyncio.wait_for(idle, timeout=10)
                except TimeoutError:
                    pass
                # ``push`` will contain EXISTS / EXPUNGE notifications. A new
                # EXISTS means at least one new mail arrived; we just re-run
                # the UNSEEN search to find them all.
                if _push_indicates_new_mail(push):
                    try:
                        mails = await self.fetch_unseen()
                    except (AioImapException, Abort, OSError) as exc:
                        logger.warning("imap_fetch_after_push_failed", extra={"error": str(exc)})
                        await self._reconnect()
                        continue
                    if mails:
                        yield mails
            except (AioImapException, Abort, OSError) as exc:
                logger.warning("imap_idle_error", extra={"error": str(exc)})
                await self._reconnect()

    async def _reconnect(self, backoff: int = 5) -> None:
        if self._shutdown.is_set():
            return
        logger.info("imap_reconnecting", extra={"backoff": backoff})
        try:
            await self.close()
        except Exception:
            pass
        await asyncio.sleep(backoff)
        try:
            await self.connect()
        except Exception as exc:
            logger.warning("imap_reconnect_failed", extra={"error": str(exc)})

    # --- Per-mail mutations --------------------------------------------------

    async def delete(self, uid: int) -> None:
        assert self._imap is not None
        await self._imap.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        await self._imap.expunge()

    async def mark_seen(self, uid: int) -> None:
        assert self._imap is not None
        await self._imap.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")

    async def move(self, uid: int, target_folder: str) -> None:
        """Move ``uid`` to ``target_folder``, creating it if missing."""
        assert self._imap is not None
        # CREATE is idempotent enough: servers return NO if it exists, which
        # we ignore.
        await self._imap.create(target_folder)
        try:
            resp = await self._imap.uid("MOVE", str(uid), target_folder)
            if resp.result == "OK":
                return
        except (AioImapException, Abort):
            pass
        # Fallback for servers without RFC 6851 MOVE: COPY + DELETE + EXPUNGE.
        await self._imap.uid("COPY", str(uid), target_folder)
        await self._imap.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        await self._imap.expunge()


def _push_indicates_new_mail(push: object) -> bool:
    """Inspect aioimaplib's idle-push payload for EXISTS/RECENT lines."""
    if push is None:
        return False
    items: list[object]
    if isinstance(push, list):
        items = list(push)
    elif hasattr(push, "lines"):
        items = list(push.lines)
    else:
        items = [push]
    for item in items:
        if isinstance(item, bytes | bytearray):
            text = item.decode("ascii", errors="replace").upper()
        else:
            text = str(item).upper()
        if " EXISTS" in text or " RECENT" in text:
            return True
    return False


# --- Lifecycle dispatcher ----------------------------------------------------

LifecycleAction = Callable[[int], Awaitable[None]]


def make_lifecycle_actions(
    client: ImapMailClient, settings: Settings
) -> tuple[LifecycleAction, LifecycleAction]:
    """Return ``(on_success, on_reject)`` callables that map env config to IMAP ops."""

    async def on_success(uid: int) -> None:
        action = settings.mail_on_success.value
        if action == "delete":
            await client.delete(uid)
        elif action == "move_processed":
            await client.move(uid, settings.processed_folder)
        elif action == "keep":
            await client.mark_seen(uid)

    async def on_reject(uid: int) -> None:
        action = settings.mail_on_reject.value
        if action == "delete":
            await client.delete(uid)
        elif action == "keep":
            await client.mark_seen(uid)

    return on_success, on_reject
