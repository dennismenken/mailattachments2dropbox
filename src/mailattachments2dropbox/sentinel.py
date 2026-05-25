"""Route mails by parsing a body sentinel and resolving it against the mapping.

The sentinel syntax (defaults: ``:::``) looks like:

    :::<branch>                 -> branch's default subfolder
    :::<branch>:::<subfolder>   -> explicit subfolder

If no sentinel is present anywhere in the body the routing falls back to
``DEFAULT_BRANCH`` / ``DEFAULT_SUBFOLDER_KEY`` from the env settings.

If a sentinel IS present but references unknown branch/subfolder keys, we
deliberately return a ``RoutingError`` so the pipeline rejects the mail and
leaves it in the inbox for the operator to inspect, instead of silently
defaulting it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from .config import Mapping, Settings


@dataclass(frozen=True)
class RoutingDecision:
    """Where in Dropbox attachments for this mail should land."""

    branch_key: str
    branch_folder: str
    subfolder_key: str  # may be "" if branch has no subfolders
    subfolder_name: str  # may be "" if branch has no subfolders
    target_subpath: str  # relative to dropbox_root_path, no leading slash
    matched_token: str | None  # raw sentinel found in the body, or None if defaults applied


@dataclass(frozen=True)
class RoutingError:
    """A sentinel was present but could not be resolved against the mapping."""

    reason: str
    matched_token: str


def _build_pattern(prefix: str, separator: str) -> re.Pattern[str]:
    """Compile a regex that finds ``<prefix><branch>[<separator><subfolder>]``.

    branch/subfolder tokens are alphanumeric (lower) and allow hyphens or
    underscores so users can name a subfolder ``mein-konto`` if they want.
    """
    p = re.escape(prefix)
    s = re.escape(separator)
    return re.compile(rf"{p}([a-zA-Z0-9_\-]+)(?:{s}([a-zA-Z0-9_\-]+))?")


def parse_sentinel(body: str, prefix: str, separator: str) -> tuple[str, str | None, str] | None:
    """Find the first sentinel match in the body.

    Returns ``(branch_key, subfolder_key_or_none, raw_matched_token)`` or None.
    """
    pattern = _build_pattern(prefix, separator)
    match = pattern.search(body or "")
    if not match:
        return None
    branch = match.group(1).lower()
    subfolder = match.group(2)
    if subfolder is not None:
        subfolder = subfolder.lower()
    return branch, subfolder, match.group(0)


def _join_subpath(branch_folder: str, subfolder_name: str) -> str:
    if subfolder_name:
        return f"{branch_folder}/{subfolder_name}"
    return branch_folder


def route(body: str, mapping: Mapping, settings: Settings) -> RoutingDecision | RoutingError:
    """Decide where this mail's attachments should be uploaded.

    Returns a ``RoutingDecision`` on success or a ``RoutingError`` if a sentinel
    is present but references an unknown branch or subfolder key.
    """
    parsed = parse_sentinel(body, settings.sentinel_prefix, settings.sentinel_separator)

    if parsed is None:
        # No sentinel anywhere in the body: apply defaults.
        branch = mapping.get_branch(settings.default_branch)
        if branch is None:
            return RoutingError(
                reason=(
                    f"DEFAULT_BRANCH '{settings.default_branch}' is not defined in mapping.yaml"
                ),
                matched_token="<default>",
            )
        subfolder_key = settings.default_subfolder_key
        subfolder_name = ""
        if subfolder_key:
            if subfolder_key not in branch.subfolders:
                return RoutingError(
                    reason=(
                        f"DEFAULT_SUBFOLDER_KEY '{subfolder_key}' is not defined under "
                        f"branch '{settings.default_branch}' in mapping.yaml"
                    ),
                    matched_token="<default>",
                )
            subfolder_name = branch.subfolders[subfolder_key]
        return RoutingDecision(
            branch_key=settings.default_branch,
            branch_folder=branch.folder,
            subfolder_key=subfolder_key,
            subfolder_name=subfolder_name,
            target_subpath=_join_subpath(branch.folder, subfolder_name),
            matched_token=None,
        )

    branch_key, parsed_subfolder, raw = parsed
    branch = mapping.get_branch(branch_key)
    if branch is None:
        return RoutingError(
            reason=f"branch '{branch_key}' is not defined in mapping.yaml",
            matched_token=raw,
        )

    if parsed_subfolder is None:
        # Sentinel was ":::<branch>" without a subfolder portion.
        if branch.default_subfolder:
            if branch.default_subfolder not in branch.subfolders:
                return RoutingError(
                    reason=(
                        f"default_subfolder '{branch.default_subfolder}' for branch "
                        f"'{branch_key}' is not present in mapping.subfolders"
                    ),
                    matched_token=raw,
                )
            subfolder_name = branch.subfolders[branch.default_subfolder]
            subfolder_key_used = branch.default_subfolder
        else:
            subfolder_name = ""
            subfolder_key_used = ""
    else:
        if parsed_subfolder not in branch.subfolders:
            return RoutingError(
                reason=(
                    f"subfolder key '{parsed_subfolder}' is not defined under branch "
                    f"'{branch_key}' in mapping.yaml"
                ),
                matched_token=raw,
            )
        subfolder_name = branch.subfolders[parsed_subfolder]
        subfolder_key_used = parsed_subfolder

    return RoutingDecision(
        branch_key=branch_key,
        branch_folder=branch.folder,
        subfolder_key=subfolder_key_used,
        subfolder_name=subfolder_name,
        target_subpath=_join_subpath(branch.folder, subfolder_name),
        matched_token=raw,
    )


def sender_allowed(sender: str, allow_list: list[str]) -> tuple[bool, str | None]:
    """Check whether ``sender`` matches at least one pattern in ``allow_list``.

    Empty allow_list means "no restriction" (always allowed).
    Returns ``(allowed, matched_rule_or_None)``.
    """
    if not allow_list:
        return True, None
    sender = (sender or "").strip().lower()
    if not sender:
        # No From header at all is suspicious; reject when an allow-list is configured.
        return False, None
    for rule in allow_list:
        pattern = rule.strip().lower()
        if not pattern:
            continue
        if fnmatch.fnmatchcase(sender, pattern):
            return True, rule
    return False, None


def extension_allowed(filename: str, allow_list: list[str]) -> bool:
    """Empty allow_list means "no restriction"."""
    if not allow_list:
        return True
    if "." not in filename:
        # No extension cannot be matched by an allow-list; reject conservatively.
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in allow_list
