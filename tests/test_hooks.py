"""P2 hook runtime tests: legacy parser, sandbox layer, hook store
transactions, staging/CAS runner, one-shot watch durability, original
helper compatibility (SPEC §6, §15.1 P2 row, HOOK-01).

Subprocess tests run children through the platform sandbox (seatbelt on
macOS). Original-helper tests are gated on the local private-vendor
copy with its pinned SHA-256; they skip cleanly when it is absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (
    ErrorCode,
    FakeClock,
    HookClaim,
    PASError,
    Store,
)
from proactive_sdk.hooks import (
    HookProtocolError,
    HookResult,
    HookRunner,
    HookRunnerConfig,
    HookSpec,
    PlainSubprocessSandbox,
    SeatbeltSandbox,
    BubblewrapSandbox,
    hook_request_hash,
    parse_hook_logs,
    parse_hook_result,
    platform_sandbox,
)

T0 = 1_760_000_000_000

ORIGINAL_HELPER = (
    Path(__file__).resolve().parents[1] / "private-vendor" / "muse-reuse" / "hatch_hook_runtime.sh"
)
ORIGINAL_HELPER_SHA256 = "c87af221181a1559e3adcb0cdd601f5be5d4bd91eec2fe0597c09dc27558e741"
ISOLATED_SANDBOX_AVAILABLE = (
    sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
)


def make_store(path, *, clock=None, profile="demo", owner="local-inbox:demo"):
    return Store(
        str(path),
        profile=profile,
        owner_destination=owner,
        clock=clock or FakeClock(wall_ms=T0),
    )


def echo_result(result_json: str) -> tuple[str, ...]:
    return ("/bin/echo", f"HATCH_HOOK_RESULT:{result_json}")


def make_runner(store, staging_root, **kwargs) -> HookRunner:
    kwargs.setdefault("config", HookRunnerConfig(timeout_ms=4000, lease_ttl_ms=2000))
    return HookRunner(store, staging_root, **kwargs)


def wake_result(reason="detected", payload=None, disable=False):
    obj = {"decision": "wake", "reason": reason}
    if payload is not None:
        obj["payload"] = payload
    if disable:
        obj["disable_after_run"] = True
    return json.dumps(obj)


class BaseRunnerCase(unittest.TestCase):
    """Store + runner with a short lease so back-to-back runs work."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(wall_ms=T0)
        self.store = make_store(Path(self.tmp.name) / "p.db", clock=self.clock)
        self.staging_root = Path(self.tmp.name) / "staging"
        self.runner = make_runner(self.store, self.staging_root)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def register(self, hook_id, command, *, poll_seconds=30):
        spec = HookSpec(hook_id=hook_id, command=tuple(command), poll_seconds=poll_seconds)
        self.runner.register_hook(spec, now_ms=T0)
        return spec


# --------------------------------------------------------------------------- #
# Legacy parser (SPEC §6.1)
# --------------------------------------------------------------------------- #


class ParseHookResultTests(unittest.TestCase):
    def parse(self, stdout: bytes, exit_code=0, **kw):
        return parse_hook_result(stdout, exit_code, **kw)

    def test_minimal_silent(self):
        result = self.parse(b"HATCH_HOOK_RESULT:" + b'{"decision":"silent","reason":"same"}\n')
        self.assertEqual(result.decision, "silent")
        self.assertEqual(result.reason, "same")
        self.assertIsNone(result.payload)
        self.assertFalse(result.disable_after_run)

    def test_wake_with_arbitrary_legacy_payload(self):
        for payload in ["text", 7, True, None, [1, {"a": 2}], {"k": "v"}]:
            body = json.dumps({"decision": "wake", "reason": "r", "payload": payload})
            result = self.parse(b"HATCH_HOOK_RESULT:" + body.encode() + b"\n")
            self.assertEqual(result.payload, payload)

    def test_disable_after_run_flag(self):
        body = json.dumps({"decision": "wake", "reason": "r", "disable_after_run": True})
        self.assertTrue(self.parse(b"HATCH_HOOK_RESULT:" + body.encode()).disable_after_run)

    def test_diagnostic_text_before_result_is_allowed(self):
        stdout = b"checking source...\nstill checking\nHATCH_HOOK_RESULT:" \
                 b'{"decision":"silent","reason":"ok"}\n'
        self.assertEqual(self.parse(stdout).decision, "silent")

    def test_zero_results_rejected(self):
        with self.assertRaises(HookProtocolError):
            self.parse(b"all quiet\n")

    def test_two_results_rejected(self):
        stdout = (b"HATCH_HOOK_RESULT:" + b'{"decision":"silent","reason":"a"}\n'
                  b"HATCH_HOOK_RESULT:" + b'{"decision":"wake","reason":"b"}\n')
        with self.assertRaises(HookProtocolError):
            self.parse(stdout)

    def test_result_must_be_last_nonempty_line(self):
        stdout = b"HATCH_HOOK_RESULT:" + b'{"decision":"silent","reason":"a"}\ntrailing\n'
        with self.assertRaises(HookProtocolError):
            self.parse(stdout)

    def test_nonzero_exit_rejected(self):
        body = b"HATCH_HOOK_RESULT:" + b'{"decision":"wake","reason":"r"}\n'
        with self.assertRaises(HookProtocolError) as ctx:
            self.parse(body, exit_code=3)
        self.assertEqual(ctx.exception.error_class, "hook_exit_nonzero")

    def test_oversize_stdout_rejected(self):
        body = b"HATCH_HOOK_RESULT:" + b'{"decision":"silent","reason":"r"}\n'
        with self.assertRaises(HookProtocolError) as ctx:
            self.parse(body, stdout_max_bytes=8)
        self.assertEqual(ctx.exception.error_class, "hook_output_oversize")

    def test_non_utf8_rejected(self):
        with self.assertRaises(HookProtocolError):
            self.parse(b"HATCH_HOOK_RESULT:" + b'{"decision":"silent","reason":"\xff\xfe"}\n')

    def test_invalid_json_rejected(self):
        with self.assertRaises(HookProtocolError):
            self.parse(b"HATCH_HOOK_RESULT:{not json}\n")

    def test_duplicate_keys_rejected(self):
        body = b'{"decision":"silent","decision":"wake","reason":"r"}'
        with self.assertRaises(HookProtocolError):
            self.parse(b"HATCH_HOOK_RESULT:" + body + b"\n")

    def test_nonfinite_numbers_rejected(self):
        with self.assertRaises(HookProtocolError):
            self.parse(b'HATCH_HOOK_RESULT:{"decision":"silent","reason":"r","payload":NaN}\n')

    def test_unknown_fields_rejected(self):
        body = b'{"decision":"silent","reason":"r","extra":1}'
        with self.assertRaises(HookProtocolError):
            self.parse(b"HATCH_HOOK_RESULT:" + body + b"\n")

    def test_bad_decision_rejected(self):
        body = b'{"decision":"restart","reason":"r"}'
        with self.assertRaises(HookProtocolError):
            self.parse(b"HATCH_HOOK_RESULT:" + body + b"\n")

    def test_reason_must_be_bounded_string(self):
        for reason in [None, 5, "", "x" * 2049][:1] + [5, "x" * 2049]:
            body = json.dumps({"decision": "silent", "reason": reason})
            with self.assertRaises(HookProtocolError):
                self.parse(b"HATCH_HOOK_RESULT:" + body.encode() + b"\n")

    def test_disable_after_run_must_be_bool(self):
        body = b'{"decision":"wake","reason":"r","disable_after_run":1}'
        with self.assertRaises(HookProtocolError):
            self.parse(b"HATCH_HOOK_RESULT:" + body)

    def test_result_must_be_object(self):
        with self.assertRaises(HookProtocolError):
            self.parse(b'HATCH_HOOK_RESULT:["decision","silent"]\n')

    def test_payload_budget(self):
        body = json.dumps({"decision": "wake", "reason": "r", "payload": "x" * 100})
        with self.assertRaises(HookProtocolError) as ctx:
            self.parse(b"HATCH_HOOK_RESULT:" + body.encode(), payload_max_bytes=16)
        self.assertEqual(ctx.exception.error_class, "hook_payload_oversize")


class ParseHookLogsTests(unittest.TestCase):
    def test_collects_structured_log_entries(self):
        stderr = (b"HATCH_HOOK_LOG:" + b'{"message":"checking","n":1}\n'
                  b"noise without prefix\n"
                  b"HATCH_HOOK_LOG:" + b'{"message":"done"}\n')
        entries = parse_hook_logs(stderr)
        self.assertEqual(entries, ({"message": "checking", "n": 1}, {"message": "done"}))

    def test_malformed_entries_dropped_not_fatal(self):
        stderr = b"HATCH_HOOK_LOG:{broken\nHATCH_HOOK_LOG:" + b'{"ok":true}\n'
        self.assertEqual(parse_hook_logs(stderr), ({"ok": True},))

    def test_entries_bounded(self):
        line = b"HATCH_HOOK_LOG:" + b'{"i":1}\n'
        self.assertEqual(len(parse_hook_logs(line * 100)), 64)

    def test_oversize_stderr_yields_nothing(self):
        self.assertEqual(parse_hook_logs(b"x" * 70000), ())


# --------------------------------------------------------------------------- #
# HookSpec
# --------------------------------------------------------------------------- #


class HookSpecTests(unittest.TestCase):
    def test_valid_spec_roundtrip(self):
        spec = HookSpec(hook_id="watch-a", command=("/bin/echo", "hi"), poll_seconds=15)
        self.assertEqual(spec.to_dict()["command"], ["/bin/echo", "hi"])
        self.assertEqual(spec.definition_hash(), HookSpec(
            hook_id="watch-a", command=("/bin/echo", "hi"), poll_seconds=15
        ).definition_hash())

    def test_different_command_different_hash(self):
        self.assertNotEqual(
            HookSpec(hook_id="a", command=("/bin/echo", "1"), poll_seconds=5).definition_hash(),
            HookSpec(hook_id="a", command=("/bin/echo", "2"), poll_seconds=5).definition_hash(),
        )

    def test_bad_ids_and_commands_rejected(self):
        for kwargs in [
            {"hook_id": "../escape", "command": ("/bin/echo",), "poll_seconds": 5},
            {"hook_id": "UPPER", "command": ("/bin/echo",), "poll_seconds": 5},
            {"hook_id": "ok", "command": "/bin/echo", "poll_seconds": 5},
            {"hook_id": "ok", "command": (), "poll_seconds": 5},
            {"hook_id": "ok", "command": ("/bin/echo", ""), "poll_seconds": 5},
            {"hook_id": "ok", "command": ("/bin/echo",), "poll_seconds": 0},
            {"hook_id": "ok", "command": ("/bin/echo",), "poll_seconds": True},
            {"hook_id": "ok", "command": ("/bin/echo",), "poll_seconds": 5, "timeout_seconds": 0},
        ]:
            with self.assertRaises(PASError, msg=kwargs):
                HookSpec(**kwargs)


# --------------------------------------------------------------------------- #
# Hook store transactions (claim/CAS/error accounting)
# --------------------------------------------------------------------------- #


class StoreHookTests(BaseRunnerCase):
    def test_register_is_idempotent_but_definition_is_immutable(self):
        spec = self.register("h1", echo_result(wake_result()))
        again = self.runner.register_hook(
            HookSpec(hook_id="h1", command=tuple(spec.command), poll_seconds=30), now_ms=T0
        )
        self.assertEqual(again.state_version, 0)
        with self.assertRaises(PASError) as ctx:
            self.runner.register_hook(
                HookSpec(hook_id="h1", command=("/bin/echo", "other"), poll_seconds=30), now_ms=T0
            )
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

    def test_register_rejects_hash_mismatch(self):
        spec = HookSpec(hook_id="h1", command=("/bin/echo", "x"), poll_seconds=5)
        with self.assertRaises(PASError):
            self.store.register_hook(
                "h1", definition_hash="0" * 64, definition=spec.to_dict(),
                poll_interval_ms=5000, now_ms=T0,
            )

    def test_due_hooks_ordered_and_enabled_only(self):
        self.register("h-a", echo_result(wake_result()), poll_seconds=10)
        self.register("h-b", echo_result(wake_result()), poll_seconds=20)
        self.register("h-far", echo_result(wake_result()), poll_seconds=20)
        self.register("h-off", echo_result(wake_result()), poll_seconds=5)
        self.store.set_hook_enabled("h-off", enabled=False, now_ms=T0)
        self.store.db.execute("UPDATE hooks SET next_due_ms=? WHERE hook_id='h-b'", (T0 + 5,))
        self.store.db.execute("UPDATE hooks SET next_due_ms=? WHERE hook_id='h-far'", (T0 + 9000,))
        due = self.store.due_hook_ids(T0 + 6)
        self.assertEqual(due, ["h-a", "h-b"])

    def test_claim_refuses_double_lease_across_connections(self):
        self.register("h1", echo_result(wake_result()))
        other_store = make_store(
            Path(self.tmp.name) / "p.db", clock=self.clock
        )
        try:
            claim = other_store.claim_hook("h1", now_ms=T0, ttl_ms=30000)
            self.assertIsNotNone(claim)
            self.assertIsNone(other_store.claim_hook("h1", now_ms=T0 + 1000, ttl_ms=30000))
            claim2 = other_store.claim_hook("h1", now_ms=T0 + 31000, ttl_ms=30000)
            self.assertIsNotNone(claim2)
            self.assertEqual(claim2.fence, claim.fence + 1)
        finally:
            other_store.close()

    def test_commit_cas_rejects_stale_fence_and_version(self):
        self.register("h1", echo_result(wake_result()))
        claim = self.store.claim_hook("h1", now_ms=T0, ttl_ms=30000)
        result = HookResult("silent", "r")
        base = dict(
            request_hash=hook_request_hash("h1", "h1-v0", 0, {}, result),
            new_state={}, decision=result.decision, reason=result.reason,
            payload=result.payload, disable_after_run=False, next_due_ms=T0 + 1, now_ms=T0,
        )
        stale = self.store.commit_hook_invocation("h1", "h1-v0", expected_state_version=0, fence=claim.fence - 1, **base)
        self.assertEqual(stale.outcome, "skipped_fence")
        stale_v = self.store.commit_hook_invocation(
            "h1", "h1-v0", expected_state_version=claim.state_version + 1, fence=claim.fence, **base
        )
        self.assertEqual(stale_v.outcome, "skipped_version")
        ok = self.store.commit_hook_invocation(
            "h1", "h1-v0", expected_state_version=claim.state_version, fence=claim.fence, **base
        )
        self.assertEqual(ok.outcome, "committed_silent")

    def test_commit_wake_admits_event_and_run(self):
        self.register("h1", echo_result(wake_result()))
        claim = self.store.claim_hook("h1", now_ms=T0, ttl_ms=30000)
        result = HookResult("wake", "changed", {"n": 1})
        commit = self.store.commit_hook_invocation(
            "h1", "h1-v0", expected_state_version=0, fence=claim.fence,
            request_hash=hook_request_hash("h1", "h1-v0", 0, {}, result),
            new_state={}, decision="wake", reason="changed", payload={"n": 1},
            disable_after_run=False, next_due_ms=T0 + 1000, now_ms=T0,
        )
        self.assertEqual(commit.outcome, "committed_wake")
        event = self.store.db.execute("SELECT origin, payload_json FROM events").fetchone()
        self.assertEqual(event["origin"], "hook")
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["hook_id"], "h1")
        self.assertEqual(payload["payload"], {"n": 1})
        run = self.store.db.execute("SELECT state FROM runs").fetchone()
        self.assertEqual(run["state"], "queued")
        record = self.store.get_hook("h1")
        self.assertEqual(record.state_version, 1)
        self.assertEqual(record.last_decision, "wake")

    def test_one_shot_watch_disables_exactly_with_its_event(self):
        """disable_after_run and the wake event commit atomically: the
        hook is disabled only when the event is already durable."""
        self.register("oneshot", echo_result(wake_result(disable=True)))
        claim = self.store.claim_hook("oneshot", now_ms=T0, ttl_ms=30000)
        result = HookResult("wake", "found it")
        commit = self.store.commit_hook_invocation(
            "oneshot", "oneshot-v0", expected_state_version=0, fence=claim.fence,
            request_hash=hook_request_hash("oneshot", "oneshot-v0", 0, {}, result),
            new_state={}, decision="wake", reason="found it", payload=None,
            disable_after_run=True, next_due_ms=T0 + 1000, now_ms=T0,
        )
        self.assertEqual(commit.outcome, "committed_wake")
        record = self.store.get_hook("oneshot")
        self.assertFalse(record.enabled)
        self.assertIsNotNone(commit.event_id)
        # 停探针不删通知：the queued run survives the disable.
        self.assertEqual(
            self.store.db.execute("SELECT state FROM runs WHERE event_id=?", (commit.event_id,)).fetchone()["state"],
            "queued",
        )

    def test_disable_wins_over_in_flight_commit(self):
        self.register("h1", echo_result(wake_result()))
        claim = self.store.claim_hook("h1", now_ms=T0, ttl_ms=30000)
        self.store.set_hook_enabled("h1", enabled=False, now_ms=T0 + 1)
        result = HookResult("wake", "late")
        commit = self.store.commit_hook_invocation(
            "h1", "h1-v0", expected_state_version=0, fence=claim.fence,
            request_hash=hook_request_hash("h1", "h1-v0", 0, {}, result),
            new_state={}, decision="wake", reason="late", payload=None,
            disable_after_run=False, next_due_ms=T0 + 1000, now_ms=T0 + 2,
        )
        self.assertEqual(commit.outcome, "skipped_disabled")
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())

    def test_idempotent_replay_returns_original_event(self):
        self.register("h1", echo_result(wake_result()))
        claim = self.store.claim_hook("h1", now_ms=T0, ttl_ms=30000)
        result = HookResult("wake", "changed")
        request_hash = hook_request_hash("h1", "h1-v0", 0, {}, result)
        first = self.store.commit_hook_invocation(
            "h1", "h1-v0", expected_state_version=0, fence=claim.fence,
            request_hash=request_hash, new_state={}, decision="wake", reason="changed",
            payload=None, disable_after_run=False, next_due_ms=T0 + 1000, now_ms=T0,
        )
        replay = self.store.commit_hook_invocation(
            "h1", "h1-v0", expected_state_version=0, fence=999,
            request_hash=request_hash, new_state={}, decision="wake", reason="changed",
            payload=None, disable_after_run=False, next_due_ms=T0 + 1000, now_ms=T0,
        )
        self.assertEqual(replay.outcome, "idempotent_replay")
        self.assertEqual(replay.event_id, first.event_id)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM hook_invocations").fetchone()[0], 1
        )

    def test_replay_with_different_content_conflicts(self):
        self.register("h1", echo_result(wake_result()))
        claim = self.store.claim_hook("h1", now_ms=T0, ttl_ms=30000)
        result = HookResult("wake", "changed")
        self.store.commit_hook_invocation(
            "h1", "h1-v0", expected_state_version=0, fence=claim.fence,
            request_hash=hook_request_hash("h1", "h1-v0", 0, {}, result),
            new_state={}, decision="wake", reason="changed", payload=None,
            disable_after_run=False, next_due_ms=T0 + 1000, now_ms=T0,
        )
        other = HookResult("wake", "changed differently")
        with self.assertRaises(PASError) as ctx:
            self.store.commit_hook_invocation(
                "h1", "h1-v0", expected_state_version=0, fence=claim.fence,
                request_hash=hook_request_hash("h1", "h1-v0", 0, {}, other),
                new_state={}, decision="wake", reason="changed differently", payload=None,
                disable_after_run=False, next_due_ms=T0 + 1000, now_ms=T0,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.CONFLICT)

    def test_error_accounting_and_backoff(self):
        self.register("h1", echo_result(wake_result()))
        claim = self.store.claim_hook("h1", now_ms=T0, ttl_ms=30000)
        first = self.store.record_hook_error(
            "h1", fence=claim.fence, error_class="hook_timeout", error_detail="killed",
            backoff_until_ms=T0 + 30000, next_due_ms=T0 + 30000, now_ms=T0,
        )
        self.assertEqual(first, "recorded")
        claim2 = self.store.claim_hook("h1", now_ms=T0 + 31000, ttl_ms=30000)
        self.store.record_hook_error(
            "h1", fence=claim2.fence, error_class="hook_timeout", error_detail="killed again",
            backoff_until_ms=T0 + 31000 + 60000, next_due_ms=T0 + 91000, now_ms=T0 + 31000,
        )
        record = self.store.get_hook("h1")
        self.assertEqual(record.consecutive_errors, 2)
        self.assertEqual(record.last_error_class, "hook_timeout")
        self.assertEqual(record.error_backoff_until_ms, T0 + 91000)
        self.assertIsNone(record.last_decision)
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())

    def test_error_recording_respects_fence_and_enabled(self):
        self.register("h1", echo_result(wake_result()))
        claim = self.store.claim_hook("h1", now_ms=T0, ttl_ms=30000)
        self.assertEqual(
            self.store.record_hook_error(
                "h1", fence=claim.fence + 5, error_class="hook_timeout", error_detail="x",
                backoff_until_ms=T0, next_due_ms=T0, now_ms=T0,
            ),
            "skipped_fence",
        )
        self.store.set_hook_enabled("h1", enabled=False, now_ms=T0)
        self.assertEqual(
            self.store.record_hook_error(
                "h1", fence=claim.fence, error_class="hook_timeout", error_detail="x",
                backoff_until_ms=T0, next_due_ms=T0, now_ms=T0,
            ),
            "skipped_disabled",
        )

    def test_defer_and_delete_rules(self):
        self.register("h1", echo_result(wake_result()))
        self.assertEqual(
            self.store.defer_hook("h1", next_due_ms=T0 + 5000, now_ms=T0), "deferred"
        )
        self.assertEqual(self.store.get_hook("h1").next_due_ms, T0 + 5000)
        # Hooks with committed invocations keep their audit trail.
        claim = self.store.claim_hook("h1", now_ms=T0 + 6000, ttl_ms=30000)
        result = HookResult("silent", "r")
        self.store.commit_hook_invocation(
            "h1", "h1-v0", expected_state_version=0, fence=claim.fence,
            request_hash=hook_request_hash("h1", "h1-v0", 0, {}, result),
            new_state={}, decision="silent", reason="r", payload=None,
            disable_after_run=False, next_due_ms=T0 + 7000, now_ms=T0 + 6000,
        )
        with self.assertRaises(PASError):
            self.store.delete_hook("h1")
        fresh = self.register("h2", echo_result(wake_result()))
        del fresh
        self.store.delete_hook("h2")
        self.assertIsNone(self.store.get_hook("h2"))


# --------------------------------------------------------------------------- #
# Runner: staging, bounded spawn, state machine, dry-run, isolation gates
# --------------------------------------------------------------------------- #


class RunnerPipelineTests(BaseRunnerCase):
    def test_silent_commit_does_not_admit_event(self):
        self.register(
            "h1", echo_result('{"decision":"silent","reason":"unchanged"}')
        )
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "committed_silent")
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())
        self.assertEqual(self.store.get_hook("h1").state_version, 1)

    def test_wake_commit_admits_exactly_one_event_and_run(self):
        self.register("h1", echo_result(wake_result("changed", {"id": 42})))
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "committed_wake")
        self.assertIsNotNone(report.event_id)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_state_roundtrip_through_staging(self):
        script = (
            "import json, os, sys\n"
            "p = os.path.join(os.environ['HATCH_HOOK_STATE_DIR'], os.environ['HATCH_HOOK_ID'] + '.json')\n"
            "state = json.load(open(p)) if os.path.exists(p) else {}\n"
            "n = state.get('n', 0) + 1\n"
            "tmp = p + '.tmp'\n"
            "open(tmp, 'w').write(json.dumps({'n': n}))\n"
            "os.replace(tmp, p)\n"
            "print('HATCH_HOOK_RESULT:' + json.dumps({'decision': 'silent', 'reason': 'counted', 'payload': {'n': n}}))\n"
        )
        path = Path(self.tmp.name) / "counter.py"
        path.write_text(script)
        self.register("counter", (sys.executable, str(path)))
        r1 = self.runner.run_hook("counter", now_ms=T0 + 100)
        r2 = self.runner.run_hook("counter", now_ms=T0 + 5000)
        self.assertEqual([r.outcome for r in (r1, r2)], ["committed_silent", "committed_silent"])
        self.assertEqual(r1.payload, {"n": 1})
        self.assertEqual(r2.payload, {"n": 2})
        record = self.store.get_hook("counter")
        self.assertEqual(record.state, {"n": 2})
        self.assertEqual(record.state_version, 2)

    def test_absent_staged_state_keeps_canonical(self):
        self.register("h1", echo_result('{"decision":"silent","reason":"tick"}'))
        self.runner.run_hook("h1", now_ms=T0 + 100)
        # Second run: the child writes no state file.
        report = self.runner.run_hook("h1", now_ms=T0 + 5000)
        self.assertEqual(report.outcome, "committed_silent")
        record = self.store.get_hook("h1")
        self.assertEqual(record.state, {})
        self.assertEqual(record.state_version, 2)

    def test_corrupt_staged_state_is_error_not_silent_reset(self):
        """A corrupt staging state must alarm, never silently reset to
        {} — that would re-trigger first-run detection (AUDIT §4.1)."""
        script = (
            "import os\n"
            "p = os.path.join(os.environ['HATCH_HOOK_STATE_DIR'], os.environ['HATCH_HOOK_ID'] + '.json')\n"
            "open(p, 'w').write('{broken json')\n"
            "print('HATCH_HOOK_RESULT:' + '{\"decision\":\"silent\",\"reason\":\"r\"}')\n"
        )
        path = Path(self.tmp.name) / "corrupt.py"
        path.write_text(script)
        self.register("h1", (sys.executable, str(path)))
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "error")
        self.assertEqual(report.error_class, "hook_state_invalid")
        record = self.store.get_hook("h1")
        self.assertEqual(record.state_version, 0)
        self.assertEqual(record.state, {})
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())

    def test_symlinked_staged_state_rejected(self):
        script = (
            "import os\n"
            "p = os.path.join(os.environ['HATCH_HOOK_STATE_DIR'], os.environ['HATCH_HOOK_ID'] + '.json')\n"
            "os.remove(p)\n"
            "os.symlink('/etc/hosts', p)\n"
            "print('HATCH_HOOK_RESULT:' + '{\"decision\":\"silent\",\"reason\":\"r\"}')\n"
        )
        path = Path(self.tmp.name) / "symlink.py"
        path.write_text(script)
        self.register("h1", (sys.executable, str(path)))
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "error")
        self.assertEqual(report.error_class, "hook_state_invalid")

    def test_non_object_staged_state_rejected(self):
        script = (
            "import os\n"
            "p = os.path.join(os.environ['HATCH_HOOK_STATE_DIR'], os.environ['HATCH_HOOK_ID'] + '.json')\n"
            "open(p, 'w').write('[1,2]')\n"
            "print('HATCH_HOOK_RESULT:' + '{\"decision\":\"silent\",\"reason\":\"r\"}')\n"
        )
        path = Path(self.tmp.name) / "array.py"
        path.write_text(script)
        self.register("h1", (sys.executable, str(path)))
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.error_class, "hook_state_invalid")

    def test_timeout_kills_process_group(self):
        self.register("sleeper", ("/bin/bash", "-c", "/bin/sleep 4711 & /bin/sleep 4711 & wait"))
        report = self.runner.run_hook("sleeper", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "error")
        self.assertEqual(report.error_class, "hook_timeout")
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())
        leftover = subprocess.run(
            ["/usr/bin/pgrep", "-f", "sleep 4711"], capture_output=True, text=True
        )
        self.assertEqual(leftover.stdout.strip(), "", "descendants must be reaped with the group")

    def test_oversize_stdout_killed(self):
        script = (
            "print('x' * 200000)\n"
            "print('HATCH_HOOK_RESULT:' + '{\"decision\":\"wake\",\"reason\":\"r\"}')\n"
        )
        path = Path(self.tmp.name) / "big.py"
        path.write_text(script)
        self.register("h1", (sys.executable, str(path)))
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.error_class, "hook_output_oversize")
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())

    def test_oversize_stderr_killed(self):
        script = (
            "import sys\n"
            "sys.stderr.write('e' * 200000)\n"
            "print('HATCH_HOOK_RESULT:' + '{\"decision\":\"wake\",\"reason\":\"r\"}')\n"
        )
        path = Path(self.tmp.name) / "bigerr.py"
        path.write_text(script)
        self.register("h1", (sys.executable, str(path)))
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.error_class, "hook_output_oversize")
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())

    def test_nonzero_exit_never_wakes(self):
        script = (
            "import sys\n"
            "print('HATCH_HOOK_RESULT:' + '{\"decision\":\"wake\",\"reason\":\"should not count\"}')\n"
            "sys.exit(2)\n"
        )
        path = Path(self.tmp.name) / "fail.py"
        path.write_text(script)
        self.register("h1", (sys.executable, str(path)))
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.error_class, "hook_exit_nonzero")
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())

    def test_no_result_line_is_protocol_error(self):
        self.register("h1", ("/bin/echo", "diagnostics only"))
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.error_class, "hook_protocol_invalid")

    def test_oversize_payload_rejected_before_commit(self):
        big = "x" * 200
        body = json.dumps({"decision": "wake", "reason": "r", "payload": big})
        config = HookRunnerConfig(timeout_ms=4000, lease_ttl_ms=2000, payload_max_bytes=64)
        runner = HookRunner(self.store, self.staging_root, config=config)
        spec = HookSpec(hook_id="h1", command=echo_result(body), poll_seconds=5)
        runner.register_hook(spec, now_ms=T0)
        report = runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.error_class, "hook_payload_oversize")
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())

    def test_error_backoff_doubles_and_force_bypasses(self):
        self.register("h1", ("/bin/false",), poll_seconds=5)
        r1 = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(r1.outcome, "error")
        record = self.store.get_hook("h1")
        self.assertEqual(record.consecutive_errors, 1)
        self.assertEqual(record.error_backoff_until_ms, T0 + 100 + 30000)
        # Inside cooldown: deferred, not re-run.
        r2 = self.runner.run_hook("h1", now_ms=T0 + 5000)
        self.assertEqual(r2.outcome, "deferred_cooldown")
        self.assertEqual(self.store.get_hook("h1").consecutive_errors, 1)
        # Cooldown over: second failure doubles the backoff.
        r3 = self.runner.run_hook("h1", now_ms=T0 + 31000)
        self.assertEqual(r3.outcome, "error")
        record = self.store.get_hook("h1")
        self.assertEqual(record.consecutive_errors, 2)
        self.assertEqual(record.error_backoff_until_ms, T0 + 31000 + 60000)
        # Admin force-run ignores the cooldown (lease from r3 expired).
        r4 = self.runner.run_hook("h1", now_ms=T0 + 34000, force=True)
        self.assertEqual(r4.outcome, "error")
        self.assertEqual(self.store.get_hook("h1").consecutive_errors, 3)

    def test_success_resets_error_accounting(self):
        control = Path(self.tmp.name) / "control"
        control.write_text("fail")
        script = (
            f"import sys\n"
            f"if open({str(control)!r}).read().strip() == 'fail':\n"
            "    sys.exit(1)\n"
            "print('HATCH_HOOK_RESULT:' + '{\"decision\":\"silent\",\"reason\":\"ok\"}')\n"
        )
        path = Path(self.tmp.name) / "flap.py"
        path.write_text(script)
        self.register("flap", (sys.executable, str(path)), poll_seconds=5)
        self.runner.run_hook("flap", now_ms=T0 + 100)
        self.assertEqual(self.store.get_hook("flap").consecutive_errors, 1)
        control.write_text("ok")
        report = self.runner.run_hook("flap", now_ms=T0 + 40000)
        self.assertEqual(report.outcome, "committed_silent")
        record = self.store.get_hook("flap")
        self.assertEqual(record.consecutive_errors, 0)
        self.assertEqual(record.error_backoff_until_ms, 0)
        self.assertEqual(record.last_error_class, None)

    def test_dry_run_touches_nothing(self):
        self.register("h1", echo_result(wake_result("dry wake", {"k": 1})))
        before = self.store.get_hook("h1")
        report = self.runner.run_hook("h1", now_ms=T0 + 100, dry_run=True)
        self.assertEqual(report.outcome, "dry_run")
        self.assertEqual(report.decision, "wake")
        after = self.store.get_hook("h1")
        self.assertEqual(after.state_version, before.state_version)
        self.assertEqual(after.next_due_ms, before.next_due_ms)
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())
        self.assertIsNone(self.store.db.execute("SELECT invocation_id FROM hook_invocations").fetchone())

    def test_disabled_hook_skipped(self):
        self.register("h1", echo_result(wake_result()))
        self.store.set_hook_enabled("h1", enabled=False, now_ms=T0)
        report = self.runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "skipped_disabled")

    def test_run_due_hooks_summary_and_staging_cleanup(self):
        self.register("waker", echo_result(wake_result()), poll_seconds=10)
        self.register("silent", echo_result('{"decision":"silent","reason":"ok"}'), poll_seconds=10)
        self.register("far", echo_result(wake_result()), poll_seconds=100000)
        self.store.defer_hook("far", next_due_ms=T0 + 1000000, now_ms=T0)
        summary = self.runner.run_due_hooks(now_ms=T0 + 100)
        self.assertEqual(summary.wakes, 1)
        self.assertEqual(summary.errors, 0)
        self.assertEqual(len(summary.reports), 2)
        self.assertEqual(list(self.staging_root.iterdir()), [], "staging must be cleaned up")

    def test_cleanup_staging_removes_orphans(self):
        orphan = self.staging_root / "h1-v0-orphan"
        orphan.mkdir(parents=True)
        (orphan / "state.json").write_text("{}")
        self.assertEqual(self.runner.cleanup_staging(), 1)
        self.assertEqual(list(self.staging_root.iterdir()), [])

    def test_unknown_hook_raises(self):
        with self.assertRaises(PASError):
            self.runner.run_hook("ghost", now_ms=T0)

    def test_runner_with_two_connections_not_double_claimed(self):
        """Two runners over independent connections must not both commit
        the same hook state: the lease fence excludes the loser."""
        self.register("h1", echo_result(wake_result()))
        other_store = make_store(Path(self.tmp.name) / "p.db", clock=self.clock)
        try:
            other_runner = make_runner(other_store, Path(self.tmp.name) / "staging2")
            first = self.runner.run_hook("h1", now_ms=T0 + 100)
            second = other_runner.run_hook("h1", now_ms=T0 + 150)
            self.assertEqual(first.outcome, "committed_wake")
            self.assertIn(second.outcome, ("skipped_lease", "skipped_disabled"))
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        finally:
            other_store.close()


class CrashRecoveryTests(BaseRunnerCase):
    def test_crash_before_commit_keeps_state_and_allows_redetect(self):
        """Kill between child exit and commit: canonical state and the
        detection watermark must not advance (SPEC §6.2 step 3→4)."""
        script = (
            "import json, os\n"
            "p = os.path.join(os.environ['HATCH_HOOK_STATE_DIR'], os.environ['HATCH_HOOK_ID'] + '.json')\n"
            "open(p, 'w').write(json.dumps({'seen': 'v1'}))\n"
            "print('HATCH_HOOK_RESULT:' + json.dumps({'decision': 'wake', 'reason': 'changed'}))\n"
        )
        path = Path(self.tmp.name) / "watch.py"
        path.write_text(script)
        self.register("watch", (sys.executable, str(path)))
        original = self.store.commit_hook_invocation

        def crash(*args, **kwargs):
            raise KeyboardInterrupt("simulated crash before commit")

        self.store.commit_hook_invocation = crash
        try:
            self.runner.run_hook("watch", now_ms=T0 + 100)
        except KeyboardInterrupt:
            pass
        finally:
            self.store.commit_hook_invocation = original
        record = self.store.get_hook("watch")
        self.assertEqual(record.state_version, 0)
        self.assertTrue(record.enabled)
        self.assertEqual(record.state, {})
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())
        # Retry re-detects and commits; same invocation id, content replays cleanly.
        report = self.runner.run_hook("watch", now_ms=T0 + 40000)
        self.assertEqual(report.outcome, "committed_wake")
        record = self.store.get_hook("watch")
        self.assertEqual(record.state_version, 1)
        self.assertEqual(record.state, {"seen": "v1"})
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1
        )

    def test_crash_after_commit_event_survives_and_replay_is_idempotent(self):
        """Kill after the commit but before the coordinator finishes:
        the wake event and queued run are durable, and a retry with the
        same invocation content replays instead of double-admitting."""
        script = (
            "import json, os\n"
            "p = os.path.join(os.environ['HATCH_HOOK_STATE_DIR'], os.environ['HATCH_HOOK_ID'] + '.json')\n"
            "open(p, 'w').write(json.dumps({'seen': 'v1'}))\n"
            "print('HATCH_HOOK_RESULT:' + json.dumps({'decision': 'wake', 'reason': 'changed'}))\n"
        )
        path = Path(self.tmp.name) / "watch.py"
        path.write_text(script)
        self.register("watch", (sys.executable, str(path)))

        class CrashAfterCommit(HookRunner):
            def run_hook(self, hook_id, **kwargs):  # simulate death after commit
                report = super().run_hook(hook_id, **kwargs)
                if report.outcome == "committed_wake":
                    raise KeyboardInterrupt("simulated crash after commit")
                return report

        crashing = CrashAfterCommit(self.store, self.staging_root, config=self.runner.config)
        with self.assertRaises(KeyboardInterrupt):
            crashing.run_hook("watch", now_ms=T0 + 100)
        event_count = self.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertEqual(event_count, 1)
        record = self.store.get_hook("watch")
        self.assertEqual(record.state_version, 1)
        self.assertEqual(record.state, {"seen": "v1"})
        # The original runner retries; the child would re-detect the same
        # content under the *new* state version — but even a stale retry
        # with the old invocation id must not double-admit.
        result = HookResult("wake", "changed")
        stale_replay = self.store.commit_hook_invocation(
            "watch", "watch-v0", expected_state_version=0, fence=0,
            request_hash=hook_request_hash("watch", "watch-v0", 0, {"seen": "v1"}, result),
            new_state={"seen": "v1"}, decision="wake", reason="changed", payload=None,
            disable_after_run=False, next_due_ms=T0 + 100, now_ms=T0 + 100,
        )
        self.assertEqual(stale_replay.outcome, "idempotent_replay")
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1
        )


class IsolationGateTests(BaseRunnerCase):
    def test_plain_sandbox_is_refused_when_isolation_required(self):
        with self.assertRaises(PASError) as ctx:
            HookRunner(
                self.store, self.staging_root, sandbox=PlainSubprocessSandbox(),
                require_isolation=True,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.UNSUPPORTED_CAPABILITY)

    def test_plain_sandbox_allowed_only_when_explicitly_downgraded(self):
        runner = HookRunner(
            self.store, self.staging_root, sandbox=PlainSubprocessSandbox(), require_isolation=False,
            config=HookRunnerConfig(timeout_ms=4000, lease_ttl_ms=2000),
        )
        self.register("h1", echo_result(wake_result()))
        report = runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "committed_wake")

    def test_unavailable_sandbox_fails_closed(self):
        class BrokenSandbox(SeatbeltSandbox):
            provides_network_isolation = True
            provides_file_write_isolation = True

            def ensure_available(self):
                return False

        runner = HookRunner(
            self.store, self.staging_root, sandbox=BrokenSandbox(),
            config=HookRunnerConfig(timeout_ms=4000, lease_ttl_ms=2000),
        )
        self.register("h1", echo_result(wake_result()))
        with self.assertRaises(PASError) as ctx:
            runner.run_hook("h1", now_ms=T0 + 100)
        self.assertEqual(ctx.exception.code, ErrorCode.UNSUPPORTED_CAPABILITY)
        # Nothing was claimed or charged: the hook is untouched.
        record = self.store.get_hook("h1")
        self.assertEqual(record.state_version, 0)
        self.assertEqual(record.consecutive_errors, 0)


class SandboxUnitTests(unittest.TestCase):
    def test_platform_sandbox_declares_isolation_on_posix(self):
        sandbox = platform_sandbox()
        if sys.platform in ("darwin",) or sys.platform.startswith("linux"):
            self.assertTrue(sandbox.provides_network_isolation)
            self.assertTrue(sandbox.provides_file_write_isolation)

    def test_seatbelt_profile_and_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()
            sandbox = SeatbeltSandbox()
            argv = sandbox.wrap(["/bin/echo", "hi"], staging_dir=stage)
            self.assertEqual(argv[:2], ["sandbox-exec", "-f"])
            profile = (stage / ".sandbox.sb").read_text()
            self.assertIn("(deny network*)", profile)
            self.assertIn("(deny file-write*)", profile)
            self.assertIn(str(stage.resolve()), profile)

    def test_bubblewrap_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()
            sandbox = BubblewrapSandbox()
            argv = sandbox.wrap(["/bin/echo"], staging_dir=stage)
            self.assertEqual(argv[0], "bwrap")
            self.assertIn("--unshare-net", argv)
            self.assertEqual(argv[-2:], ["--", "/bin/echo"])


@unittest.skipUnless(
    ISOLATED_SANDBOX_AVAILABLE, "seatbelt (sandbox-exec) not available on this platform"
)
class SeatbeltIsolationTests(BaseRunnerCase):
    """End-to-end proof of the no-network / no-file-escape acceptance
    for P2, using the platform sandbox the runner actually applies."""

    def test_hook_cannot_reach_network(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        script = (
            "import socket, sys\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
            "    print('HATCH_HOOK_RESULT:' + '{\"decision\":\"wake\",\"reason\":\"network reached\"}')\n"
            "except OSError:\n"
            "    print('HATCH_HOOK_RESULT:' + '{\"decision\":\"silent\",\"reason\":\"network denied\"}')\n"
        )
        path = Path(self.tmp.name) / "net.py"
        path.write_text(script)
        self.register("netprobe", (sys.executable, str(path)))
        report = self.runner.run_hook("netprobe", now_ms=T0 + 100)
        listener.close()
        self.assertEqual(report.decision, "silent", "network must be denied inside the sandbox")
        self.assertEqual(report.outcome, "committed_silent")

    def test_hook_cannot_write_outside_staging(self):
        escape = Path(self.tmp.name) / "escape.txt"
        script = (
            "import os\n"
            "allowed = os.environ['HATCH_HOOK_STATE_DIR']\n"
            "open(os.path.join(allowed, 'inside.txt'), 'w').write('ok')\n"
            "try:\n"
            f"    open({str(escape)!r}, 'w').write('escape')\n"
            "    print('HATCH_HOOK_RESULT:' + '{\"decision\":\"wake\",\"reason\":\"escaped\"}')\n"
            "except OSError:\n"
            "    print('HATCH_HOOK_RESULT:' + '{\"decision\":\"silent\",\"reason\":\"write denied\"}')\n"
        )
        path = Path(self.tmp.name) / "fs.py"
        path.write_text(script)
        self.register("fsprobe", (sys.executable, str(path)))
        report = self.runner.run_hook("fsprobe", now_ms=T0 + 100)
        self.assertEqual(report.decision, "silent")
        self.assertFalse(escape.exists(), "no file may appear outside staging")
        # The staging scratch tree is gone with the invocation, but the
        # commit shows the child ran and its in-staging write succeeded.
        self.assertEqual(self.store.get_hook("fsprobe").state_version, 1)


# --------------------------------------------------------------------------- #
# Original helper compatibility (SPEC §6.1/§6.3; gated on private-vendor)
# --------------------------------------------------------------------------- #


def _helper_available() -> str | None:
    if not ORIGINAL_HELPER.exists():
        return "original helper not present (private-vendor absent)"
    if shutil.which("bash") is None or shutil.which("jq") is None:
        return "bash/jq not available"
    digest = hashlib.sha256(ORIGINAL_HELPER.read_bytes()).hexdigest()
    if digest != ORIGINAL_HELPER_SHA256:
        return f"unexpected helper hash {digest}"
    return None


_HELPER_BLOCKER = _helper_available()


@unittest.skipIf(_HELPER_BLOCKER is not None, _HELPER_BLOCKER or "")
class OriginalHelperCompatTests(BaseRunnerCase):
    """Run the byte-identical original helper through the full P2
    pipeline (staging + CAS + sandbox), proving wire-protocol
    compatibility without ever shipping the helper itself."""

    def hook_command(self, body: str) -> tuple[str, ...]:
        return ("/bin/bash", "-c", f'set -e; source "{ORIGINAL_HELPER}"; {body}')

    def test_silent_through_full_pipeline(self):
        self.register("compat", self.hook_command('silent "unchanged"'))
        report = self.runner.run_hook("compat", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "committed_silent")
        self.assertEqual(report.decision, "silent")
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())

    def test_wake_with_disable_after_run(self):
        self.register(
            "oneshot", self.hook_command('disable_after_run; wake "changed" \'{"version":2}\'')
        )
        report = self.runner.run_hook("oneshot", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "committed_wake")
        self.assertEqual(report.payload, {"version": 2})
        self.assertTrue(report.disable_after_run)
        record = self.store.get_hook("oneshot")
        self.assertFalse(record.enabled, "one-shot watch must disable itself")
        event = self.store.db.execute("SELECT payload_json FROM events").fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(json.loads(event["payload_json"])["reason"], "changed")
        # The queued run (future notification) survives the disable.
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1
        )

    def test_state_roundtrip_via_canonical_state(self):
        self.register(
            "stateful",
            self.hook_command(
                'state=$(hook_state_get); '
                'n=$(printf "%s" "$state" | jq -r ".n // 0"); '
                "hook_state_set \"$(printf '%s' \"$state\" | jq \".n = ($n + 1)\")\"; "
                'silent "counted" "{\\"n\\": $((n + 1))}"'
            ),
        )
        r1 = self.runner.run_hook("stateful", now_ms=T0 + 100)
        self.assertEqual(r1.outcome, "committed_silent")
        self.assertEqual(r1.payload, {"n": 1})
        r2 = self.runner.run_hook("stateful", now_ms=T0 + 5000)
        self.assertEqual(r2.payload, {"n": 2})
        self.assertEqual(self.store.get_hook("stateful").state, {"n": 2})

    def test_helper_dry_run_skips_state_write_and_commit(self):
        self.register("stateful", self.hook_command('hook_state_set \'{"n":9}\'; silent "dry"'))
        report = self.runner.run_hook("stateful", now_ms=T0 + 100, dry_run=True)
        self.assertEqual(report.outcome, "dry_run")
        self.assertEqual(report.decision, "silent")
        record = self.store.get_hook("stateful")
        self.assertEqual(record.state, {})
        self.assertEqual(record.state_version, 0)
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())
        self.assertIsNone(
            self.store.db.execute("SELECT invocation_id FROM hook_invocations").fetchone()
        )

    def test_helper_log_lines_captured(self):
        self.register("compat", self.hook_command('log "checking" \'{"n":1}\'; silent "done"'))
        report = self.runner.run_hook("compat", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "committed_silent")
        self.assertIn({"message": "checking", "n": 1}, report.logs)

    def test_invalid_payload_is_hook_error_never_wake(self):
        self.register("compat", self.hook_command("wake x 'not-json'"))
        report = self.runner.run_hook("compat", now_ms=T0 + 100)
        self.assertEqual(report.outcome, "error")
        self.assertEqual(report.error_class, "hook_exit_nonzero")
        record = self.store.get_hook("compat")
        self.assertEqual(record.consecutive_errors, 1)
        self.assertIsNone(self.store.db.execute("SELECT event_id FROM events").fetchone())


if __name__ == "__main__":
    unittest.main()
