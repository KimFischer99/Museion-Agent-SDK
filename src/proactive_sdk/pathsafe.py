"""Path containment guards (P0 acceptance: zip-slip / path escape).

Used by the hook runner (staging dirs), the skill importer (extracting
vendor archives) and the blob store. Everything here is pure path logic
plus filesystem checks; no network, no subprocess.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

__all__ = ["PathSafetyError", "safe_join", "ensure_within", "validate_zip_member"]

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PathSafetyError(ValueError):
    """A path attempted to escape its containment boundary."""


def _reject_special(name: str) -> None:
    if "\x00" in name:
        raise PathSafetyError("NUL byte in path")


def safe_join(base: Path, *parts: str) -> Path:
    """Join ``parts`` under ``base``, refusing any escape.

    Rejects absolute components, ``..`` segments (before and after
    normalization) and, on real filesystems, symlink escapes: the resolved
    result must stay inside the resolved base.
    """
    base_resolved = base.resolve()
    current = base_resolved
    for part in parts:
        _reject_special(part)
        candidate = Path(part)
        if candidate.is_absolute() or _DRIVE_RE.match(part):
            raise PathSafetyError(f"absolute path component not allowed: {part!r}")
        posix = PurePosixPath(part.replace("\\", "/"))
        if ".." in posix.parts:
            raise PathSafetyError(f"parent-directory segment not allowed: {part!r}")
        current = current / candidate
    resolved = current.resolve()
    ensure_within(base_resolved, resolved)
    return resolved


def ensure_within(base: Path, candidate: Path) -> Path:
    """Return ``candidate`` resolved, verifying it lies inside ``base``."""
    base_resolved = base.resolve()
    candidate_resolved = candidate.resolve()
    if candidate_resolved == base_resolved:
        return candidate_resolved
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError:
        raise PathSafetyError(
            f"path escapes containment: {candidate!r} is outside {base!r}"
        ) from None
    return candidate_resolved


def validate_zip_member(name: str) -> str:
    """Validate a zip entry name for extraction; return its normalized
    posix form.

    Rejects absolute paths, Windows drive letters, backslash-separated
    traversals and any exact ``..`` segment (zip slip). Pure validation —
    callers still extract through :func:`safe_join`.
    """
    _reject_special(name)
    if not name or name.strip() == "":
        raise PathSafetyError("empty zip member name")
    if name.startswith("/") or name.startswith("\\"):
        raise PathSafetyError(f"absolute zip member: {name!r}")
    if _DRIVE_RE.match(name):
        raise PathSafetyError(f"drive-letter zip member: {name!r}")
    normalized = name.replace("\\", "/")
    if any(seg == ".." for seg in normalized.split("/")):
        raise PathSafetyError(f"parent-directory segment in zip member: {name!r}")
    return normalized
