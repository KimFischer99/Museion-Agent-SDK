"""Public-contract drift guard: schemas/v1 ⇄ generated TypeScript types.

SPEC §21.1: a public object change must land in Python, ``schemas/v1/``,
the generated client types, RPC/CLI validation and the fixtures in the
same change. Nothing enforced the generated half before v0.1.1, so this
module regenerates the client types in memory and fails when the committed
file drifted.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_sdk.rpc import PROACTIVE_RPC_METHODS  # noqa: E402


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "gen_client_ts", ROOT / "tools" / "gen_client_ts.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ClientTsDriftTests(unittest.TestCase):
    def test_committed_types_match_the_generator_output(self):
        module = _load_generator()
        committed = module.OUT_FILE.read_text(encoding="utf-8")
        self.assertEqual(
            committed,
            module.generate(),
            "packages/client-ts/src/schema_types.ts is stale;"
            " run: python3 tools/gen_client_ts.py",
        )

    def test_every_schema_is_represented(self):
        module = _load_generator()
        text = module.OUT_FILE.read_text(encoding="utf-8")
        for schema in sorted(module.SCHEMA_DIR.glob("*.json")):
            self.assertIn(f"// {schema.name}:", text, schema.name)

    def test_the_new_v011_contracts_are_wire_visible(self):
        module = _load_generator()
        text = module.OUT_FILE.read_text(encoding="utf-8")
        self.assertIn('"mode": "heartbeat" | "task" | "reminder"', text)
        self.assertIn('"recent_notifications"', text)
        self.assertIn("RECENT", text.upper())

    def test_the_ts_method_list_matches_the_frozen_python_list(self):
        """The TypeScript method list mirrors PROACTIVE_RPC_METHODS exactly."""
        client = (ROOT / "packages" / "client-ts" / "src" / "rpc.ts").read_text(encoding="utf-8")
        block = client.split("export const PROACTIVE_RPC_METHODS = [", 1)[1].split("]", 1)[0]
        from_ts = re.findall(r'"([^"]+)"', block)
        self.assertEqual(tuple(from_ts), tuple(PROACTIVE_RPC_METHODS))

    def test_the_v011_methods_are_reachable_from_the_typed_client(self):
        client = (ROOT / "packages" / "client-ts" / "src" / "rpc.ts").read_text(encoding="utf-8")
        for method, helper in (("jobs.stop", "stopJob"), ("jobs.activity", "jobActivity")):
            self.assertIn(f'this.call("{method}"', client)
            self.assertIn(helper, client)

    def test_job_spec_schema_keeps_reminder_closed(self):
        schema = json.loads((ROOT / "schemas" / "v1" / "job_spec.json").read_text())
        reminder = schema["$defs"]["reminder"]
        self.assertFalse(reminder["additionalProperties"])
        self.assertEqual(set(reminder["required"]), {"body", "timezone"})
        self.assertEqual(
            set(reminder["properties"]),
            {
                "title",
                "body",
                "timezone",
                "destination",
                "topic",
                "refresh_sources",
                "fact_refs",
                "artifact_refs",
            },
        )

    def test_context_pack_summary_has_no_body_field(self):
        schema = json.loads((ROOT / "schemas" / "v1" / "context_pack.json").read_text())
        entry = schema["$defs"]["recent_notification"]
        self.assertNotIn("body", entry["properties"])
        self.assertEqual(
            set(entry["required"]), {"fact_digest", "channel_kind", "title", "sent_at_ms"}
        )


if __name__ == "__main__":
    unittest.main()
