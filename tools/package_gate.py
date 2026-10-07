#!/usr/bin/env python3
"""P7 package gate (SPEC §17.3.2/17.3.3): scan the BUILT distribution.

The P0 license gate scans git-tracked files; this gate scans the actual
artifacts a user would receive — wheel/sdist members plus their hashes —
against the same forbidden sets (private-vendor paths, recorded audit
hashes, helper hash) plus generic secret-ish members (.pem/.key, token
files, .DS_Store). A clean git tree does not excuse a dirty package.

Usage:
  python tools/package_gate.py dist/*.whl dist/*.tar.gz
Exit 0 = all artifacts pass; 1 = violations; 2 = environment error.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from license_gate import (  # noqa: E402  (local tool import, pinned contract)
    BANNED_BASENAMES,
    BANNED_PREFIXES,
    collect_forbidden_hashes,
)

SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".env")
SECRET_NAME_PARTS = ("token", "credential", "secret")

BANNED_CONTAINER_PREFIXES = BANNED_PREFIXES + ("muse", "private_vendor/")
BANNED_CONTAINER_NAMES = BANNED_BASENAMES | {"private-vendor", "muse-sdk"}


def iter_members(archive: Path) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    if archive.name.endswith(".whl") or archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                members.append((name, zf.read(name)))
    elif archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                handle = tf.extractfile(member)
                if handle is not None:
                    members.append((member.name, handle.read()))
    else:
        raise SystemExit(f"unsupported archive type: {archive}")
    return members


def check_archive(archive: Path, forbidden_hashes: set[str]) -> list[str]:
    problems: list[str] = []
    for name, payload in iter_members(archive):
        posix = name.replace("\\", "/")
        lowered = posix.lower()
        if any(lowered.startswith(p) for p in BANNED_CONTAINER_PREFIXES):
            problems.append(f"{archive.name}: banned path member {posix}")
        if Path(posix).name in BANNED_CONTAINER_NAMES:
            problems.append(f"{archive.name}: banned member name {posix}")
        if lowered.endswith(SECRET_SUFFIXES):
            problems.append(f"{archive.name}: secret-shaped member {posix}")
        base = Path(posix).name.lower()
        if any(part in base for part in SECRET_NAME_PARTS):
            problems.append(f"{archive.name}: secret-named member {posix}")
        digest = hashlib.sha256(payload).hexdigest()
        if digest in forbidden_hashes:
            problems.append(f"{archive.name}: member {posix} matches a forbidden audit hash")
    return problems


_PEM_BLOCK_RE = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----\r?\n"
    rb"[A-Za-z0-9+/=\r\n]{80,}"
    rb"-----END [A-Z ]*PRIVATE KEY-----"
)


def scan_text_members(archive: Path) -> list[str]:
    """Content scan of text members: an actual PEM private-key block
    (header + base64 body + END). A bare header mention — e.g. the SDK's
    own log-redaction pattern — is not a key and does not match."""
    problems: list[str] = []
    for name, payload in iter_members(archive):
        low = name.lower()
        if not low.endswith((".py", ".md", ".txt", ".json", ".sql", ".cfg", ".toml")):
            continue
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _PEM_BLOCK_RE.search(payload):
            problems.append(f"{archive.name}: private key block inside {name}")
    return problems


def main(argv: list[str]) -> int:
    archives = [Path(a) for a in argv]
    if not archives:
        print("usage: package_gate.py dist/*.whl dist/*.tar.gz", file=sys.stderr)
        return 2
    manifest = json.loads(
        (REPO / "audit" / "selected-source-manifest.json").read_text(encoding="utf-8")
    )
    skills_doc = json.loads((REPO / "audit" / "skills.json").read_text(encoding="utf-8"))
    forbidden = collect_forbidden_hashes(manifest, skills_doc)

    all_problems: list[str] = []
    for archive in archives:
        if not archive.is_file():
            print(f"missing artifact: {archive}", file=sys.stderr)
            return 2
        all_problems.extend(check_archive(archive, forbidden))
        all_problems.extend(scan_text_members(archive))

    if all_problems:
        print("PACKAGE GATE: BLOCKED")
        for problem in all_problems:
            print(f"  - {problem}")
        return 1
    print(f"PACKAGE GATE: PASS ({len(archives)} archive(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
