#!/usr/bin/env python3
"""P7 SBOM generator (SPEC §17.3.3): CycloneDX 1.5 JSON for a built wheel.

The SDK's runtime dependency set is the Python standard library only, so
the SBOM's components are: the wheel itself (with one SHA-256 checksum)
and its declared install extras (documented, optional). If a
future version gains real dependencies, extend ``components`` from the
wheel METADATA Requires-Dist — the generator reads METADATA and records
what it finds, so third-party entries cannot be silently omitted.

Usage: python tools/gen_sbom.py dist/proactive_sdk-<ver>-py3-none-any.whl -o sbom.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

__all__ = ["generate_sbom"]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_metadata(text: str) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        if ":" in line:
            key, _, value = line.partition(":")
            headers.setdefault(key.strip().lower(), []).append(value.strip())
    return headers


def generate_sbom(wheel: Path) -> dict:
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        metadata_name = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = _parse_metadata(zf.read(metadata_name).decode("utf-8"))

    version = metadata.get("version", ["unknown"])[0]
    name = metadata.get("name", ["proactive-sdk"])[0]
    requires_dist = metadata.get("requires-dist", [])

    components = [
        {
            "type": "library",
            "bom-ref": f"pkg:pypi/{name.lower()}@{version}",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name.lower()}@{version}",
            "scope": "required",
            "hashes": [{"alg": "SHA-256", "content": _sha256(wheel.read_bytes())}],
            "properties": [
                {"name": "pas:member-count", "value": str(len(names))},
                {"name": "pas:runtime-dependencies", "value": "none (stdlib only)" if not requires_dist else ", ".join(requires_dist)},
            ],
        }
    ]
    for requirement in requires_dist:
        # extras (e.g. jsonschema) are optional; recorded, not hidden.
        components.append(
            {
                "type": "library",
                "bom-ref": f"optional:{requirement}",
                "name": requirement,
                "version": "declared-range",
                "scope": "optional",
                "properties": [
                    {"name": "pas:note", "value": "optional extra; not installed by the runtime"}
                ],
            }
        )

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "component": components[0],
            "properties": [
                {"name": "pas:generator", "value": "tools/gen_sbom.py"},
                {"name": "pas:source-package-file", "value": wheel.name},
                {"name": "pas:private-vendor-included", "value": "false"},
                {"name": "pas:deployment-skill-reference-count", "value": str(sum(n.startswith("proactive_sdk/_deployment_reference/skills/") and n.endswith("/SKILL.md") for n in names))},
            ],
        },
        "components": components[1:],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("sbom.json"))
    args = parser.parse_args(argv)
    if not args.wheel.is_file():
        print(f"wheel not found: {args.wheel}", file=sys.stderr)
        return 2
    bom = generate_sbom(args.wheel)
    args.output.write_text(json.dumps(bom, indent=1, sort_keys=False), encoding="utf-8")
    print(f"SBOM written: {args.output} ({len(bom['components'])} listed deps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
