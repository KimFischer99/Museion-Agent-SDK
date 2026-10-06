from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import validate_decision, validate_schedule
from proactive_sdk.schema_validate import SchemaValidationError, assert_valid, validate

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "v1"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def all_schema_files() -> list[Path]:
    return sorted(SCHEMA_DIR.glob("*.json"))


VALID_FIXTURES = {
    "error.json": {
        "protocol_version": "1.0",
        "code": "rate_limited",
        "safe_message": "provider quota exceeded",
        "retryable": True,
        "retry_after_s": 30,
        "correlation_id": "0123456789abcdef",
    },
    "job_spec.json": {
        "protocol_version": "1.0",
        "job_id": "daily-agenda",
        "revision": 3,
        "owner": "pas",
        "mode": "task",
        "schedule": {"kind": "daily", "local_time": "09:00", "timezone": "Europe/Berlin"},
        "task": {"instruction": "总结今日已授权日历中的安排，并只通知本人。"},
        "grant_refs": ["grant:calendar-primary"],
        "misfire_policy": "grace_once",
        "enabled": True,
    },
    "wake_event.json": {
        "protocol_version": "1.0",
        "event_id": "evt-0001abcd",
        "origin": "hook",
        "occurrence_id": "job:watch-x:slot:000123",
        "observed_at": "2026-10-06T07:00:00Z",
        "expires_at": "2026-10-06T09:00:00Z",
        "source_refs": ["source:tracking:rev-2"],
        "cause": "tracked source changed",
        "dedupe_key": "profile:personal:goal:watch-x:fact:f1:rev-2",
    },
    "run_request.json": {
        "protocol_version": "1.0",
        "run_id": "run-0001abcd",
        "attempt": 1,
        "fence": 2,
        "context_ref": "contextpack:snapshot-42",
        "skill_refs": ["google-calendar"],
        "tool_allowlist": ["calendar.read"],
        "budget": {"max_model_turns": 8, "max_tool_calls": 12, "wall_time_s": 120, "max_proposals": 8},
        "deadline": "2026-10-06T07:05:00Z",
        "policy_version": "policy-v1",
    },
    "run_handle.json": {
        "protocol_version": "1.0",
        "executor_id": "hermes-adapter",
        "host_run_id": "hrun-9",
        "state": "running",
        "resume_token": None,
        "capabilities": ["streaming", "cancellation"],
    },
    "context_pack.json": {
        "schema_version": "1.0",
        "task": {"goal_id": "daily-agenda", "scope": "selected-calendar"},
        "locale": "zh-CN",
        "timezone": "Europe/Berlin",
        "preferences_ref": "preferences:7",
        "sources": [
            {
                "source_id": "calendar",
                "account_ref": "account:primary",
                "snapshot_ref": "snapshot:example",
                "observed_at": "2026-10-06T06:55:00Z",
                "fresh_until": "2026-10-06T07:10:00Z",
                "sensitivity": "private",
            }
        ],
        "pending_refs": [],
        "sent_fact_refs": [],
        "memory_refs": [],
        "untrusted_content_policy": "data_only",
    },
    "decision.json": {
        "protocol_version": "1.0",
        "decision": "propose",
        "summary": "授权来源中出现了尚未通知的新修订。",
        "proposals": [
            {
                "kind": "notify_self",
                "fact_id": "source:item-123",
                "revision": "rev-2",
                "body": "发现一条与你的跟踪目标相关的新内容。",
                "evidence_refs": ["snapshot:item-123:rev-2"],
                "expires_at": "2026-10-07T00:00:00Z",
            }
        ],
    },
    "action_record.json": {
        "protocol_version": "1.0",
        "action_id": "act-0001abcd",
        "kind": "notify_self",
        "canonical_arguments_hash": "a" * 64,
        "grant_id": "grant:notify-owner",
        "approval_id": None,
        "policy_version": "policy-v1",
        "state": "queued",
        "fact_id": "source:item-123",
        "created_at": "2026-10-06T07:01:00Z",
    },
    "delivery_attempt.json": {
        "protocol_version": "1.0",
        "message_id": "msg-0001abcd",
        "attempt_id": "att-0001abcd",
        "fence": 1,
        "provider_key": "notify:owner-default",
        "state": "delivery_unknown",
        "receipt": None,
        "last_error_class": "ack_lost",
        "not_before": "2026-10-06T07:30:00Z",
    },
    "usage.json": {
        "protocol_version": "1.0",
        "provider": "example-llm",
        "model": "example-large",
        "input_tokens": 1200,
        "output_tokens": 240,
        "cache_read_tokens": None,
        "tool_calls": 3,
        "elapsed_ms": 4123,
        "pricing_basis": "measured",
    },
}


class SchemaValidityTests(unittest.TestCase):
    def test_every_schema_file_is_valid_json_object(self):
        for path in all_schema_files():
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(doc, dict, path.name)
            self.assertEqual(doc.get("$schema"), "https://json-schema.org/draft/2020-12/schema", path.name)

    def test_valid_fixtures_pass(self):
        for name, instance in VALID_FIXTURES.items():
            schema = load_schema(name)
            errors = validate(schema, instance)
            self.assertEqual(errors, [], f"{name}: {errors}")

    def test_unknown_property_rejected(self):
        schema = load_schema("decision.json")
        bad = dict(VALID_FIXTURES["decision.json"])
        bad["sneaky_extra"] = True
        with self.assertRaises(SchemaValidationError):
            assert_valid(schema, bad)

    def test_unknown_action_kind_rejected(self):
        schema = load_schema("decision.json")
        bad = json.loads(json.dumps(VALID_FIXTURES["decision.json"]))
        bad["proposals"][0]["kind"] = "send_email_to_anyone"
        with self.assertRaises(SchemaValidationError):
            assert_valid(schema, bad)

    def test_naive_timestamp_rejected(self):
        schema = load_schema("wake_event.json")
        bad = dict(VALID_FIXTURES["wake_event.json"])
        bad["observed_at"] = "2026-10-06T07:00:00"
        errors = validate(schema, bad)
        self.assertTrue(any("RFC 3339" in e for e in errors), errors)

    def test_protocol_version_pattern(self):
        schema = load_schema("run_request.json")
        bad = dict(VALID_FIXTURES["run_request.json"], protocol_version="1")
        errors = validate(schema, bad)
        self.assertTrue(any("pattern" in e for e in errors), errors)

    def test_budget_bounds(self):
        schema = load_schema("run_request.json")
        bad = json.loads(json.dumps(VALID_FIXTURES["run_request.json"]))
        bad["budget"]["max_model_turns"] = 0
        self.assertTrue(validate(schema, bad))

    def test_weekday_bounds(self):
        schema = load_schema("job_spec.json")
        bad = json.loads(json.dumps(VALID_FIXTURES["job_spec.json"]))
        bad["schedule"] = {"kind": "weekly", "weekdays": [8], "local_time": "09:00", "timezone": "UTC"}
        self.assertTrue(validate(schema, bad))

    def test_unique_items_enforced(self):
        schema = load_schema("run_handle.json")
        bad = dict(VALID_FIXTURES["run_handle.json"], capabilities=["streaming", "streaming"])
        self.assertTrue(any("uniqueItems" in e for e in validate(schema, bad)))

    def test_nullable_fields(self):
        schema = load_schema("usage.json")
        ok = dict(VALID_FIXTURES["usage.json"], input_tokens=None, output_tokens=None)
        self.assertEqual(validate(schema, ok), [])


class SemanticCrossFieldTests(unittest.TestCase):
    """Rules deliberately outside JSON Schema; enforced in contracts.py."""

    def test_silent_with_proposals_fails_schema_but_semantics_catch(self):
        instance = dict(VALID_FIXTURES["decision.json"], decision="silent")
        # schema alone accepts it (shape is fine)...
        self.assertEqual(validate(load_schema("decision.json"), instance), [])
        # ...semantics do not.
        self.assertTrue(validate_decision(instance))

    def test_propose_with_empty_proposals(self):
        instance = dict(VALID_FIXTURES["decision.json"], proposals=[])
        self.assertEqual(validate(load_schema("decision.json"), instance), [])
        self.assertTrue(any("at least one" in e for e in validate_decision(instance)))

    def test_schedule_semantics_on_schema_valid_instance(self):
        instance = json.loads(json.dumps(VALID_FIXTURES["job_spec.json"]))
        instance["schedule"] = {"kind": "interval", "every_seconds": 1800}
        self.assertEqual(validate(load_schema("job_spec.json"), instance), [])
        self.assertTrue(any("anchor" in e for e in validate_schedule(instance["schedule"])))


if __name__ == "__main__":
    unittest.main()
