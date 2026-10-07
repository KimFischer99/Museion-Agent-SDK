"""Backup and restore (OPS-01; §17.3.5 安装备份恢复说明).

Backup uses SQLite's online backup API against the live store — no
locks are held longer than one internal copy, so a running daemon is
not corrupted by the copy. The backup file is written 0600 and a
sidecar ``.meta.json`` records the profile identity, schema version and
SHA-256 so restore can fail closed on any mismatch.

Restore refuses to touch a database whose profile/owner identity or
schema version does not match, refuses while a daemon lock is present,
and performs the swap atomically (copy to a temp file next to the
target, then ``os.replace``). A restore is a destructive operation on
the target file — the caller (CLI/facade) must obtain explicit user
confirmation before invoking it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .contracts import ErrorCode, PASError
from .store import Store

__all__ = ["BackupError", "create_backup", "restore_backup", "read_backup_meta"]

_META_SUFFIX = ".meta.json"


class BackupError(PASError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, message, scope="backup")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(store: Store, path: str | Path) -> dict[str, Any]:
    """Copy the live store into ``path`` (0600) plus a meta sidecar.
    Returns the meta record."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw_dst = sqlite3.connect(str(target))
        try:
            store.db.backup(raw_dst)
        finally:
            raw_dst.close()
    except sqlite3.Error as exc:
        raise BackupError(f"backup copy failed: {exc}") from exc
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass  # e.g. unusual filesystems; the meta check still verifies content
    check = sqlite3.connect(str(target))
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
        integrity = str(row[0]) if row is not None else "unknown"
    finally:
        check.close()
    if integrity != "ok":
        target.unlink(missing_ok=True)
        raise BackupError(f"backup failed integrity check: {integrity}")
    meta = {
        "kind": "pas-backup",
        "meta_version": 1,
        "profile": store.profile,
        "owner_destination": store.owner_destination,
        "schema_version": store.schema_version(),
        "sha256": _sha256_file(target),
        "created_at": store.clock.wall_now_ms(),
    }
    meta_path = target.with_name(target.name + _META_SUFFIX)
    meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(meta_path, 0o600)
    except OSError:
        pass
    return meta


def read_backup_meta(path: str | Path) -> dict[str, Any]:
    backup = Path(path).expanduser()
    meta_path = backup.with_name(backup.name + _META_SUFFIX)
    if not backup.is_file() or not meta_path.is_file():
        raise BackupError(f"backup or meta sidecar missing: {backup}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BackupError(f"meta sidecar is not valid JSON: {exc}") from exc
    if meta.get("kind") != "pas-backup":
        raise BackupError("not a pas-backup meta sidecar")
    return meta


def restore_backup(
    db_path: str | Path,
    backup_path: str | Path,
    *,
    expect_profile: str | None = None,
    expect_owner_destination: str | None = None,
) -> dict[str, Any]:
    """Verify then atomically replace ``db_path`` with ``backup_path``.

    Fail-closed checks, in order: meta sidecar present and consistent,
    file hash matches the recorded SHA-256, backup passes SQLite
    integrity, backup schema version is not newer than this binary's
    latest migration, identity matches the expectation, and no daemon
    lock file sits next to the target (stop the daemon first — restoring
    under a live daemon would fork its state).
    """
    backup = Path(backup_path).expanduser()
    target = Path(db_path).expanduser()
    meta = read_backup_meta(backup)
    if _sha256_file(backup) != meta.get("sha256"):
        raise BackupError("backup file hash does not match the meta sidecar")
    if expect_profile is not None and meta.get("profile") != expect_profile:
        raise BackupError(
            f"backup profile {meta.get('profile')!r} does not match {expect_profile!r}"
        )
    if (
        expect_owner_destination is not None
        and meta.get("owner_destination") != expect_owner_destination
    ):
        raise BackupError(
            f"backup owner destination {meta.get('owner_destination')!r} does not match"
            f" {expect_owner_destination!r}"
        )
    lock_file = target.parent / "daemon.lock"
    if lock_file.is_file():
        raise BackupError(
            f"daemon lock present at {lock_file}; stop the running instance before restore"
        )
    verify = sqlite3.connect(str(backup))
    try:
        row = verify.execute("PRAGMA integrity_check").fetchone()
        integrity = str(row[0]) if row is not None else "unknown"
        version_row = verify.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        schema_version = int(version_row[0]) if version_row and version_row[0] is not None else 0
    except sqlite3.Error as exc:
        raise BackupError(f"backup is not a readable PAS database: {exc}") from exc
    finally:
        verify.close()
    if integrity != "ok":
        raise BackupError(f"backup failed integrity check: {integrity}")
    recorded_version = meta.get("schema_version")
    if recorded_version != schema_version:
        raise BackupError(
            f"meta schema_version {recorded_version!r} does not match the database ({schema_version});"
            " the backup or its sidecar was modified"
        )
    latest_supported = _latest_packaged_migration_version()
    if schema_version > latest_supported:
        raise BackupError(
            f"backup schema version {schema_version} is newer than this binary ({latest_supported});"
            " upgrade the binary before restoring"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=".restore-", dir=str(target.parent))
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(backup, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"restore swap failed: {exc}") from exc
    return {"restored_from": str(backup), "schema_version": schema_version,
            "profile": meta.get("profile")}


def _latest_packaged_migration_version() -> int:
    migrations_dir = Path(__file__).parent / "migrations"
    versions = [
        int(p.name[1:4]) for p in migrations_dir.glob("m*.sql") if p.name[1:4].isdigit()
    ]
    return max(versions) if versions else 0
