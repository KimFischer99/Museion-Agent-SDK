from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import ProactiveAgent
from proactive_sdk.service import main


class NoModel:
    async def generate(self, _request):
        raise AssertionError("direct controls must not call the model")


class ProactiveCliTests(unittest.TestCase):
    def test_trusted_disable_persists_without_model_or_existing_grants(self):
        with tempfile.TemporaryDirectory() as root:
            agent = ProactiveAgent(state_dir=root, model=NoModel())
            output = io.StringIO()
            with patch("proactive_sdk.service._load_app", return_value=agent), contextlib.redirect_stdout(output):
                self.assertEqual(main(["proactive", "以后别主动发消息", "--app", "app:build_agent", "--json"]), 0)
            self.assertFalse(json.loads(output.getvalue())["preferences"]["enabled"])
            reopened = ProactiveAgent(state_dir=root, model=NoModel())
            try:
                self.assertFalse(reopened.proactive_preferences()["enabled"])
                self.assertEqual(reopened.grants_list(), [])
                self.assertEqual(reopened.store.list_runs(), [])
            finally:
                reopened.store.close()


if __name__ == "__main__":
    unittest.main()
