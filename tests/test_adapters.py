import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from hermes_runs_adapter import HermesRunsClient, TransportError


class HermesContractTests(unittest.TestCase):
    def test_reject_non_local_http(self):
        with self.assertRaises(ValueError): HermesRunsClient("http://example.org", "test")

    def test_reject_url_credentials(self):
        with self.assertRaises(ValueError): HermesRunsClient("https://user:pass@example.org", "test")

    def test_submit_matches_documented_contract(self):
        c = HermesRunsClient("http://127.0.0.1:8642", "test")
        with patch.object(c, "_request", return_value={"run_id": "run_1", "status": "started"}) as call:
            self.assertEqual(c.submit(operation_key="random-op-1", prompt="hello", instructions="return a proposal"), "run_1")
            call.assert_called_once_with("POST", "/v1/runs", {"input": "hello", "instructions": "return a proposal"},
                                         {"Idempotency-Key": "random-op-1"})

    def test_started_is_not_completed(self):
        self.assertIsNone(HermesRunsClient.completed_output({"status": "started"}))

    def test_waiting_approval_not_auto_approved(self):
        self.assertIsNone(HermesRunsClient.completed_output({"status": "waiting_for_approval"}))

    def test_interrupted_is_not_success(self):
        with self.assertRaises(TransportError): HermesRunsClient.completed_output({"status": "interrupted", "output": "partial"})

    def test_only_completed_returns_text(self):
        self.assertEqual(HermesRunsClient.completed_output({"status": "completed", "output": "{}"}), "{}")

    def test_quote_run_id(self):
        c = HermesRunsClient("http://127.0.0.1:8642", "test")
        with patch.object(c, "_request", return_value={}) as call:
            c.stop("run/x")
            call.assert_called_once_with("POST", "/v1/runs/run%2Fx/stop", {})


if __name__ == "__main__": unittest.main()
