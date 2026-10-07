"""SPEC §22.1 item 10: every subcommand fails with a message, never a traceback.

This file exists because of a real defect found during v0.1.2 acceptance:
`pas config print` crashed with `AttributeError: 'Namespace' object has no
attribute 'config'`. The cause was structural, not local —
`_common_options` registers `--state-dir/--config/--json` with
`default=argparse.SUPPRESS` so that a value given *before* the subcommand is
not clobbered by the subparser's default. The cost is that the attribute
does not exist at all when the option was never supplied, so any handler
that reads `args.config` directly raises instead of reporting.

One handler did. A per-handler unit test would not have caught the next one,
so the invariant tested here is the general one: for every subcommand, in
its cheapest valid invocation, the CLI must produce a message and a defined
exit code — never an unhandled Python exception.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every subcommand, with the minimum arguments that satisfy argparse.
INVOCATIONS: list[tuple[str, list[str]]] = [
    ("version", ["version"]),
    ("doctor", ["doctor"]),
    ("status", ["status"]),
    ("jobs list", ["jobs", "list"]),
    ("jobs show", ["jobs", "show", "no-such-job"]),
    ("jobs activity", ["jobs", "activity"]),
    ("input", ["input", "hello", "--grant-ref", "g-missing"]),
    ("suggestions list", ["suggestions", "list"]),
    ("suggestions accept", ["suggestions", "accept", "ws-missing"]),
    ("activity", ["activity"]),
    ("runs list", ["runs", "list"]),
    ("runs show", ["runs", "show", "run-missing"]),
    ("approvals list", ["approvals", "list"]),
    ("notifications list", ["notifications", "list"]),
    ("skills audit", ["skills", "audit"]),
    ("hooks list", ["hooks", "list"]),
    ("backup", ["backup", "{tmp}/out.zip"]),
    ("restore", ["restore", "missing.zip"]),
    ("export", ["export", "{tmp}/out.json"]),
    ("config check", ["config", "check"]),
    ("config print", ["config", "print"]),
    ("rpc", ["rpc", "--method", "system.health"]),
    ("delete-data", ["delete-data"]),
    ("serve", ["serve", "--app", "no.such:factory"]),
    ("tick", ["tick", "--app", "no.such:factory"]),
]

# Things that only ever appear when an exception escaped a handler.
CRASH_MARKERS = (
    "Traceback (most recent call last)",
    "AttributeError",
    "TypeError",
    "KeyError:",
    "NameError",
    "UnboundLocalError",
)


class EverySubcommandReportsTests(unittest.TestCase):
    def _run(self, argv: list[str], state_dir: str, workdir: str) -> subprocess.CompletedProcess:
        # Run from a scratch directory and hand every file-producing command
        # an absolute path: a smoke test that writes `out.zip` into the repo
        # root is a test that pollutes the tree it is validating.
        return subprocess.run(
            [sys.executable, "-m", "proactive_sdk.service", "--state-dir", state_dir, *argv],
            capture_output=True, text=True, timeout=120, cwd=workdir,
            env={
                **__import__("os").environ,
                "PYTHONPATH": str(ROOT / "src"),
            },
        )

    def test_no_subcommand_raises_out_of_its_handler(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = str(Path(root) / "state")
            workdir = str(Path(root) / "cwd")
            Path(workdir).mkdir()
            for label, argv in INVOCATIONS:
                argv = [arg.replace("{tmp}", root) for arg in argv]
                with self.subTest(command=label):
                    done = self._run(argv, state_dir, workdir)
                    output = done.stdout + done.stderr
                    for marker in CRASH_MARKERS:
                        self.assertNotIn(
                            marker, output,
                            f"`pas {' '.join(argv)}` crashed:\n{done.stdout}\n{done.stderr}",
                        )
                    # argparse uses 2 for usage errors, and handlers use 0/1/2.
                    self.assertIn(
                        done.returncode, (0, 1, 2),
                        f"unexpected exit {done.returncode} for `pas {' '.join(argv)}`",
                    )

    def test_the_commands_that_should_work_actually_work(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = str(Path(root) / "state")
            workdir = str(Path(root) / "cwd")
            Path(workdir).mkdir()
            for argv in (["version"], ["status"], ["jobs", "list"],
                         ["suggestions", "list"], ["activity"], ["runs", "list"],
                         ["doctor"]):
                with self.subTest(command=" ".join(argv)):
                    done = self._run(argv, state_dir, workdir)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertTrue(done.stdout.strip())

    def test_config_without_a_file_says_so_instead_of_crashing(self):
        """The exact defect that motivated this file."""
        with tempfile.TemporaryDirectory() as root:
            state_dir = str(Path(root) / "state")
            workdir = str(Path(root) / "cwd")
            Path(workdir).mkdir()
            for action in ("check", "print"):
                done = self._run(["config", action], state_dir, workdir)
                self.assertEqual(done.returncode, 2)
                self.assertIn("need --config FILE", done.stderr)

    def test_a_missing_config_file_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = str(Path(root) / "state")
            workdir = str(Path(root) / "cwd")
            Path(workdir).mkdir()
            done = self._run(["config", "check", "--config", "/nope/none.yaml"], state_dir, workdir)
            self.assertEqual(done.returncode, 2)
            self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()


class TheSmokeTestItselfTests(unittest.TestCase):
    def test_it_does_not_write_into_the_repository(self):
        """A validator that dirties the tree it validates is a bad validator."""
        before = {p.name for p in ROOT.iterdir()}
        with tempfile.TemporaryDirectory() as root:
            state_dir = str(Path(root) / "state")
            workdir = str(Path(root) / "cwd")
            Path(workdir).mkdir()
            for argv in (["backup", str(Path(root) / "b.zip")],
                         ["export", str(Path(root) / "e.json")]):
                EverySubcommandReportsTests()._run(argv, state_dir, workdir)
        after = {p.name for p in ROOT.iterdir()}
        self.assertEqual(before, after)
