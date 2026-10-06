from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.pathsafe import (
    PathSafetyError,
    ensure_within,
    safe_join,
    validate_zip_member,
)


class SafeJoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "base"
        self.base.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_simple_join(self):
        result = safe_join(self.base, "a/b.txt")
        self.assertTrue(result.is_relative_to(self.base.resolve()))
        self.assertEqual(result.name, "b.txt")

    def test_parent_escape_rejected(self):
        for bad in ("../x", "a/../../x", "a/..", "..\\..\\x"):
            with self.assertRaises(PathSafetyError, msg=bad):
                safe_join(self.base, bad)

    def test_absolute_component_rejected(self):
        for bad in ("/etc/passwd", "C:/Windows", "C:\\x"):
            with self.assertRaises(PathSafetyError, msg=bad):
                safe_join(self.base, bad)

    def test_nul_rejected(self):
        with self.assertRaises(PathSafetyError):
            safe_join(self.base, "a\x00b")

    def test_dotdot_filename_segment_is_allowed(self):
        # "..hidden" or "a..b" are legal filenames; only exact ".." escapes.
        result = safe_join(self.base, "a..b", "..hidden")
        self.assertTrue(result.is_relative_to(self.base.resolve()))

    def test_symlink_escape_rejected(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret")
        link = self.base / "link"
        link.symlink_to(outside)
        with self.assertRaises(PathSafetyError):
            safe_join(self.base, "link")
        # indirection through a directory symlink
        outside_dir = Path(self.tmp.name) / "out_dir"
        outside_dir.mkdir()
        dir_link = self.base / "dlink"
        dir_link.symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaises(PathSafetyError):
            safe_join(self.base, "dlink", "file.txt")

    def test_symlink_within_base_allowed(self):
        target = self.base / "real.txt"
        target.write_text("ok")
        link = self.base / "alias.txt"
        link.symlink_to(target)
        result = safe_join(self.base, "alias.txt")
        self.assertEqual(result.resolve(), target.resolve())


class EnsureWithinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_inside_ok(self):
        candidate = self.base / "sub" / "f.txt"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("x")
        self.assertEqual(ensure_within(self.base, candidate), candidate.resolve())

    def test_base_itself_ok(self):
        self.assertEqual(ensure_within(self.base, self.base), self.base)

    def test_escape_rejected(self):
        with self.assertRaises(PathSafetyError):
            ensure_within(self.base, self.base.parent / "elsewhere.txt")


class ZipMemberTests(unittest.TestCase):
    def test_valid_names_pass_and_normalize(self):
        self.assertEqual(validate_zip_member("skills/gmail/SKILL.md"), "skills/gmail/SKILL.md")
        self.assertEqual(validate_zip_member("a\\b\\c.txt"), "a/b/c.txt")
        self.assertEqual(validate_zip_member("..hidden/x"), "..hidden/x")

    def test_classic_zip_slip_rejected(self):
        for bad in (
            "../evil.txt",
            "../../etc/passwd",
            "/etc/passwd",
            "\\windows\\system32\\x",
            "a/../../b",
            "a/..\\b",
            "C:/evil",
            "foo/\x00bar",
            "",
            "   ",
        ):
            with self.assertRaises(PathSafetyError, msg=bad):
                validate_zip_member(bad)

    def test_exact_dotdot_segment_only(self):
        # "a..b" is a legal segment; ".." alone is not.
        self.assertEqual(validate_zip_member("a..b/c"), "a..b/c")
        with self.assertRaises(PathSafetyError):
            validate_zip_member("a/../b")


if __name__ == "__main__":
    unittest.main()
