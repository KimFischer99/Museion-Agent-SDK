"""P6 legacy Skill importer tests (SPEC §11).

Cover the parsing dialect (inline JSON, block maps, block lists,
continuations), canonical ids/aliases/collisions, dependency closure
(including nested-artifact shared assets), sidecar shape, store
recording, and — when the private-vendor Muse snapshot is present —
exact consistency with audit/skills.json (88 entries, issue counts,
hashes).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.skills import (
    LegacySkillImporter,
    audit_consistency,
    parse_legacy_frontmatter,
)

REPO = Path(__file__).resolve().parents[1]
VENDOR = REPO / "private-vendor" / "muse-sdk"
AUDIT = json.loads((REPO / "audit" / "skills.json").read_text(encoding="utf-8"))


class FrontmatterTests(unittest.TestCase):
    def test_inline_json_metadata(self):
        fields, issues = parse_legacy_frontmatter(
            '---\nname: "gmail"\ndescription: "Mail"\nmetadata: { "includeInPrompt": true }\n---\n'
        )
        self.assertEqual(fields["metadata"], {"includeInPrompt": True})
        self.assertEqual(issues, [])

    def test_block_mapping_and_block_list(self):
        fields, issues = parse_legacy_frontmatter(
            "---\nname: x\nmetadata:\n  includeInPrompt: true\nallowed-tools:\n  - \"exec\"\n  - \"read\"\n---\n"
        )
        self.assertEqual(fields["metadata"], {"includeInPrompt": True})
        self.assertEqual(fields["allowed-tools"], ["exec", "read"])
        self.assertEqual(issues, [])

    def test_continuation_line_joins_value(self):
        fields, issues = parse_legacy_frontmatter(
            '---\nname: x\ndescription: "first"\n  second\n---\n'
        )
        self.assertEqual(fields["description"], "first second")
        self.assertEqual(issues, [])

    def test_missing_frontmatter_recorded(self):
        fields, issues = parse_legacy_frontmatter("no frontmatter here")
        self.assertEqual(fields, {})
        self.assertEqual(issues, ["missing_frontmatter"])


def _make_tree() -> Path:
    tmp = Path(tempfile.mkdtemp())
    skills = tmp / "skills"
    # A gws-style skill with underscore name and block metadata.
    gmail = skills / "gmail"
    gmail.mkdir(parents=True)
    (gmail / "SKILL.md").write_text(
        '---\nname: "gmail"\ndescription: "Work with mail."\n'
        'metadata: { "includeInPrompt": true }\n---\n'
        "Use `hatch_gws_cli gmail status` and `hatch_gws_cli gmail +read`.\n",
        encoding="utf-8",
    )
    # A calendar skill with a manifest and directory/name mismatch.
    cal = skills / "google-calendar"
    cal.mkdir(parents=True)
    (cal / "SKILL.md").write_text(
        '---\nname: "google_calendar"\ndescription: "Calendar ops."\n'
        'metadata: { "includeInPrompt": false }\n---\nbody\n',
        encoding="utf-8",
    )
    (cal / "manifest.yaml").write_text("version: 1\nconnector: google_calendar\n", encoding="utf-8")
    # Nested artifact sharing references with its layer.
    doc = skills / "artifacts" / "document"
    doc.mkdir(parents=True)
    (doc / "SKILL.md").write_text(
        "---\nname: artifact_document\nmetadata:\n  includeInPrompt: false\n"
        'description: "Word docs."\n---\nSee references/shared.md for layout rules.\n',
        encoding="utf-8",
    )
    shared = skills / "artifacts" / "references"
    shared.mkdir(parents=True)
    (shared / "shared.md").write_text("# shared\n", encoding="utf-8")
    # A second skill whose name canonicalizes onto an existing one.
    clash = skills / "gmail-2"
    clash.mkdir(parents=True)
    (clash / "SKILL.md").write_text(
        '---\nname: "gmail"\ndescription: "Clash"\nmetadata: { "includeInPrompt": false }\n---\n',
        encoding="utf-8",
    )
    # An escaping symlink: must be ignored entirely.
    escape = skills / "escape"
    escape.mkdir()
    try:
        (escape / "SKILL.md").symlink_to(tmp / "outside.md")
    except OSError:
        pass
    (tmp / "outside.md").write_text("outside", encoding="utf-8")
    return tmp


class ImporterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = _make_tree()
        cls.importer = LegacySkillImporter(root=cls.root)
        cls.skills = cls.importer.scan()
        cls.by_canonical = {s.canonical_name: s for s in cls.skills}

    def test_discovers_all_entries_including_escaping_symlink_ignored(self):
        self.assertEqual({s.canonical_name.split("-")[0] for s in self.skills}, {"gmail", "google", "artifact", "escape"} - {"escape"})
        self.assertNotIn("escape", {s.rel_path.split("/")[0] for s in self.skills})

    def test_canonical_and_aliases(self):
        cal = self.by_canonical["google-calendar"]
        self.assertEqual(cal.original_name, "google_calendar")
        self.assertEqual(cal.canonical_name, "google-calendar")
        self.assertIn("google_calendar", cal.aliases)

    def test_collision_gets_distinct_canonical_ids(self):
        canonicals = [s.canonical_name for s in self.skills]
        self.assertEqual(len(canonicals), len(set(canonicals)))
        gmail_like = sorted(s.canonical_name for s in self.skills if s.original_name == "gmail")
        self.assertEqual(gmail_like, ["gmail", "gmail-2"])

    def test_requirements_and_compatibility_mapped(self):
        gmail = self.by_canonical["gmail"]
        self.assertEqual(gmail.capability_requirements, ["gmail.read"])
        self.assertEqual(gmail.tool_requirements, ["hatch_gws_cli"])
        self.assertEqual(gmail.compatibility_status, "adapter_required")
        # Shared artifacts layer: unmapped → default.
        doc = self.by_canonical["artifact-document"]
        self.assertEqual(doc.compatibility_status, "adapter_required")
        self.assertEqual(doc.capability_requirements, [])

    def test_dependency_closure_includes_shared_assets(self):
        doc = self.by_canonical["artifact-document"]
        self.assertIn("skills/artifacts/references/shared.md", doc.dependency_files)
        self.assertIn("skills/artifacts/document/SKILL.md", doc.dependency_files)

    def test_sidecar_shape_and_distribution_independence(self):
        gmail = self.by_canonical["gmail"]
        sidecar = gmail.sidecar()
        self.assertEqual(sidecar["schema_version"], "1.0")
        self.assertEqual(sidecar["source"]["format"], "muse-legacy")
        self.assertEqual(sidecar["source"]["original_path"], "skills/gmail/SKILL.md")
        self.assertEqual(sidecar["canonical_name"], "gmail")
        self.assertEqual(sidecar["requirements"]["grants"], ["selected_mail_account"])
        self.assertEqual(sidecar["compatibility"]["path_strategy"], "isolated_virtual_mount")
        self.assertEqual(sidecar["distribution"]["status"], "permission_unverified")
        self.assertIn("includeInPrompt", sidecar["legacy"]["metadata_normalized"])

    def test_report_counts_issues(self):
        report = self.importer.report(self.skills)
        self.assertEqual(report["count"], len(self.skills))
        self.assertEqual(report["issue_counts"]["metadata_values_not_all_strings"], 4)
        self.assertIn("name_directory_mismatch", report["issue_counts"])


class StoreSkillInstallTests(unittest.TestCase):
    def _store(self):
        from proactive_sdk.store import Store

        return Store(":memory:", profile="skills-test", owner_destination="local-inbox:owner")

    def test_record_advance_and_conflict(self):
        store = self._store()
        install_id = store.record_skill_install(
            canonical_name="google-calendar",
            source_hash="sha256:aaa",
            sidecar={"canonical_name": "google-calendar"},
            technical_status="parsed",
            distribution_status="permission_unverified",
            now_ms=10,
        )
        again = store.record_skill_install(
            canonical_name="google-calendar",
            source_hash="sha256:aaa",
            sidecar={"canonical_name": "google-calendar"},
            technical_status="contract_tested",
            distribution_status="permission_unverified",
            now_ms=20,
        )
        self.assertEqual(install_id, again)
        record = store.get_skill_install("google-calendar")
        assert record is not None
        self.assertEqual(record["technical_status"], "contract_tested")
        with self.assertRaises(Exception):
            store.record_skill_install(
                canonical_name="google-calendar",
                source_hash="sha256:different",
                sidecar={},
                technical_status="parsed",
                distribution_status="permission_unverified",
                now_ms=30,
            )
        self.assertEqual(len(store.list_skill_installs()), 1)
        store.close()


@unittest.skipUnless(
    VENDOR.is_dir(), "private-vendor Muse snapshot not present; audit-consistency run is skipped, not faked"
)
class AuditConsistencyTests(unittest.TestCase):
    def test_full_corpus_matches_audit_exactly(self):
        importer = LegacySkillImporter(root=VENDOR)
        skills = importer.scan()
        report = importer.report(skills)
        self.assertEqual(report["count"], 88)
        self.assertEqual(
            report["issue_counts"],
            {
                "invalid_name": 43,
                "name_directory_mismatch": 41,
                "metadata_values_not_all_strings": 88,
                "invalid_description": 1,
            },
        )
        consistency = audit_consistency(report, AUDIT)
        self.assertTrue(
            consistency["match"],
            json.dumps(consistency["mismatches"][:8], ensure_ascii=False),
        )
        # The mapped loop skills expose typed requirements (§11.5).
        by_name = {s.canonical_name: s for s in skills}
        for name, capability in (("gmail", "gmail.read"), ("google-calendar", "calendar.read")):
            self.assertEqual(by_name[name].capability_requirements, [capability])
            self.assertEqual(by_name[name].tool_requirements, ["hatch_gws_cli"])
            self.assertEqual(by_name[name].technical_status, "parsed")


if __name__ == "__main__":
    unittest.main()
