#!/usr/bin/env python3
"""Reproduce the legacy skill audit (tests/fixtures/skills.json) and the selected-source
hash manifest (tests/fixtures/selected-source-manifest.json) from a local copy of the
vendor snapshot directory. Read-only: this tool never writes to the audit
directory or the source directory.

Reproduced byte-exactly (compared against the recorded audit):
  - discovered SKILL.md path set (recursive, incl. nested artifacts entries)
  - per-file SHA-256
  - original_name / canonical_name_proposed (with collision detection)
  - description_characters (via the bundled minimal frontmatter parser)
  - issue flags: invalid_name, name_directory_mismatch,
    metadata_values_not_all_strings, invalid_description
  - non_string_metadata type map
  - license_frontmatter
  - audit-level count and issue_counts
  - selected-source manifest: sha256 / bytes / lines per entry

Not reproduced (informational auditor-run columns, verified for presence and
type only): platform_markers_heuristic, additional_tool_tokens_heuristic,
note. Judgment constants are enforced: technical_status,
distribution_status, full_prompt_included.

Usage:
  python tools/reproduce_audit.py --source-dir 01/private-vendor/muse-sdk --audit-dir tests/fixtures
Exit code 0 = audit is reproducible from this directory; 1 = mismatches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024

EXPECTED_TECHNICAL_STATUS = "unverified_requires_capability_audit"
EXPECTED_DISTRIBUTION_STATUS = "no_grant_identified_in_snapshot"
EXPECTED_FULL_PROMPT_INCLUDED = False

MANIFEST_FIELDS = ("sha256", "bytes", "lines")


# --------------------------------------------------------------------------- #
# Minimal frontmatter parsing (no PyYAML dependency)
# --------------------------------------------------------------------------- #


def split_frontmatter(text: str) -> tuple[list[str], str]:
    if not text.startswith("---"):
        return [], text
    lines = text.splitlines()
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return lines[1:idx], "\n".join(lines[idx + 1 :])
    return [], text


def _parse_scalar(raw: str) -> tuple[str, object]:
    """Return (type_name, typed_value) for a YAML plain/quoted scalar."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return "list", raw
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return "str", raw[1:-1]
    if raw in ("true", "True"):
        return "bool", True
    if raw in ("false", "False"):
        return "bool", False
    if raw in ("null", "~", ""):
        return "null", None
    if re.fullmatch(r"-?\d+", raw):
        return "int", int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return "float", float(raw)
    return "str", raw


def _split_flow_items(raw: str) -> list[str]:
    items: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for ch in raw:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        items.append("".join(current))
    return items


def _parse_flow_scalar(raw: str) -> tuple[str, object]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner_items = [
            item.strip() for item in _split_flow_items(raw[1:-1]) if item.strip()
        ]
        return "list", [_parse_flow_scalar(item)[1] for item in inner_items]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return "str", raw[1:-1]
    return _parse_scalar(raw)


def _parse_flow_mapping(raw: str) -> dict[str, object]:
    """Parse a one-line flow mapping: metadata: { "k": v, ... }."""
    inner = raw.strip()[1:-1]
    out: dict[str, object] = {}
    for item in _split_flow_items(inner):
        if not item.strip():
            continue
        key, sep, val = item.partition(":")
        if not sep:
            continue
        key = key.strip().strip('"').strip("'")
        out[key] = _parse_flow_scalar(val)[1]
    return out


def parse_frontmatter(text: str) -> dict[str, object]:
    """Parse the subset of frontmatter the audit depends on.

    Supports top-level ``key: value`` scalars, one-line flow mappings
    (``metadata: { "k": v }``), indent-based nested mappings, and block
    scalars with chomping (``|``/``>`` with ``-``/``+`` indicators).
    """
    lines, _ = split_frontmatter(text)
    out: dict[str, object] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent != 0:
            i += 1
            continue
        key, sep, rest = line.partition(":")
        if not sep:
            i += 1
            continue
        key = key.strip()
        rest = rest.strip()
        if rest in ("|", "|-", "|+", ">", ">-", ">+"):
            block: list[str] = []
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    block.append("")
                    i += 1
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                if nxt_indent == 0:
                    break
                block.append(nxt)
                i += 1
            while block and block[-1].strip() == "":
                block.pop()
            if block:
                common = min(len(b) - len(b.lstrip(" ")) for b in block if b.strip())
                content = [b[common:] if len(b) >= common else b.lstrip() for b in block]
            else:
                content = []
            folded = rest[0] == ">"
            body = " ".join(part.strip() for part in content) if folded else "\n".join(content)
            if not rest.endswith("-") and body:
                body += "\n"  # clip chomping keeps exactly one trailing newline
            out[key] = body
            continue
        if rest.startswith("{") and rest.endswith("}"):
            out[key] = _parse_flow_mapping(rest)
            i += 1
            continue
        if rest == "":
            nested: dict[str, object] = {}
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    i += 1
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                if nxt_indent == 0:
                    break
                if nxt.lstrip().startswith("- "):
                    i += 1
                    continue
                nkey, nsep, nrest = nxt.lstrip().partition(":")
                if nsep:
                    _, tval = _parse_flow_scalar(nrest)
                    nested[nkey.strip()] = tval
                i += 1
            out[key] = nested
            continue
        _, typed = _parse_flow_scalar(rest)
        out[key] = typed
        i += 1
    return out


# --------------------------------------------------------------------------- #
# Audit recomputation
# --------------------------------------------------------------------------- #


def normalize_canonical_name(name: str) -> str:
    lowered = name.lower().replace("_", "-").replace(" ", "-")
    cleaned = re.sub(r"[^a-z0-9-]+", "", lowered)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned


def discover_skill_files(source_dir: Path) -> list[str]:
    root = source_dir / "skills"
    found = [
        p.relative_to(source_dir).as_posix()
        for p in root.rglob("SKILL.md")
        if p.is_file()
    ]
    return sorted(found)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def audit_one_skill(source_dir: Path, relpath: str) -> dict[str, object]:
    data = (source_dir / relpath).read_bytes()
    text = data.decode("utf-8")
    meta = parse_frontmatter(text)
    name = meta.get("name")
    name = name if isinstance(name, str) else ""
    description = meta.get("description")
    description = description if isinstance(description, str) else ""
    metadata = meta.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    non_string = {
        key: type_name
        for key, value in metadata.items()
        if (type_name := _scalar_type_name(value)) != "str"
    }

    parts = PurePosixPath(relpath).parts  # ("skills", <dir>, ..., "SKILL.md")
    directory = parts[1]
    # Canonical issue order matches the recorded audit:
    # invalid_name, name_directory_mismatch, invalid_description,
    # metadata_values_not_all_strings.
    issues: list[str] = []
    if not NAME_RE.fullmatch(name) or len(name) > NAME_MAX:
        issues.append("invalid_name")
    if name != directory:
        issues.append("name_directory_mismatch")
    if len(description) > DESCRIPTION_MAX:
        issues.append("invalid_description")
    if non_string:
        issues.append("metadata_values_not_all_strings")

    license_value = meta.get("license")
    return {
        "source_path": relpath,
        "sha256": sha256_bytes(data),
        "original_name": name,
        "canonical_name_proposed": normalize_canonical_name(name),
        "description_characters": len(description),
        "issues": issues,
        "non_string_metadata": dict(sorted(non_string.items())),
        "license_frontmatter": license_value if isinstance(license_value, str) else None,
        "nested": len(parts) > 3,
    }


def _scalar_type_name(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, list):
        return "list"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return "str"


RECOMPUTED_FIELDS = (
    "sha256",
    "original_name",
    "canonical_name_proposed",
    "description_characters",
    "issues",
    "non_string_metadata",
    "license_frontmatter",
)
PRESENCE_ONLY_FIELDS = ("platform_markers_heuristic", "additional_tool_tokens_heuristic")
CONSTANT_FIELDS = {
    "technical_status": EXPECTED_TECHNICAL_STATUS,
    "distribution_status": EXPECTED_DISTRIBUTION_STATUS,
    "full_prompt_included": EXPECTED_FULL_PROMPT_INCLUDED,
}


def compare_with_audit(source_dir: Path, audit_doc: dict) -> dict[str, object]:
    discovered = discover_skill_files(source_dir)
    recorded = {entry["source_path"]: entry for entry in audit_doc["skills"]}
    mismatches: list[dict[str, object]] = []
    recomputed: dict[str, dict[str, object]] = {}

    for relpath in discovered:
        record = audit_one_skill(source_dir, relpath)
        recomputed[relpath] = record
        entry = recorded.get(relpath)
        if entry is None:
            mismatches.append({"path": relpath, "field": "<presence>", "detail": "not in recorded audit"})
            continue
        for field_name in RECOMPUTED_FIELDS:
            if record[field_name] != entry.get(field_name):
                mismatches.append(
                    {
                        "path": relpath,
                        "field": field_name,
                        "recomputed": record[field_name],
                        "recorded": entry.get(field_name),
                    }
                )
        for field_name in PRESENCE_ONLY_FIELDS:
            if not isinstance(entry.get(field_name), list):
                mismatches.append(
                    {"path": relpath, "field": field_name, "detail": "missing or not a list"}
                )
        for field_name, expected in CONSTANT_FIELDS.items():
            if entry.get(field_name) != expected:
                mismatches.append(
                    {
                        "path": relpath,
                        "field": field_name,
                        "detail": f"expected constant {expected!r}, got {entry.get(field_name)!r}",
                    }
                )

    canonical_names = [r["canonical_name_proposed"] for r in recomputed.values()]
    collisions = sorted({n for n in canonical_names if canonical_names.count(n) > 1})

    issue_counts: dict[str, int] = {}
    for record in recomputed.values():
        for issue in record["issues"]:  # type: ignore[union-attr]
            issue_counts[issue] = issue_counts.get(issue, 0) + 1  # type: ignore[index]
    if issue_counts != audit_doc.get("issue_counts"):
        mismatches.append(
            {
                "path": "<audit.issue_counts>",
                "field": "issue_counts",
                "recomputed": issue_counts,
                "recorded": audit_doc.get("issue_counts"),
            }
        )
    if len(discovered) != audit_doc.get("count"):
        mismatches.append(
            {
                "path": "<audit.count>",
                "field": "count",
                "recomputed": len(discovered),
                "recorded": audit_doc.get("count"),
            }
        )

    return {
        "discovered": len(discovered),
        "recorded": len(recorded),
        "missing_from_disk": sorted(set(recorded) - set(discovered)),
        "collisions": collisions,
        "issue_counts_recomputed": issue_counts,
        "mismatches": mismatches,
        "ok": not mismatches and set(discovered) == set(recorded) and not collisions,
    }


def verify_source_manifest(source_dir: Path, manifest: dict) -> dict[str, object]:
    mismatches: list[dict[str, object]] = []
    for source in manifest["sources"]:
        path = source_dir / source["path"]
        if not path.is_file():
            mismatches.append({"path": source["path"], "field": "<presence>", "detail": "missing on disk"})
            continue
        data = path.read_bytes()
        actual = {
            "sha256": sha256_bytes(data),
            "bytes": len(data),
            "lines": len(data.decode("utf-8", errors="replace").splitlines()),
        }
        for field_name in MANIFEST_FIELDS:
            if actual[field_name] != source[field_name]:
                mismatches.append(
                    {
                        "path": source["path"],
                        "field": field_name,
                        "recomputed": actual[field_name],
                        "recorded": source[field_name],
                    }
                )
    return {
        "entries": len(manifest["sources"]),
        "mismatches": mismatches,
        "ok": not mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("01/private-vendor/muse-sdk"))
    parser.add_argument("--audit-dir", type=Path, default=Path("tests/fixtures"))
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    skills_path = args.audit_dir / "skills.json"
    manifest_path = args.audit_dir / "selected-source-manifest.json"
    if not skills_path.is_file() or not args.source_dir.is_dir():
        print("reproduce_audit: source-dir or audit files missing", file=sys.stderr)
        return 2

    audit_doc = json.loads(skills_path.read_text(encoding="utf-8"))
    report = {"skills_audit": compare_with_audit(args.source_dir, audit_doc)}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report["source_manifest"] = verify_source_manifest(args.source_dir, manifest)

    ok = report["skills_audit"]["ok"] and report.get("source_manifest", {}).get("ok", False)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        sa = report["skills_audit"]
        print(
            f"skills: discovered {sa['discovered']} / recorded {sa['recorded']}; "
            f"issue_counts {'match' if sa['issue_counts_recomputed'] == audit_doc['issue_counts'] else 'MISMATCH'}"
        )
        for name, sub in report.items():
            label = "OK" if sub["ok"] else "FAILED"  # type: ignore[index]
            print(f"{name}: {label}")
            for mismatch in sub["mismatches"]:  # type: ignore[index]
                print(f"  mismatch: {mismatch}")
        if sa["collisions"]:
            print(f"canonical name collisions: {sa['collisions']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
