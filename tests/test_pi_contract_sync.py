"""Cross-language decision-envelope contract sync (Pi chain).

``src/proactive_sdk/pi_worker/pi_worker.ts`` and ``examples/pi_executor.ts``
each carry a hand-written whitelist of allowed envelope keys. Those copies
must agree with the Python parser's ``DECISION_KEYS`` — the v2
``silence_reason`` field was missing from both TS copies once, and real Pi
responses were rejected before the Python normaliser could see them.

This test reads the two TS sources and fails when the whitelists drift; the
semantic validation itself stays in Python (one validation path).
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_sdk.decision_parse import DECISION_KEYS  # noqa: E402

TS_CONTRACT_FILES = (
    ROOT / "src" / "proactive_sdk" / "pi_worker" / "pi_worker.ts",
    ROOT / "examples" / "pi_executor.ts",
)

_WHITELIST_RE = re.compile(
    r"Object\.keys\(o\)\.some\(k => !\[([^\]]+)\]\.includes\(k\)\)"
)


def _ts_whitelist(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    match = _WHITELIST_RE.search(text)
    if match is None:
        raise AssertionError(f"{path}: decision envelope whitelist not found")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


class PiEnvelopeContractSyncTests(unittest.TestCase):
    def test_every_ts_whitelist_matches_the_python_decision_keys(self):
        expected = set(DECISION_KEYS)
        for path in TS_CONTRACT_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(_ts_whitelist(path), expected)

    def test_the_two_ts_copies_agree_with_each_other(self):
        whitelists = {path.name: _ts_whitelist(path) for path in TS_CONTRACT_FILES}
        self.assertEqual(len(set(frozenset(v) for v in whitelists.values())), 1)

    def test_silence_reason_is_silent_only_and_unknown_fields_still_rejected(self):
        for path in TS_CONTRACT_FILES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIn(
                    'o.decision !== "silent" && o.silence_reason != null', text
                )
                self.assertIn("Object.keys(o).some", text)
                # Proposals stay opaque to the envelope layer: one JSON Schema
                # validator refines them, not a second hand-written copy.
                self.assertIn("o.proposals.length > 8", text)


if __name__ == "__main__":
    unittest.main()
