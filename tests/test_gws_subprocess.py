from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import ErrorCode, PASError
from proactive_sdk.gws import GwsAdapter, SubprocessGwsConnector


class SubprocessGwsConnectorTests(unittest.TestCase):
    def _connector(self, script: str, **kwargs) -> SubprocessGwsConnector:
        return SubprocessGwsConnector((sys.executable, "-c", script), **kwargs)

    def test_adapter_passes_read_only_argv_and_provider_status(self):
        connector = self._connector(
            "import json,sys; print(json.dumps({'service':sys.argv[1], 'args':sys.argv[2:], 'connected':True}))"
        )
        adapter = GwsAdapter(connector=connector)

        result = adapter.execute(
            [
                "hatch_gws_cli",
                "gmail",
                "+triage",
                "--query",
                "subject:test && echo sentinel",
                "--max",
                "3",
                "--format",
                "json",
            ]
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["service"], "gmail")
        self.assertEqual(
            result.data["args"],
            ["+triage", "--query", "subject:test && echo sentinel", "--max", "3", "--format", "json"],
        )
        self.assertTrue(result.data["connected"])

    def test_status_connect_url_is_provider_supplied(self):
        connector = self._connector(
            "import json; print(json.dumps({'connected':False,'connect_url':'https://provider.example/connect'}))"
        )

        result = GwsAdapter(connector=connector).execute(["hatch_gws_cli", "gmail", "status"])

        self.assertTrue(result.ok)
        self.assertFalse(result.data["connected"])
        self.assertEqual(result.connect_url, "https://provider.example/connect")

    def test_invalid_service_args_and_command_are_rejected(self):
        connector = self._connector("print('{}')")
        with self.assertRaises(PASError) as ctx:
            connector.call("drive", [])
        self.assertEqual(ctx.exception.code, ErrorCode.UNSUPPORTED_CAPABILITY)
        with self.assertRaises(PASError) as ctx:
            connector.call("gmail", ["status", ""])
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)
        for command in ((), ("python", ""), "python -c pass"):
            with self.subTest(command=command), self.assertRaises(PASError):
                SubprocessGwsConnector(command)

    def test_invalid_json_and_non_object_are_provider_errors(self):
        for script in ("print('not json')", "print('[]')"):
            connector = self._connector(script)
            with self.subTest(script=script), self.assertRaises(PASError) as ctx:
                connector.call("gmail", ["status"])
            self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_output_limit_and_timeout_are_provider_errors(self):
        too_large = self._connector("print('x' * 2048)", max_output_bytes=128)
        with self.assertRaises(PASError) as ctx:
            too_large.call("gmail", ["status"])
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        self.assertIn("output exceeded limit", str(ctx.exception))

        slow = self._connector("import time; time.sleep(2)", timeout_s=0.05)
        with self.assertRaises(PASError) as ctx:
            slow.call("gmail", ["status"])
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        self.assertIn("timed out", str(ctx.exception))

    def test_stderr_and_nonzero_exit_do_not_expose_provider_output(self):
        sentinel = "credential-sentinel-should-not-leak"
        connector = self._connector(f"import sys; print({sentinel!r}, file=sys.stderr); sys.exit(7)")

        with self.assertRaises(PASError) as ctx:
            connector.call("gmail", ["status"])

        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
        self.assertNotIn(sentinel, str(ctx.exception))

    def test_timeout_and_output_bounds_are_finite(self):
        for kwargs in (
            {"timeout_s": float("inf")},
            {"timeout_s": 121},
            {"max_output_bytes": 1_048_577},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PASError):
                SubprocessGwsConnector((sys.executable, "-c", "pass"), **kwargs)


if __name__ == "__main__":
    unittest.main()
