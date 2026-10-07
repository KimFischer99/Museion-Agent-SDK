"""P6 connector tests: honest authorization states, typed delta mapping,
public material revision semantics. Providers are scripted at the
GwsConnector boundary; no network, no real accounts."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.connectors import (
    CalendarAgendaSource,
    GmailMailSource,
    PublicMaterialSource,
)
from proactive_sdk.contracts import ErrorCode, PASError
from proactive_sdk.gws import GwsAdapter, GwsConnector
from proactive_sdk.net import EgressBroker


class ScriptedGws(GwsConnector):
    def __init__(self) -> None:
        self.replies: dict[tuple[str, tuple[str, ...]], dict] = {}
        self.calls: list[tuple[str, list[str]]] = []

    def call(self, service: str, args: list[str]) -> dict:
        self.calls.append((service, list(args)))
        return self.replies[(service, tuple(args))]


def _adapter_with(status: dict, reply: dict) -> tuple[GwsAdapter, ScriptedGws]:
    gws = ScriptedGws()
    gws.replies[("gmail", ("status",))] = status
    gws.replies[("calendar", ("status",))] = status
    gws.replies[
        ("gmail", ("+triage", "--query", "is:unread", "--max", "10", "--format", "json"))
    ] = reply
    gws.replies[
        ("calendar", ("+agenda", "--days", "1", "--format", "json"))
    ] = reply
    return GwsAdapter(connector=gws), gws


def _fetch(source, *, account="account:primary", scope=None, cursor=None):
    from proactive_sdk.contracts import SourceRequest

    return asyncio.run(
        source.fetch_delta(
            SourceRequest(
                source_id=source.source_id,
                account_ref=account,
                deadline="2026-10-20T12:00:00Z",
                scope=scope or {},
                cursor_ref=cursor,
            )
        )
    )


class NotConnectedHonestyTests(unittest.TestCase):
    def test_not_connected_without_url_is_unavailable(self):
        adapter, _ = _adapter_with({"connected": False}, {})
        source = GmailMailSource(adapter=adapter, account_ref="account:primary")
        with self.assertRaises(PASError) as ctx:
            _fetch(source)
        self.assertEqual(ctx.exception.code, ErrorCode.AUTH_REQUIRED)
        self.assertIn("unavailable", str(ctx.exception))

    def test_not_connected_with_url_reports_its_presence(self):
        adapter, _ = _adapter_with(
            {"connected": False, "connect_url": "https://connect.example/x"}, {}
        )
        source = GmailMailSource(adapter=adapter, account_ref="account:primary")
        with self.assertRaises(PASError) as ctx:
            _fetch(source)
        self.assertIn("connect_url is available", str(ctx.exception))

    def test_no_url_is_ever_invented(self):
        # The exception text must not contain a fabricated URL when the
        # provider offered none.
        adapter, _ = _adapter_with({"connected": False}, {})
        source = GmailMailSource(adapter=adapter, account_ref="account:primary")
        with self.assertRaises(PASError) as ctx:
            _fetch(source)
        self.assertNotIn("https://", str(ctx.exception))


class GmailSourceMappingTests(unittest.TestCase):
    def test_delta_maps_typed_fields_and_stable_revisions(self):
        adapter, gws = _adapter_with(
            {"connected": True, "accounts": [{"account_id": "a1", "display_name": "P"}]},
            {
                "messages": [
                    {"id": "m1", "from": "alice@example.com", "subject": "Invoice",
                     "date": "Mon", "snippet": "Your invoice is ready"},
                    {"id": "m2", "from": "bob@example.com", "subject": "Hello", "snippet": None},
                    {"id": "", "from": "skip", "subject": "no id"},
                ]
            },
        )
        source = GmailMailSource(adapter=adapter, account_ref="account:primary")
        batch = _fetch(source, scope={"query": "is:unread", "max": 10})
        self.assertEqual(batch.source_id, "gmail")
        self.assertEqual(len(batch.items), 2)  # the id-less entry is skipped
        first = batch.items[0]
        self.assertEqual(first.fact_id, "gmail:m1")
        self.assertIn("alice@example.com", first.content)
        self.assertIn("Your invoice is ready", first.content)
        self.assertEqual(first.sensitivity, "private")
        again = _fetch(source, scope={"query": "is:unread", "max": 10})
        self.assertEqual(again.items[0].revision, first.revision)

    def test_account_identity_is_bound(self):
        adapter, _ = _adapter_with({"connected": True}, {"messages": []})
        source = GmailMailSource(adapter=adapter, account_ref="account:primary")
        with self.assertRaises(PASError) as ctx:
            _fetch(source, account="account:other")
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_CONFIG)


class CalendarSourceMappingTests(unittest.TestCase):
    def test_agenda_items_are_content_hashed(self):
        adapter, _ = _adapter_with(
            {"connected": True},
            {
                "events": [
                    {"id": "e1", "summary": "Standup", "start": "2026-10-20T09:00:00+02:00",
                     "end": "2026-10-20T09:30:00+02:00", "responseStatus": "accepted"},
                ]
            },
        )
        source = CalendarAgendaSource(adapter=adapter, account_ref="account:primary")
        batch = _fetch(source, scope={"days": 1})
        self.assertEqual(len(batch.items), 1)
        self.assertEqual(batch.items[0].fact_id, "gcal:e1")
        self.assertIn("Standup", batch.items[0].content)


class _StubBroker(EgressBroker):
    """EgressBroker-typed stub returning scripted page bodies."""

    def __init__(self) -> None:
        super().__init__(allowed_hosts=("example.com",))
        self.bodies: list[str] = []

    def fetch_text(self, url: str) -> str:
        return self.bodies.pop(0)


class PublicMaterialTests(unittest.TestCase):
    def test_revision_changes_only_with_content(self):
        broker = _StubBroker()
        source = PublicMaterialSource(
            account_ref="account:primary", broker=broker, url="https://example.com/feed"
        )
        broker.bodies = ["v1"]
        first = _fetch(source)
        self.assertEqual(len(first.items), 1)
        self.assertEqual(first.items[0].sensitivity, "public")
        self.assertIsNotNone(first.cursor_ref)
        # Unchanged page + the stored cursor → empty delta (L0 suppresses).
        broker.bodies = ["v1"]
        second = _fetch(source, cursor=first.cursor_ref)
        self.assertEqual(second.items, ())
        # Changed page → same fact_id, new revision.
        broker.bodies = ["v2"]
        third = _fetch(source, cursor=first.cursor_ref)
        self.assertEqual(len(third.items), 1)
        self.assertNotEqual(third.items[0].revision, first.items[0].revision)
        self.assertEqual(third.items[0].fact_id, first.items[0].fact_id)

    def test_gmail_and_calendar_cursor_deltas(self):
        adapter, gws = _adapter_with(
            {"connected": True},
            {"messages": [{"id": "m1", "from": "a@x", "subject": "Hi", "snippet": "s"}]},
        )
        source = GmailMailSource(adapter=adapter, account_ref="account:primary")
        scope = {"query": "is:unread", "max": 10}
        first = _fetch(source, scope=scope)
        self.assertEqual(len(first.items), 1)
        second = _fetch(source, scope=scope, cursor=first.cursor_ref)
        self.assertEqual(second.items, ())
        gws.replies[
            ("gmail", ("+triage", "--query", "is:unread", "--max", "10", "--format", "json"))
        ] = {"messages": [{"id": "m1", "from": "a@x", "subject": "Hi CHANGED", "snippet": "s"}]}
        changed = _fetch(source, scope=scope, cursor=first.cursor_ref)
        self.assertEqual(len(changed.items), 1)
        self.assertNotEqual(changed.cursor_ref, first.cursor_ref)


if __name__ == "__main__":
    unittest.main()
