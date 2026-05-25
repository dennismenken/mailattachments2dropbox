"""Thin wrapper around the official Dropbox SDK.

Uses the refresh-token auth flow so the worker can run unattended for months:
the SDK transparently mints short-lived access tokens from the configured
``DROPBOX_REFRESH_TOKEN`` whenever the current one expires.

Uploads use ``autorename=True`` so a duplicate filename ``rechnung.pdf`` is
stored as ``rechnung (1).pdf`` instead of failing or overwriting.
"""

from __future__ import annotations

from dataclasses import dataclass

import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import CreateFolderError, WriteMode

from .config import Settings


@dataclass(frozen=True)
class UploadResult:
    requested_path: str
    final_path: str
    renamed: bool
    size: int


class DropboxClient:
    def __init__(self, settings: Settings) -> None:
        self._root = settings.dropbox_root_path.rstrip("/")
        self._dbx = dropbox.Dropbox(
            app_key=settings.dropbox_app_key,
            app_secret=settings.dropbox_app_secret,
            oauth2_refresh_token=settings.dropbox_refresh_token,
            timeout=60,
        )

    def check(self) -> str:
        """Confirm the credentials work; returns the account display name."""
        account = self._dbx.users_get_current_account()
        return str(account.name.display_name)

    def ensure_folder(self, subpath: str) -> None:
        """Create ``<root>/<subpath>`` recursively if any segments are missing.

        Dropbox's create_folder_v2 is not recursive on its own, so we walk the
        path segments. ``conflict`` errors (folder already exists) are ignored.
        """
        if not subpath:
            return
        accumulated = self._root
        for segment in subpath.strip("/").split("/"):
            if not segment:
                continue
            accumulated = f"{accumulated}/{segment}"
            try:
                self._dbx.files_create_folder_v2(accumulated, autorename=False)
            except ApiError as exc:
                err = exc.error
                if isinstance(err, CreateFolderError) and err.is_path():
                    path_err = err.get_path()
                    if path_err.is_conflict():
                        continue
                raise

    def upload(self, subpath: str, filename: str, content: bytes) -> UploadResult:
        """Upload ``content`` to ``<root>/<subpath>/<filename>``.

        With ``autorename=True`` Dropbox appends ``(1)``, ``(2)`` etc. on
        name collisions and returns the resolved path.
        """
        requested = f"{self._root}/{subpath.strip('/')}/{filename}".replace("//", "/")
        result = self._dbx.files_upload(
            content,
            requested,
            mode=WriteMode.add,
            autorename=True,
            mute=True,
        )
        return UploadResult(
            requested_path=requested,
            final_path=result.path_display,
            renamed=(result.path_display != requested),
            size=result.size,
        )
