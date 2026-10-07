"""Local artifact references (SPEC §21.1 step 8).

When a notification asks the owner to open something this run produced —
a Markdown report, an HTML page, an image — the message body must carry a
reference the owner can actually open. This module owns the one grammar
such a reference may use, so the rule is enforced identically for a
model proposal and for a frozen direct reminder.

Grammar::

    artifact:<relative/posix/path>

Deliberately restrictive:

* no absolute paths and no Windows drive letters — a product must never
  bake a machine-local path into a message (AGENTS.md: 不要把本机私人
  路径写成产品默认值);
* no ``..`` segment, so a reference cannot point outside the profile's
  artifact area;
* a bounded segment count and length, so a reference stays a reference and
  not a payload.

The scheme is a *name*, not a capability: resolving it to a real file and
deciding who may read that file stays with the host.
"""

from __future__ import annotations

import re

from .contracts import ErrorCode, PASError

__all__ = [
    "ARTIFACT_SCHEME",
    "MAX_ARTIFACT_REF_CHARS",
    "ArtifactRefError",
    "normalize_artifact_ref",
    "missing_artifact_refs",
]

ARTIFACT_SCHEME = "artifact:"
MAX_ARTIFACT_REF_CHARS = 512

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ArtifactRefError(PASError):
    """An artifact reference that is not openable as written."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INVALID_CONFIG, message, scope="artifacts")


def normalize_artifact_ref(ref: str) -> str:
    """Validate and normalize one artifact reference.

    Returns the canonical ``artifact:...`` form. Raises
    :class:`ArtifactRefError` for anything that is not provably openable
    and contained.
    """
    if not isinstance(ref, str) or not ref:
        raise ArtifactRefError("artifact ref must be a non-empty string")
    if len(ref) > MAX_ARTIFACT_REF_CHARS:
        raise ArtifactRefError(
            f"artifact ref exceeds {MAX_ARTIFACT_REF_CHARS} chars"
        )
    if not ref.startswith(ARTIFACT_SCHEME):
        raise ArtifactRefError(
            f"artifact ref must start with {ARTIFACT_SCHEME!r}: {ref!r}"
        )
    body = ref[len(ARTIFACT_SCHEME):]
    if not body:
        raise ArtifactRefError("artifact ref has an empty path")
    if body.startswith("/") or body.startswith("\\"):
        raise ArtifactRefError(f"artifact ref must be relative: {ref!r}")
    if _DRIVE_RE.match(body):
        raise ArtifactRefError(f"artifact ref must not carry a drive letter: {ref!r}")
    if "\\" in body:
        raise ArtifactRefError(f"artifact ref must use posix separators: {ref!r}")
    segments = body.split("/")
    if len(segments) > 8:
        raise ArtifactRefError(f"artifact ref is nested too deeply: {ref!r}")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ArtifactRefError(f"artifact ref has an invalid segment: {ref!r}")
        if not _SEGMENT_RE.fullmatch(segment):
            raise ArtifactRefError(f"artifact ref segment {segment!r} is not allowed")
    return f"{ARTIFACT_SCHEME}{body}"


def missing_artifact_refs(body: str, refs: list[str] | tuple[str, ...]) -> list[str]:
    """Which of ``refs`` do not appear verbatim in the message ``body``.

    A notification that names artifacts must let the owner open them from
    the message itself; a reference that never reaches the body is an
    unopenable promise, so callers reject the message instead of shipping
    it (SPEC §21.1 step 8).
    """
    if not isinstance(body, str):
        return list(refs)
    return [ref for ref in refs if ref not in body]
