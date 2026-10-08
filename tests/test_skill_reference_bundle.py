"""Separate Skill references are complete data, not default runtime inputs."""

from __future__ import annotations

import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

from license_gate import load_reference_hashes, reference_member_errors, find_forbidden_hash_matches, reference_tree_hash
from package_gate import check_archive
from proactive_sdk import ProactiveAgent
from proactive_sdk.skills import LegacySkillImporter, audit_consistency


class ReferenceBundleTests(unittest.TestCase):
    def test_all_88_skills_and_assets_are_exact_and_audit_consistent(self):
        expected = load_reference_hashes(REPO)
        root = REPO / "src" / "proactive_sdk" / "_deployment_reference"
        hashes = {
            "proactive_sdk/_deployment_reference/" + p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()
        }
        self.assertEqual(len(expected), 377)
        self.assertEqual(len(hashes), 380)
        self.assertEqual(reference_member_errors(hashes, expected), [])
        importer = LegacySkillImporter(root=root)
        report = importer.report(importer.scan())
        self.assertEqual(report["count"], 88)
        audit = json.loads((REPO / "tests" / "fixtures" / "skills.json").read_text())
        self.assertTrue(audit_consistency(report, audit)["match"])

    def test_private_hash_is_allowed_only_at_registered_path_with_exact_content(self):
        path, digest = next(iter(load_reference_hashes(REPO).items()))
        self.assertEqual(find_forbidden_hash_matches({path: digest}, {digest}, {path: digest}), [])
        self.assertEqual(find_forbidden_hash_matches({"elsewhere.py": digest}, {digest}, {path: digest}), ["elsewhere.py"])
        self.assertTrue(reference_member_errors({path: "0" * 64}, {path: digest}))

    def test_one_tree_checksum_still_rejects_modified_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = repo / "src" / "proactive_sdk" / "_deployment_reference"
            skill = root / "skills" / "example" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_bytes(b"original")
            path = "proactive_sdk/_deployment_reference/skills/example/SKILL.md"
            hashes = {path: hashlib.sha256(b"original").hexdigest()}
            (root / "manifest.json").write_text(json.dumps({
                "purpose": "deployment_reference", "auto_load": False, "auto_execute": False,
                "file_count": 1, "skill_count": 1, "content_sha256": reference_tree_hash(hashes),
            }))
            self.assertEqual(load_reference_hashes(repo), hashes)
            skill.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                load_reference_hashes(repo)

    def test_sbom_has_one_artifact_hash_and_no_duplicate_member_hash_table(self):
        from gen_sbom import generate_sbom
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "example.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("example.dist-info/METADATA", "Name: example\nVersion: 1.0\n")
                archive.writestr("example/data.txt", b"x")
            bom = generate_sbom(wheel)
            self.assertEqual(len(bom["metadata"]["component"]["hashes"]), 1)
            self.assertFalse(any(p["name"] == "pas:member-sha256" for p in bom["metadata"]["properties"]))

    def test_package_gate_checks_reference_hashes_completeness_and_sdist_private_paths(self):
        expected = load_reference_hashes(REPO)
        path = "proactive_sdk/_deployment_reference/skills/gmail/SKILL.md"
        payload = (REPO / "src" / path).read_bytes()
        digest = expected[path]
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(path, payload)
                for name in ("README.md", "NOTICE.md", "manifest.json"):
                    member = "proactive_sdk/_deployment_reference/" + name
                    archive.writestr(member, (REPO / "src" / member).read_bytes())
            self.assertEqual(check_archive(wheel, {digest}, {path: digest}), [])
            self.assertTrue(check_archive(wheel, {digest}))  # runtime wheels must exclude Skills
            self.assertTrue(check_archive(wheel, {digest}, expected))  # missing assets
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(path, payload + b"\nchanged")
            self.assertTrue(check_archive(wheel, {digest}, {path: digest}))
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("elsewhere.md", payload)
            self.assertTrue(check_archive(wheel, {digest}, {path: digest}))
            sdist = Path(tmp) / "fixture.tar.gz"
            import io
            with tarfile.open(sdist, "w:gz") as archive:
                info = tarfile.TarInfo("fixture/01/private-file.txt")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            self.assertTrue(check_archive(sdist, set()))


class PassiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_agent_never_imports_or_executes_reference_skills(self):
        class NeverCalledModel:
            async def generate(self, request):
                raise AssertionError("a reference bundle must not cause model calls")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "proactive_sdk.facade.LegacySkillImporter", side_effect=AssertionError("automatic Skill import")
        ):
            async with ProactiveAgent(state_dir=tmp, model=NeverCalledModel()) as agent:
                self.assertEqual(agent.store.list_skill_installs(), ())
                report = await agent.tick(run_hooks=False)
                self.assertEqual(report["runs"], [])
                self.assertEqual(agent.store.list_skill_installs(), ())
        self.assertFalse(any(name.startswith("proactive_sdk._deployment_reference") for name in sys.modules))


if __name__ == "__main__":
    unittest.main()
