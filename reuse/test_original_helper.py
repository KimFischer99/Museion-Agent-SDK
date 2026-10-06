"""Controlled functional tests of the known user-supplied helper ONLY.
Usage: python reuse/test_original_helper.py /private/path/hatch_hook_runtime.sh
No archived machine-health scripts are run. Requires Bash and jq.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

EXPECTED = "c87af221181a1559e3adcb0cdd601f5be5d4bd91eec2fe0597c09dc27558e741"
if len(sys.argv) < 2:
    raise SystemExit(__doc__)
HELPER = Path(sys.argv.pop(1)).resolve()
if hashlib.sha256(HELPER.read_bytes()).hexdigest() != EXPECTED:
    raise SystemExit("Unexpected helper; refuse to run unreviewed code")


class OriginalHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
                    "HATCH_HOOK_STATE_DIR": self.tmp.name, "HATCH_HOOK_ID": "test",
                    "HATCH_HOOK_INVOCATION_ID": "test-invocation"}

    def tearDown(self): self.tmp.cleanup()

    def run_shell(self, body):
        return subprocess.run(["bash", "-c", 'set -e; source "$1"; ' + body, "test", str(HELPER)],
                              cwd=self.tmp.name, env=self.env, capture_output=True, text=True, timeout=5)

    def test_silent(self):
        p = self.run_shell('silent "unchanged"')
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout.split(":", 1)[1])["decision"], "silent")

    def test_wake_and_disable(self):
        p = self.run_shell('disable_after_run; wake "changed" \'{"version":2}\'')
        obj = json.loads(p.stdout.split(":", 1)[1])
        self.assertEqual(obj["payload"], {"version": 2})
        self.assertTrue(obj["disable_after_run"])

    def test_log_to_stderr(self):
        p = self.run_shell('log "hello" \'{"n":1}\'')
        self.assertEqual(p.stdout, "")
        self.assertEqual(json.loads(p.stderr.split(":", 1)[1])["n"], 1)

    def test_initial_state(self):
        self.assertEqual(json.loads(self.run_shell("hook_state_get").stdout), {})

    def test_state_roundtrip(self):
        p = self.run_shell('hook_state_set \'{"n":1}\'; hook_state_get')
        self.assertEqual(json.loads(p.stdout), {"n": 1})

    def test_dryrun_skips_helper_state_write(self):
        self.env["HATCH_HOOK_DRY_RUN"] = "1"
        p = self.run_shell('hook_state_set \'{"n":1}\'; hook_state_get')
        self.assertEqual(json.loads(p.stdout), {})
        self.assertFalse((Path(self.tmp.name) / "test.json").exists())

    def test_invalid_state_type(self):
        self.assertNotEqual(self.run_shell("hook_state_set '[]'").returncode, 0)

    def test_invalid_payload(self):
        self.assertNotEqual(self.run_shell("wake x 'not-json'").returncode, 0)


if __name__ == "__main__": unittest.main()
