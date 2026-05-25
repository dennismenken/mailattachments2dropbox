"""Configuration layer.

Two sources: environment variables (loaded via pydantic-settings, optionally from .env)
and a YAML mapping file that describes the Dropbox folder taxonomy.

Both are validated at startup so a misconfigured deployment fails fast instead of
silently misrouting attachments.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class SuccessAction(StrEnum):
    KEEP = "keep"
    MOVE_PROCESSED = "move_processed"
    DELETE = "delete"


class RejectAction(StrEnum):
    KEEP = "keep"
    DELETE = "delete"


def _csv(value: str | list[str] | None) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v and v.strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    """Process-wide settings sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # IMAP connection
    imap_host: str
    imap_port: int = 993
    imap_user: str
    imap_password: str
    imap_use_tls: bool = True
    imap_folder: str = "INBOX"
    imap_use_idle: bool = True
    imap_poll_interval_seconds: int = 300

    # Mail lifecycle behavior
    mail_on_success: SuccessAction = SuccessAction.DELETE
    mail_on_reject: RejectAction = RejectAction.DELETE
    processed_folder: str = "Processed"

    # Allow lists (empty means "no restriction"). Provided as CSV in the env vars.
    allowed_senders: Annotated[list[str], NoDecode, Field(default_factory=list)]
    allowed_extensions: Annotated[list[str], NoDecode, Field(default_factory=lambda: ["pdf"])]

    # Sentinel syntax
    sentinel_prefix: str = ":::"
    sentinel_separator: str = ":::"
    default_branch: str = "in"
    default_subfolder_key: str = "auto"

    # Dropbox
    dropbox_app_key: str
    dropbox_app_secret: str
    dropbox_refresh_token: str
    dropbox_root_path: str = "/"

    # Mapping file
    mapping_file: Path = Path("/app/mapping.yaml")

    # Logging
    log_dir: Path = Path("/app/logs")
    log_filename: str = "mailattachments2dropbox.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 10
    log_retention_days: int = 30
    log_level: str = "INFO"

    @field_validator("allowed_senders", "allowed_extensions", mode="before")
    @classmethod
    def _split_csv(cls, value: str | list[str] | None) -> list[str]:
        return _csv(value)

    @field_validator("allowed_extensions", mode="after")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        return [v.lower().lstrip(".") for v in value]

    @field_validator("dropbox_root_path", mode="after")
    @classmethod
    def _normalize_root(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/"):
            value = "/" + value
        return value.rstrip("/")


class BranchConfig(BaseModel):
    """Definition of one routing branch (e.g. 'in' or 'out')."""

    folder: str
    default_subfolder: str = ""
    subfolders: dict[str, str] = Field(default_factory=dict)

    @field_validator("folder")
    @classmethod
    def _strip_folder(cls, value: str) -> str:
        return value.strip().strip("/")


class Mapping(BaseModel):
    """Top-level mapping document."""

    branches: dict[str, BranchConfig]

    def get_branch(self, key: str) -> BranchConfig | None:
        return self.branches.get(key.lower())


def load_mapping(path: str | Path) -> Mapping:
    """Load and validate the mapping YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Mapping file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Mapping.model_validate(raw)


def validate_defaults(settings: Settings, mapping: Mapping) -> None:
    """Make sure DEFAULT_BRANCH / DEFAULT_SUBFOLDER_KEY actually resolve in the mapping."""
    branch = mapping.get_branch(settings.default_branch)
    if branch is None:
        raise ValueError(
            f"DEFAULT_BRANCH '{settings.default_branch}' is not present in mapping.yaml"
        )
    # Allow empty subfolder key only when the branch defines no subfolders.
    if (
        settings.default_subfolder_key
        and settings.default_subfolder_key not in branch.subfolders
        and branch.subfolders
    ):
        raise ValueError(
            f"DEFAULT_SUBFOLDER_KEY '{settings.default_subfolder_key}' is not present "
            f"in mapping.yaml under branch '{settings.default_branch}'"
        )
