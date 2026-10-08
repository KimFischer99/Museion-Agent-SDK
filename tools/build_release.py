#!/usr/bin/env python3
"""Build release/runtime and release/skills without changing development files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from license_gate import (
    REFERENCE_PREFIX,
    collect_forbidden_hashes,
    load_reference_hashes,
    reference_member_errors,
)
from package_gate import check_archive, iter_members, scan_text_members

REPO = Path(__file__).resolve().parents[1]


def build_release(destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"{destination} already exists; use --output for a new directory")
    references = load_reference_hashes(REPO)
    forbidden = collect_forbidden_hashes(
        json.loads((REPO / "tests/fixtures/selected-source-manifest.json").read_text()),
        json.loads((REPO / "tests/fixtures/skills.json").read_text()),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pas-release-", dir=destination.parent) as tmp:
        work = Path(tmp)
        source = work / "source"
        source.mkdir()
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(REPO / name, source / name)
        shutil.copytree(
            REPO / "src/proactive_sdk", source / "src/proactive_sdk",
            ignore=shutil.ignore_patterns("_deployment_reference", "__pycache__", "*.pyc", ".DS_Store"),
        )
        staged = work / "release"
        runtime = staged / "runtime"
        runtime.mkdir(parents=True)
        command = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                   "--no-index", str(source), "-w", str(runtime)]
        result = subprocess.run(command, capture_output=True, text=True,
                                env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        wheels = list(runtime.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("expected exactly one runtime wheel")
        wheel = wheels[0]
        problems = check_archive(wheel, forbidden) + scan_text_members(wheel)
        if problems:
            raise ValueError("\n".join(problems))
        members = {name for name, _ in iter_members(wheel)}
        assert "proactive_sdk/pi_worker/pi_worker.ts" in members
        assert not any(name.startswith(("tests/", "tools/", "docs/", "examples/", "schemas/",
                                        "packages/", "deploy/", "proactive_sdk/_deployment_reference/"))
                       for name in members)
        shutil.copy2(REPO / "README.md", runtime / "README.md")
        shutil.copy2(REPO / "examples/release_app.py", runtime / "app.py")

        reference_root = REPO / "src" / REFERENCE_PREFIX
        skills = staged / "skills"
        shutil.copytree(reference_root / "skills", skills)
        for name in ("README.md", "NOTICE.md", "manifest.json"):
            shutil.copy2(reference_root / name, skills / name)
        hashes = {
            REFERENCE_PREFIX + (path.name if path.parent == skills and path.name in
                                {"README.md", "NOTICE.md", "manifest.json"}
                                else "skills/" + path.relative_to(skills).as_posix()):
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in skills.rglob("*") if path.is_file()
        }
        problems = reference_member_errors(hashes, references)
        if problems:
            raise ValueError("\n".join(problems))
        assert set(path.name for path in runtime.iterdir()) == {wheel.name, "README.md", "app.py"}
        staged.rename(destination)
        print(f"RELEASE: {destination}")
        print(f"runtime: {wheel.name} ({(destination / 'runtime' / wheel.name).stat().st_size} bytes)")
        print(f"skills: {len(references)} source files; exact contents verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / "release")
    build_release(parser.parse_args().output)
