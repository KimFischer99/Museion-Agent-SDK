from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reproduce_audit = _load_tool("reproduce_audit")
license_gate = _load_tool("license_gate")


def build_fixture_tree(root: Path) -> None:
    """Tiny stand-in for the vendor skills tree: one clean skill, one nested,
    one with underscore name and boolean metadata."""
    clean = root / "skills" / "clean-skill"
    clean.mkdir(parents=True)
    (clean / "SKILL.md").write_text(
        "---\n"
        'name: clean-skill\n'
        'description: "A clean skill."\n'
        "metadata:\n"
        "  includeInPrompt: true\n"
        "---\n"
        "Body text.\n",
        encoding="utf-8",
    )
    nested = root / "skills" / "artifacts" / "document"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\n"
        'name: artifact_document\n'
        'description: "Nested artifact skill."\n'
        "metadata:\n"
        "  includeInPrompt: false\n"
        "---\n",
        encoding="utf-8",
    )
    underscore = root / "skills" / "my-thing"
    underscore.mkdir(parents=True)
    (underscore / "SKILL.md").write_text(
        "---\n"
        "name: my_thing\n"
        "description: Underlined.\n"
        "license: none-here\n"
        "---\n",
        encoding="utf-8",
    )
    flow = root / "skills" / "flow-style"
    flow.mkdir(parents=True)
    (flow / "SKILL.md").write_text(
        "---\n"
        'name: "flow-style"\n'
        "description: >-\n"
        "  Folded strip description spanning\n"
        "  two lines.\n"
        'metadata: { "includeInPrompt": true, "devices": ["a", "b"] }\n'
        "---\n",
        encoding="utf-8",
    )


class ReproduceAuditFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self.tmp.name) / "corpus"
        self.tree.mkdir()
        build_fixture_tree(self.tree)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovery_finds_nested_entries(self):
        found = reproduce_audit.discover_skill_files(self.tree)
        self.assertEqual(
            found,
            [
                "skills/artifacts/document/SKILL.md",
                "skills/clean-skill/SKILL.md",
                "skills/flow-style/SKILL.md",
                "skills/my-thing/SKILL.md",
            ],
        )

    def test_flow_style_metadata_and_folded_description(self):
        record = reproduce_audit.audit_one_skill(self.tree, "skills/flow-style/SKILL.md")
        # the real corpus uses flow mappings: metadata: { "k": v, ... }
        self.assertEqual(
            record["non_string_metadata"],
            {"includeInPrompt": "bool", "devices": "list"},
        )
        # folded (>-) block scalar: lines joined, strip chomping, no trailing \n
        self.assertEqual(record["description_characters"], len("Folded strip description spanning two lines."))
        self.assertEqual(record["original_name"], "flow-style")
        self.assertEqual(record["issues"], ["metadata_values_not_all_strings"])

    def test_record_flags_and_normalization(self):
        record = reproduce_audit.audit_one_skill(self.tree, "skills/my-thing/SKILL.md")
        self.assertEqual(record["original_name"], "my_thing")
        self.assertEqual(record["canonical_name_proposed"], "my-thing")
        self.assertIn("invalid_name", record["issues"])
        self.assertIn("name_directory_mismatch", record["issues"])
        self.assertNotIn("metadata_values_not_all_strings", record["issues"])

        nested = reproduce_audit.audit_one_skill(self.tree, "skills/artifacts/document/SKILL.md")
        self.assertTrue(nested["nested"])
        self.assertIn("invalid_name", nested["issues"])
        self.assertIn("name_directory_mismatch", nested["issues"])
        self.assertIn("metadata_values_not_all_strings", nested["issues"])
        self.assertEqual(nested["non_string_metadata"], {"includeInPrompt": "bool"})

        clean = reproduce_audit.audit_one_skill(self.tree, "skills/clean-skill/SKILL.md")
        # boolean includeInPrompt alone still triggers the metadata issue
        # (all 88 recorded corpus skills carry it).
        self.assertEqual(clean["issues"], ["metadata_values_not_all_strings"])
        self.assertEqual(clean["non_string_metadata"], {"includeInPrompt": "bool"})
        self.assertEqual(clean["canonical_name_proposed"], "clean-skill")
        self.assertFalse(clean["nested"])

    def test_description_length_and_block_scalars(self):
        long_desc = "x" * 1100
        root = self.tree / "skills" / "chatty"
        root.mkdir()
        (root / "SKILL.md").write_text(
            f"---\nname: chatty\ndescription: \"{long_desc}\"\n---\n",
            encoding="utf-8",
        )
        record = reproduce_audit.audit_one_skill(self.tree, "skills/chatty/SKILL.md")
        self.assertEqual(record["description_characters"], 1100)
        self.assertIn("invalid_description", record["issues"])

    def test_compare_ok_against_matching_audit(self):
        records = [
            reproduce_audit.audit_one_skill(self.tree, path)
            for path in reproduce_audit.discover_skill_files(self.tree)
        ]
        issue_counts: dict[str, int] = {}
        for record in records:
            for issue in record["issues"]:
                issue_counts[issue] = issue_counts.get(issue, 0) + 1
        audit_doc = {
            "count": len(records),
            "issue_counts": issue_counts,
            "skills": [
                {
                    **record,
                    "platform_markers_heuristic": [],
                    "additional_tool_tokens_heuristic": [],
                    "technical_status": "unverified_requires_capability_audit",
                    "distribution_status": "no_grant_identified_in_snapshot",
                    "full_prompt_included": False,
                }
                for record in records
            ],
        }
        report = reproduce_audit.compare_with_audit(self.tree, audit_doc)
        self.assertTrue(report["ok"], report["mismatches"])

    def test_compare_detects_tampering(self):
        records = [
            reproduce_audit.audit_one_skill(self.tree, path)
            for path in reproduce_audit.discover_skill_files(self.tree)
        ]
        audit_doc = {"count": len(records), "issue_counts": {}, "skills": records}
        audit_doc["skills"][0]["sha256"] = "0" * 64  # tampered hash
        audit_doc["count"] = 99
        report = reproduce_audit.compare_with_audit(self.tree, audit_doc)
        self.assertFalse(report["ok"])
        fields = {m["field"] for m in report["mismatches"]}
        self.assertIn("sha256", fields)
        self.assertIn("count", fields)

    def test_manifest_verification(self):
        records = reproduce_audit.discover_skill_files(self.tree)
        sources = []
        for relpath in records[:1]:
            data = (self.tree / relpath).read_bytes()
            import hashlib

            sources.append(
                {
                    "path": relpath,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "lines": len(data.decode().splitlines()),
                }
            )
        report = reproduce_audit.verify_source_manifest(self.tree, {"sources": sources})
        self.assertTrue(report["ok"], report["mismatches"])
        bad = dict(sources[0], bytes=sources[0]["bytes"] + 1)
        report = reproduce_audit.verify_source_manifest(self.tree, {"sources": [bad]})
        self.assertFalse(report["ok"])


class LicenseGateUnitTests(unittest.TestCase):
    def test_banned_paths(self):
        tracked = [
            "README.md",
            "AGENTS.md",
            "AUDIT_AND_REUSE.md",
            "SPEC.md",
            "VALIDATION.md",
            "audit/helper-tests.log",
            "reuse/extract_original.py",
            "01/private-vendor/muse-sdk/skills/gmail/SKILL.md",
            "01/muse-refer/Ling.md",
            "private-vendor/muse-sdk/skills/gmail/SKILL.md",
            "muse-sdk/README.md",
            "muse-reuse/hatch_hook_runtime.sh",
            "__MACOSX/._x",
            "docs/.DS_Store",
            "src/proactive_sdk/contracts.py",
        ]
        violations = license_gate.banned_tracked_paths(tracked)
        self.assertEqual(
            violations,
            [
                "AGENTS.md",
                "AUDIT_AND_REUSE.md",
                "SPEC.md",
                "VALIDATION.md",
                "audit/helper-tests.log",
                "reuse/extract_original.py",
                "01/private-vendor/muse-sdk/skills/gmail/SKILL.md",
                "01/muse-refer/Ling.md",
                "private-vendor/muse-sdk/skills/gmail/SKILL.md",
                "muse-sdk/README.md",
                "muse-reuse/hatch_hook_runtime.sh",
                "__MACOSX/._x",
                "docs/.DS_Store",
            ],
        )

    def test_hash_matching(self):
        tracked = {"a": "1" * 64, "b": "2" * 64}
        forbidden = {"2" * 64, license_gate.HELPER_SHA256}
        self.assertEqual(license_gate.find_forbidden_hash_matches(tracked, forbidden), ["b"])
        self.assertEqual(license_gate.find_forbidden_hash_matches(tracked, {"3" * 64}), [])

    def test_gate_status_parsing(self):
        passing = "<!-- license-gate\nstatus: pass\n-->\n# doc"
        blocked = "<!-- license-gate\nstatus: blocked\n-->"
        self.assertEqual(license_gate.read_gate_status(passing), "pass")
        self.assertEqual(license_gate.read_gate_status(blocked), "blocked")
        self.assertIsNone(license_gate.read_gate_status("no marker"))
        self.assertIsNone(license_gate.read_gate_status("<!-- license-gate\nno status\n-->"))


if __name__ == "__main__":
    unittest.main()
