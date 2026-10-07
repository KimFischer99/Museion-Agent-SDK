#!/usr/bin/env python3
"""P0 release gate (SPEC §15.1 P0 / §17.3).

Blocks a release when:
  1. any private-source path is tracked by git (the vendor snapshot
     prefixes in BANNED_PREFIXES, plus __MACOSX/, .DS_Store);
  2. any tracked file's SHA-256 matches a hash recorded in the audit
     manifests (selected-source-manifest.json, skills.json) or the known
     private helper hash;
  3. docs/LICENSES.md is missing, or its license-gate marker says
     ``status: blocked`` (unknown-permission content present).

Usage: python tools/license_gate.py [--repo .]
Exit code 0 = gate passed; 1 = blocked; 2 = environment error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

BANNED_PREFIXES = ("private-vendor/", "muse-sdk/", "muse-reuse/", "__MACOSX/")
BANNED_BASENAMES = {".DS_Store"}
HELPER_SHA256 = "c87af221181a1559e3adcb0cdd601f5be5d4bd91eec2fe0597c09dc27558e741"
GATE_MARKER_RE = re.compile(r"<!--\s*license-gate\s*(.*?)-->", re.DOTALL)
STATUS_RE = re.compile(r"^\s*status:\s*(\w+)\s*$", re.MULTILINE)


def banned_tracked_paths(tracked: list[str]) -> list[str]:
    violations = []
    for path in tracked:
        posix = path.replace("\\", "/")
        if posix.startswith(BANNED_PREFIXES) or Path(posix).name in BANNED_BASENAMES:
            violations.append(path)
    return violations


def collect_forbidden_hashes(manifest: dict, skills_doc: dict) -> set[str]:
    hashes = {HELPER_SHA256}
    hashes.update(source["sha256"] for source in manifest.get("sources", []))
    hashes.update(entry["sha256"] for entry in skills_doc.get("skills", []))
    return hashes


def find_forbidden_hash_matches(tracked_hashes: dict[str, str], forbidden: set[str]) -> list[str]:
    return sorted(path for path, digest in tracked_hashes.items() if digest in forbidden)


def read_gate_status(licenses_text: str) -> str | None:
    marker = GATE_MARKER_RE.search(licenses_text)
    if not marker:
        return None
    status = STATUS_RE.search(marker.group(1))
    return status.group(1) if status else None


def git_tracked_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return sorted(p for p in result.stdout.decode("utf-8").split("\x00") if p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    repo: Path = args.repo

    try:
        tracked = git_tracked_files(repo)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"license_gate: cannot list git files: {exc}", file=sys.stderr)
        return 2

    blocked: list[str] = []

    path_violations = banned_tracked_paths(tracked)
    if path_violations:
        blocked.append(f"Private-source paths tracked in git: {path_violations}")

    tracked_hashes: dict[str, str] = {}
    for relpath in tracked:
        file_path = repo / relpath
        if file_path.is_file():
            tracked_hashes[relpath] = hashlib.sha256(file_path.read_bytes()).hexdigest()

    manifest_path = repo / "audit/selected-source-manifest.json"
    skills_path = repo / "audit/skills.json"
    if not manifest_path.is_file() or not skills_path.is_file():
        print("license_gate: audit manifests missing; cannot build forbidden-hash set", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    skills_doc = json.loads(skills_path.read_text(encoding="utf-8"))
    forbidden = collect_forbidden_hashes(manifest, skills_doc)
    hash_matches = find_forbidden_hash_matches(tracked_hashes, forbidden)
    if hash_matches:
        blocked.append(f"tracked files match recorded private-source hashes: {hash_matches}")

    licenses_path = repo / "docs/LICENSES.md"
    if not licenses_path.is_file():
        blocked.append("docs/LICENSES.md missing")
    else:
        status = read_gate_status(licenses_path.read_text(encoding="utf-8"))
        if status is None:
            blocked.append("docs/LICENSES.md has no <!-- license-gate --> status marker")
        elif status != "pass":
            blocked.append(f"docs/LICENSES.md gate status is {status!r} (blocked)")

    if blocked:
        print("license_gate: BLOCKED")
        for item in blocked:
            print(f"  - {item}")
        return 1
    print(
        f"license_gate: PASS ({len(tracked_hashes)} tracked files scanned, "
        f"{len(forbidden)} forbidden hashes, gate status pass)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
