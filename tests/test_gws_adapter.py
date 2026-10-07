"""P6 GWS adapter tests: restricted hatch_gws_cli grammar, honest
authorization states, read-only subset, typed provider plumbing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import PASError
from proactive_sdk.gws import GwsAdapter, GwsConnector, GwsResult


class ScriptedGws(GwsConnector):
    """Scripted provider; replies keyed by (service, args tuple)."""

    def __init__(self) -> None:
        self.replies: dict[tuple[str, tuple[str, ...]], dict] = {}
        self.errors: dict[tuple[str, tuple[str, ...]], Exception] = {}
        self.calls: list[tuple[str, list[str]]] = []

    def call(self, service: str, args: list[str]) -> dict:
        self.calls.append((service, list(args)))
        key = (service, tuple(args))
        if key in self.errors:
            raise self.errors[key]
        if key in self.replies:
            return self.replies[key]
        raise AssertionError(f"unexpected provider call {key}")


def _connected_gmail():
    return {"connected": True, "accounts": [{"account_id": "a1", "display_name": "Primary"}]}


class GrammarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gws = ScriptedGws()
        self.adapter = GwsAdapter(connector=self.gws)

    def test_requires_hatch_prefix(self):
        result = self.adapter.execute(["gcloud", "gmail", "status"])
        self.assertEqual(result.error_code, "unsupported_command")

    def test_unknown_service_refused(self):
        result = self.adapter.execute(["hatch_gws_cli", "shell", "rm"])
        self.assertEqual(result.error_code, "unsupported_command")

    def test_unknown_gmail_command_refused(self):
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "+send", "--to", "x"])
        self.assertEqual(result.error_code, "unsupported_command")
        self.assertIn("writes go through PAS approvals", result.safe_error or "")

    def test_calendar_write_refused(self):
        result = self.adapter.execute(["hatch_gws_cli", "calendar", "events.insert"])
        self.assertEqual(result.error_code, "unsupported_command")

    def test_triage_requires_query(self):
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "+triage"])
        self.assertEqual(result.error_code, "unsupported_command")

    def test_triage_max_is_cost_bounded(self):
        result = self.adapter.execute(
            ["hatch_gws_cli", "gmail", "+triage", "--query", "is:unread", "--max", "500"]
        )
        self.assertEqual(result.error_code, "unsupported_command")

    def test_read_requires_id(self):
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "+read"])
        self.assertEqual(result.error_code, "unsupported_command")

    def test_malformed_flags_fail_typed(self):
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "+read", "--id"])
        self.assertEqual(result.error_code, "unsupported_command")

    def test_non_argv_input_refused(self):
        self.assertEqual(
            self.adapter.execute("hatch_gws_cli gmail status").error_code, "unsupported_command"
        )


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gws = ScriptedGws()
        self.adapter = GwsAdapter(connector=self.gws)

    def test_connected_status_passes_accounts_identity_only(self):
        self.gws.replies[("gmail", ("status",))] = _connected_gmail()
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "status"])
        self.assertTrue(result.ok)
        self.assertTrue(result.data["connected"])
        self.assertEqual(result.data["accounts"][0]["account_id"], "a1")

    def test_not_connected_surfaces_provider_connect_url_verbatim(self):
        url = "https://accounts.example/connector?state=abc"
        self.gws.replies[("gmail", ("status",))] = {"connected": False, "connect_url": url}
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "status"])
        self.assertTrue(result.ok)  # the status command itself succeeded
        self.assertFalse(result.data["connected"])
        self.assertEqual(result.data["connect_url"], url)

    def test_unavailable_when_provider_gives_no_url(self):
        self.gws.replies[("gmail", ("status",))] = {"connected": False}
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "status"])
        self.assertTrue(result.data["unavailable"])
        self.assertIsNone(result.connect_url)  # never synthesized

    def test_for_command_scope_forwarded(self):
        self.gws.replies[("gmail", ("status", "--for-command", "gmail.+read"))] = _connected_gmail()
        result = self.adapter.execute(
            ["hatch_gws_cli", "gmail", "status", "--for-command", "gmail.+read"]
        )
        self.assertTrue(result.ok)


class ReadPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gws = ScriptedGws()
        self.gws.replies[("gmail", ("status",))] = _connected_gmail()
        self.gws.replies[("calendar", ("status",))] = _connected_gmail()
        self.adapter = GwsAdapter(connector=self.gws, default_account="a1")

    def test_triage_happy_path_forwards_cost_bound_args(self):
        self.gws.replies[
            ("gmail", ("+triage", "--query", "is:unread", "--max", "10", "--format", "json", "--account", "a1"))
        ] = {"messages": [{"id": "m1", "from": "a@x", "subject": "Hi", "snippet": "body"}]}
        result = self.adapter.execute(
            ["hatch_gws_cli", "gmail", "+triage", "--query", "is:unread", "--max", "10"]
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["messages"][0]["id"], "m1")

    def test_read_happy_path_includes_headers_flag(self):
        self.gws.replies[
            ("gmail", ("+read", "--id", "m9", "--headers", "--format", "json", "--account", "a1"))
        ] = {"id": "m9", "subject": "Hi"}
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "+read", "--id", "m9"])
        self.assertTrue(result.ok)
        self.assertEqual(result.data["subject"], "Hi")

    def test_agenda_defaults_to_today(self):
        self.gws.replies[
            ("calendar", ("+agenda", "--today", "--format", "json", "--account", "a1"))
        ] = {"events": [{"id": "e1", "summary": "Standup"}]}
        result = self.adapter.execute(["hatch_gws_cli", "calendar", "+agenda"])
        self.assertTrue(result.ok)
        self.assertEqual(result.data["events"][0]["summary"], "Standup")

    def test_agenda_days_bounded(self):
        result = self.adapter.execute(
            ["hatch_gws_cli", "calendar", "+agenda", "--days", "400"]
        )
        self.assertEqual(result.error_code, "unsupported_command")

    def test_provider_auth_error_maps_to_reauth(self):
        self.gws.replies[("gmail", ("+triage", "--query", "q", "--max", "20", "--format", "json", "--account", "a1"))] = {
            "auth_error": "gmail.+triage"
        }
        result = self.adapter.execute(["hatch_gws_cli", "gmail", "+triage", "--query", "q"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "reauth_required")

    def test_connector_crash_hides_internals(self):
        class Boom(GwsConnector):
            def call(self, service, args):
                raise RuntimeError("secret internal stack details")

        adapter = GwsAdapter(connector=Boom())
        result = adapter.execute(["hatch_gws_cli", "gmail", "+triage", "--query", "q"])
        self.assertEqual(result.error_code, "provider_error")
        self.assertNotIn("secret", result.safe_error or "")


if __name__ == "__main__":
    unittest.main()
