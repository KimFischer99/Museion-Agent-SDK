#!/usr/bin/env python3
"""P5 real-service validation against the locked host versions.

Run ON the host that runs Hermes and Pi (see VALIDATION.md §5e for the
recorded invocation and result). Cost model: a handful of tiny model
calls per full run — one completion, one cancellation, one Pi envelope
run and one Pi cancellation; replay/conflict probes make no model call.

Environment:
  PAS_HERMES_URL    default http://127.0.0.1:8642/p/pas-p5
  PAS_HERMES_TOKEN  bearer token (required for the Hermes part)
  PAS_NODE_BIN      node binary for the Pi worker (default "node")
  PAS_PI_ENTRY      absolute path to pi-coding-agent dist/index.js
  PAS_PI_SCRATCH    vetted scratch cwd for Pi sessions (default mkdtemp)

Exit code 0 = all acceptance probes passed. Writes a JSON summary line
per probe; the caller records them in VALIDATION.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk.contracts import ErrorCode, PASError
from proactive_sdk.hermes import (
    CancellationUnconfirmed,
    HermesRunsClient,
    HermesRunsExecutor,
    HermesRunRequest,
)
from proactive_sdk.pi_worker import PiWorkerConfig, PiWorkerExecutor

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: dict | None = None) -> bool:
    entry = {"probe": name, "ok": ok}
    if detail:
        entry["detail"] = detail
    RESULTS.append(entry)
    print(("PASS " if ok else "FAIL ") + json.dumps(entry, ensure_ascii=False), flush=True)
    return ok


def envelope_instruction() -> str:
    return (
        "Answer with ONLY this JSON object and nothing else:\n"
        '{"decision":"silent","summary":"no changes observed","proposals":[]}'
    )


async def validate_hermes() -> bool:
    url = os.environ.get("PAS_HERMES_URL", "http://127.0.0.1:8642/p/pas-p5")
    token = os.environ.get("PAS_HERMES_TOKEN", "")
    if not token:
        record("hermes.config", False, {"reason": "PAS_HERMES_TOKEN missing"})
        return False
    ok = True
    client = HermesRunsClient(url, token, timeout=20.0)
    ex = HermesRunsExecutor(url, token, client=client, poll_interval_s=2.0, cancel_timeout_s=90.0)

    try:
        caps = await ex.capabilities()
    except PASError as exc:
        record("hermes.capabilities", False, {"error": str(exc)})
        return False
    features = caps.get("features", {})
    ok &= record(
        "hermes.capabilities",
        bool(features.get("run_submission") and features.get("run_stop")
             and isinstance(features.get("runs_idempotency"), dict)
             and features["runs_idempotency"].get("supported") is True),
        {"object": caps.get("object"),
         "idempotency": features.get("runs_idempotency")},
    )

    # 提交≠完成: acceptance is not completion; the output comes from polling.
    op_key = f"pas-validate-{int(time.time())}"
    request = HermesRunRequest(
        operation_key=op_key,
        prompt="Reply with exactly: OK",
        instructions="Smoke validation for PAS. Reply with only: OK",
    )
    try:
        handle = await ex.start(request)
        result = await ex.wait(handle, timeout_s=180)
    except PASError as exc:
        record("hermes.submit_complete", False, {"error": str(exc)})
        return False
    ok &= record(
        "hermes.submit_complete",
        result.handle.state == "completed"
        and isinstance(result.output, str)
        and result.usage.get("pricing_basis") == "measured",
        {"state": result.handle.state, "usage": result.usage},
    )

    # Replay of the identical operation key + payload returns the same run.
    try:
        replayed = await ex.reconcile(request)
    except PASError as exc:
        record("hermes.idempotency_replay", False, {"error": str(exc)})
        replayed = None
    ok &= record(
        "hermes.idempotency_replay",
        replayed is not None and replayed.host_run_id == handle.host_run_id,
        {"reconciled_run": replayed.host_run_id if replayed else None},
    )

    # Cancellation: stop, then poll to the host-confirmed terminal state.
    op_key_cancel = f"pas-validate-cancel-{int(time.time())}"
    cancel_req = HermesRunRequest(
        operation_key=op_key_cancel,
        prompt="Count from 1 to 40, one number per line, then say DONE.",
        instructions="Follow the input literally.",
    )
    try:
        cancel_handle = await ex.start(cancel_req)
        await asyncio.sleep(2.0)
        cancelled = await ex.cancel(cancel_handle)
        ok &= record(
            "hermes.cancel_confirmed",
            cancelled.handle.state == "cancelled",
            {"state": cancelled.handle.state, "host_status": cancelled.host_status},
        )
    except CancellationUnconfirmed as exc:
        ok &= record("hermes.cancel_confirmed", False, {"error": str(exc)})
    except PASError as exc:
        ok &= record("hermes.cancel_confirmed", False, {"error": str(exc)})

    # A used operation key with a different payload must conflict, never rewrite.
    try:
        await ex.reconcile(
            HermesRunRequest(
                operation_key=op_key,
                prompt="DIFFERENT payload",
                instructions="DIFFERENT payload",
            )
        )
        ok &= record("hermes.key_conflict", False, {"reason": "conflict not raised"})
    except PASError as exc:
        ok &= record("hermes.key_conflict", exc.code == ErrorCode.CONFLICT, {"code": exc.code})
    return ok


async def validate_pi() -> bool:
    node = os.environ.get("PAS_NODE_BIN", "node")
    pi_entry = os.environ.get(
        "PAS_PI_ENTRY",
        "/home/anoki1018/nodejs/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js",
    )
    scratch = os.environ.get("PAS_PI_SCRATCH") or tempfile.mkdtemp(prefix="pas-p5-pi-")
    worker_ts = str(
        Path(__file__).resolve().parents[1]
        / "src" / "proactive_sdk" / "pi_worker" / "pi_worker.ts"
    )
    config = PiWorkerConfig(
        command=(node, "--experimental-strip-types", worker_ts),
        pi_entry=pi_entry,
        allowed_tools=("read", "grep", "find", "ls"),
        init_timeout_s=60.0,
        run_timeout_s=240.0,
        cancel_timeout_s=30.0,
    )
    ok = True
    ex = PiWorkerExecutor(config)
    try:
        info = await ex.start()
    except PASError as exc:
        record("pi.initialize", False, {"error": str(exc)})
        return False
    ok &= record(
        "pi.initialize", isinstance(info.get("pi_version"), str),
        {"pi_version": info.get("pi_version"), "scratch": scratch},
    )

    try:
        result = await ex.run(
            run_id=f"pas-validate-{int(time.time())}",
            instruction=envelope_instruction(),
            cwd=scratch,
        )
        envelope = result.envelope
        ok &= record(
            "pi.run_envelope",
            envelope.get("decision") == "silent"
            and isinstance(envelope.get("summary"), str)
            and envelope.get("proposals") == []
            and result.usage.get("pricing_basis") == "measured",
            {"usage": result.usage},
        )
    except PASError as exc:
        ok &= record("pi.run_envelope", False, {"error": str(exc)})

    # 取消≠已停: cancel the long run and require the worker to settle honestly.
    run_id = f"pas-validate-cancel-{int(time.time())}"
    task = asyncio.ensure_future(ex.run(
        run_id=run_id,
        instruction=(
            "Write a 400-word essay about the number 7. After the essay, "
            "reply with ONLY this JSON object and nothing else:\n"
            '{"decision":"silent","summary":"essay done","proposals":[]}'
        ),
        cwd=scratch,
    ))
    await asyncio.sleep(4.0)
    try:
        await ex.cancel(run_id)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
            ok &= record("pi.cancel", False, {"reason": "run finished despite cancel"})
        except asyncio.TimeoutError:
            ok &= record("pi.cancel", False, {"reason": "run did not settle after cancel"})
        except PASError as exc:
            ok &= record("pi.cancel", exc.code == ErrorCode.CONFLICT, {"code": exc.code})
    except PASError as exc:
        if exc.code == ErrorCode.CONFLICT and "cancellation_unconfirmed" in str(exc):
            ok &= record("pi.cancel", True, {"outcome": "cancellation_unconfirmed"})
        else:
            ok &= record("pi.cancel", False, {"error": str(exc)})
    finally:
        # Always retrieve the run task's outcome so evidence output stays clean.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except (PASError, asyncio.TimeoutError):
            pass

    await ex.close()
    return ok


async def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    passed = True
    if which in ("all", "hermes"):
        passed &= await validate_hermes()
    if which in ("all", "pi"):
        passed &= await validate_pi()
    summary = {"suite": "p5-real", "passed": passed,
               "probes": RESULTS,
               "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    Path("/tmp/p5-real-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("SUITE " + ("PASS" if passed else "FAIL"), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
