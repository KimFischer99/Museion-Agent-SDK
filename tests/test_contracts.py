from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import (
    NOT_AUTOMATICALLY_RETRYABLE,
    ErrorCode,
    ModelRequest,
    ModelResponse,
    PASError,
    ProfileConfig,
    RuntimeConfig,
    AgentExecutor,
    Source,
    ToolBroker,
    DeliverySink,
    ModelPort,
    assert_single_profile,
    canonical_json,
    content_hash,
    error_for_code,
    require_utc_timestamp,
    validate_decision,
    validate_schedule,
)

SPEC_ERROR_CODES = {
    "invalid_config",
    "unsupported_capability",
    "dependency_missing",
    "permission_denied",
    "approval_required",
    "auth_required",
    "rate_limited",
    "budget_exceeded",
    "deadline_exceeded",
    "stale_context",
    "provider_unavailable",
    "conflict",
    "effect_unknown",
    "internal_error",
}


class ErrorTests(unittest.TestCase):
    def test_all_spec_codes_present(self):
        self.assertEqual({c.value for c in ErrorCode}, SPEC_ERROR_CODES)

    def test_error_fields_are_safe_and_complete(self):
        err = PASError(ErrorCode.AUTH_REQUIRED, "credential rejected by provider", scope="provider")
        d = err.to_dict()
        self.assertEqual(
            set(d),
            {"protocol_version", "code", "safe_message", "retryable", "correlation_id", "scope"},
        )
        self.assertIn("protocol_version", d)
        self.assertEqual(d["code"], "auth_required")

    def test_str_carries_no_extras(self):
        err = PASError(ErrorCode.RATE_LIMITED, "provider quota exceeded", retryable=True, retry_after_s=30)
        text = str(err)
        self.assertIn("rate_limited", text)
        self.assertIn("provider quota exceeded", text)

    def test_message_length_guard(self):
        with self.assertRaises(ValueError):
            PASError(ErrorCode.INTERNAL_ERROR, "x" * 501)

    def test_permission_denied_not_auto_retryable(self):
        self.assertIn(ErrorCode.PERMISSION_DENIED, NOT_AUTOMATICALLY_RETRYABLE)
        self.assertIn(ErrorCode.CONFLICT, NOT_AUTOMATICALLY_RETRYABLE)

    def test_error_for_code_factory(self):
        err = error_for_code(ErrorCode.DEADLINE_EXCEEDED, "run exceeded wall time", retryable=True)
        self.assertIsInstance(err, PASError)
        self.assertEqual(err.code, ErrorCode.DEADLINE_EXCEEDED)


class CanonicalJsonTests(unittest.TestCase):
    def test_key_order_independent(self):
        a = canonical_json({"b": 1, "a": 2})
        b = canonical_json({"a": 2, "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a, '{"a":2,"b":1}')

    def test_content_hash_shape_and_stability(self):
        h1 = content_hash({"x": [1, 2], "y": {"z": "汉字"}})
        h2 = content_hash({"y": {"z": "汉字"}, "x": [1, 2]})
        self.assertEqual(h1, h2)
        self.assertRegex(h1, r"^[0-9a-f]{64}$")

    def test_unicode_preserved(self):
        self.assertIn("汉字", canonical_json({"k": "汉字"}))


class TimestampTests(unittest.TestCase):
    def test_accepts_z_and_offset(self):
        require_utc_timestamp("2026-10-06T07:10:00Z")
        require_utc_timestamp("2026-10-06T07:10:00+02:00")
        require_utc_timestamp("2026-10-06T07:10:00.123456Z")

    def test_rejects_naive_and_malformed(self):
        for bad in (
            "2026-10-06T07:10:00",  # no tz
            "2026-10-06 07:10:00Z",  # space separator
            "2026-13-01T00:00:00Z",  # invalid calendar time
            "2026-10-06",
            "",
            "not-a-time",
        ):
            with self.assertRaises(ValueError, msg=bad):
                require_utc_timestamp(bad)


class ScheduleSemanticsTests(unittest.TestCase):
    def test_valid_kinds_pass(self):
        self.assertEqual(validate_schedule({"kind": "interval", "anchor": "2026-10-06T00:00:00Z", "every_seconds": 1800}), [])
        self.assertEqual(validate_schedule({"kind": "daily", "local_time": "09:00", "timezone": "Europe/Berlin"}), [])
        self.assertEqual(validate_schedule({"kind": "weekly", "weekdays": [1, 3], "local_time": "09:00", "timezone": "UTC"}), [])
        self.assertEqual(validate_schedule({"kind": "monthly", "day_of_month": 31, "local_time": "09:00", "timezone": "UTC"}), [])
        self.assertEqual(validate_schedule({"kind": "runonce", "at": "2026-10-07T00:00:00Z"}), [])

    def test_per_kind_required_fields(self):
        errors = validate_schedule({"kind": "interval", "every_seconds": 60})
        self.assertTrue(any("anchor" in e for e in errors))
        errors = validate_schedule({"kind": "runonce"})
        self.assertTrue(any("'at'" in e for e in errors))
        errors = validate_schedule({"kind": "daily", "local_time": "09:00", "timezone": "Europe/Berlin", "anchor": "x"})
        # anchor is not required for daily; no error about anchor
        self.assertFalse(any("anchor" in e for e in errors))

    def test_invalid_timezone_and_time(self):
        errors = validate_schedule({"kind": "daily", "local_time": "25:00", "timezone": "Europe/Berlin"})
        self.assertTrue(any("local_time" in e for e in errors))
        errors = validate_schedule({"kind": "daily", "local_time": "09:00", "timezone": "Mars/Olympus"})
        self.assertTrue(any("IANA" in e for e in errors))

    def test_weekday_range(self):
        errors = validate_schedule({"kind": "weekly", "weekdays": [0, 8], "local_time": "09:00", "timezone": "UTC"})
        self.assertTrue(any("1–7" in e for e in errors))

    def test_naive_anchor_rejected(self):
        errors = validate_schedule({"kind": "interval", "anchor": "2026-10-06T00:00:00", "every_seconds": 60})
        self.assertTrue(any("RFC 3339" in e for e in errors))


class DecisionSemanticsTests(unittest.TestCase):
    def test_silent_requires_empty_proposals(self):
        errors = validate_decision({"decision": "silent", "proposals": [{"kind": "notify_self", "fact_id": "f", "revision": "r"}]})
        self.assertTrue(any("silent" in e for e in errors))

    def test_propose_requires_at_least_one(self):
        errors = validate_decision({"decision": "propose", "proposals": []})
        self.assertTrue(any("at least one" in e for e in errors))

    def test_notify_self_requires_evidence_and_expiry(self):
        base = {"decision": "propose", "proposals": [{"kind": "notify_self", "fact_id": "f", "revision": "r"}]}
        errors = validate_decision(base)
        self.assertTrue(any("evidence_refs" in e for e in errors))
        self.assertTrue(any("expires_at" in e for e in errors))

    def test_complete_notify_self_passes(self):
        decision = {
            "decision": "propose",
            "proposals": [
                {
                    "kind": "notify_self",
                    "fact_id": "source:item-123",
                    "revision": "rev-2",
                    "evidence_refs": ["snapshot:item-123:rev-2"],
                    "expires_at": "2026-10-07T00:00:00Z",
                }
            ],
        }
        self.assertEqual(validate_decision(decision), [])


class SingleProfileTests(unittest.TestCase):
    def make_profile(self, pid: str = "personal") -> ProfileConfig:
        return ProfileConfig(profile_id=pid, state_dir=f"./state-{pid}", timezone="Europe/Berlin", locale="zh-CN")

    def test_valid_profile(self):
        profile = self.make_profile()
        self.assertEqual(profile.profile_id, "personal")

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(PASError):
            ProfileConfig(profile_id="p", state_dir="./s", timezone="Not/AZone", locale="zh-CN")

    def test_invalid_profile_id_rejected(self):
        with self.assertRaises(PASError):
            ProfileConfig(profile_id="BAD ID!", state_dir="./s", timezone="UTC", locale="zh-CN")

    def test_exactly_one_profile(self):
        self.assertEqual(assert_single_profile([self.make_profile()]), self.make_profile())
        with self.assertRaises(PASError):
            assert_single_profile([])
        with self.assertRaises(PASError):
            assert_single_profile([self.make_profile("a"), self.make_profile("b")])

    def test_runtime_config_defaults(self):
        config = RuntimeConfig(profile=self.make_profile())
        self.assertEqual(config.max_concurrent_agent_runs, 1)
        with self.assertRaises(PASError):
            RuntimeConfig(profile=self.make_profile(), max_concurrent_agent_runs=0)


class ProtocolTests(unittest.TestCase):
    def test_runtime_checkable_protocols(self):
        class FakeExecutor:
            async def capabilities(self): ...
            async def start(self, request): ...
            def events(self, handle, after_seq=0): ...
            async def status(self, handle): ...
            async def cancel(self, handle): ...
            async def close(self): ...

        class FakeBroker:
            async def call(self, request): ...

        self.assertIsInstance(FakeExecutor(), AgentExecutor)
        self.assertIsInstance(FakeBroker(), ToolBroker)
        self.assertNotIsInstance(object(), AgentExecutor)

    def test_model_message_records(self):
        request = ModelRequest(messages=({"role": "user", "content": "hi"},))
        response = ModelResponse(content="ok", usage={"input_tokens": 1})
        self.assertEqual(request.messages[0]["role"], "user")
        self.assertEqual(response.usage["input_tokens"], 1)


if __name__ == "__main__":
    unittest.main()
