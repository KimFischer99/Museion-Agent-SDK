#!/usr/bin/env python3
"""v0.1.2 real-host validation: the host bridge against locked hosts.

Run ON the host that runs Hermes and Pi (the locked P5 environment).

What this validates, and why it is different from the P5 probes: P5 asked
"can PAS talk to the host at all". This asks **"can a host be driven
through the bridge without the caller knowing anything about it"** — the
instruction below is plain natural language, with no hand-written JSON
spec. If an envelope comes back at all, the bridge injected the decision
contract successfully.

Cost model: ~5 tiny model calls (two Hermes completions, one Hermes
cancellation, one Pi completion, one Pi cancellation). Everything else is
transport-level.

Environment:
  PAS_HERMES_URL    default http://127.0.0.1:8642/p/pas-p5
  PAS_HERMES_TOKEN  bearer token (required; never printed)
  PAS_NODE_BIN      node binary for the Pi worker (default "node")
  PAS_PI_ENTRY      absolute path to pi-coding-agent dist/index.js
  PAS_PI_SCRATCH    vetted scratch cwd for Pi sessions (default mkdtemp)

Exit 0 = every probe passed.
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

from proactive_sdk import (  # noqa: E402
    CANCEL_CONFIRMED,
    CANCEL_REQUESTED,
    HOST_CANCEL_LEVELS,
    ContextPack,
    ErrorCode,
    HostBridge,
    PASError,
    RunBudget,
    RunCancelled,
    RunRequest,
)
from proactive_sdk.hermes import HermesRunsClient, HermesRunsExecutor  # noqa: E402
from proactive_sdk.host_drivers import HermesHostDriver, PiHostDriver  # noqa: E402
from proactive_sdk.pi_worker import PiWorkerConfig, PiWorkerExecutor  # noqa: E402

RESULTS: list[dict] = []

# A plain instruction. No JSON shape, no field list — the bridge supplies all
# of that. A working host bridge therefore shows up as a parseable envelope.
PLAIN_INSTRUCTION = (
    "Nothing has changed since the last check. Report that there is nothing to"
    " act on, and do not propose any action."
)


def record(name: str, ok: bool, detail: dict | None = None) -> bool:
    entry = {"probe": name, "ok": bool(ok)}
    if detail:
        entry["detail"] = detail
    RESULTS.append(entry)
    print(("PASS " if ok else "FAIL ") + json.dumps(entry, ensure_ascii=False), flush=True)
    return bool(ok)


def _request(run_id: str) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        attempt=1,
        fence=1,
        context_ref=f"ctx:{run_id}",
        budget=RunBudget(),
        deadline="2030-01-01T00:00:00Z",
        tool_allowlist=(),
    )


def _pack() -> ContextPack:
    return ContextPack(
        task_goal_id="pas-v012-validation",
        task_scope="job:task",
        locale="en",
        timezone="UTC",
        preferences_ref="preferences:validation",
    )


class RecordingClient:
    """Wraps HermesRunsClient to capture submitted bodies (no extra calls)."""

    def __init__(self, inner: HermesRunsClient) -> None:
        self._inner = inner
        self.bodies: list[dict] = []

    def capabilities(self) -> dict:
        return self._inner.capabilities()

    def submit(self, body: dict, operation_key: str) -> dict:
        self.bodies.append(json.loads(json.dumps(body)))
        return self._inner.submit(body, operation_key)

    def status(self, run_id: str) -> dict:
        return self._inner.status(run_id)

    def stop(self, run_id: str) -> dict:
        return self._inner.stop(run_id)

    def events(self, run_id: str) -> dict:
        return self._inner.events(run_id)


def _load_hermes_token() -> str:
    """Resolve the loopback Runs API key *without* exporting the secret.

    Order: explicit env var, then the validation profile's own config, which
    is where the P5 setup keeps it. Either way the value is used only to
    talk to 127.0.0.1 on this host and is never printed or returned."""
    token = os.environ.get("PAS_HERMES_TOKEN", "")
    if token:
        return token
    cfg = Path(
        os.environ.get("PAS_HERMES_PROFILE_CONFIG", "~/.hermes/profiles/pas-p5/config.yaml")
    ).expanduser()
    if not cfg.is_file():
        return ""
    lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "api_server:":
            start = index
            break
    if start is None:
        return ""
    indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if stripped.startswith("key:"):
            value = stripped.split(":", 1)[1].strip()
            return value.strip("\"'")
    return ""


async def validate_hermes() -> bool:
    url = os.environ.get("PAS_HERMES_URL", "http://127.0.0.1:8642/p/pas-p5")
    token = _load_hermes_token()
    if not token:
        record("hermes.config", False, {"reason": "no Runs API key available on this host"})
        return False
    record("hermes.config", True, {"key_source": "env" if os.environ.get("PAS_HERMES_TOKEN") else "profile-config"})

    ok = True
    inner = HermesRunsClient(url, token, timeout=60.0)
    client = RecordingClient(inner)
    executor = HermesRunsExecutor(
        url, token, client=client, poll_interval_s=3.0, cancel_timeout_s=90.0
    )
    driver = HermesHostDriver(executor)
    bridge = HostBridge(driver, capabilities=frozenset({"calendar.read"}))

    # 1. capabilities: the locked feature set, fail closed on anything missing.
    try:
        caps = await driver.capabilities()
        features = caps.get("features") if isinstance(caps, dict) else None
        ok &= record(
            "hermes.capabilities",
            isinstance(features, dict) and features.get("run_submission") is True,
            {"features": sorted(features) if isinstance(features, dict) else None},
        )
    except PASError as exc:
        ok &= record("hermes.capabilities", False, {"error": exc.safe_message})

    # 2. a real envelope through the bridge, from a plain instruction.
    run_id = f"pas-v012-h-{int(time.time())}"
    outcome = None
    try:
        outcome = await bridge.execute(
            _request(run_id), _pack(), [], instruction=PLAIN_INSTRUCTION
        )
        ok &= record(
            "hermes.bridge_envelope",
            outcome.decision.decision in ("silent", "propose"),
            {
                "decision": outcome.decision.decision,
                "proposals": len(outcome.decision.proposals),
                "pricing_basis": outcome.usage.get("pricing_basis"),
            },
        )
    except PASError as exc:
        ok &= record("hermes.bridge_envelope", False, {"error": exc.safe_message})
    except RunCancelled:
        ok &= record("hermes.bridge_envelope", False, {"error": "unexpected cancel"})

    # 3. the contract really travelled: inspect what was POSTed.
    if client.bodies:
        body = client.bodies[-1]
        instructions = str(body.get("instructions") or "")
        ok &= record(
            "hermes.contract_injected",
            "Respond with ONLY one JSON object" in instructions
            and PLAIN_INSTRUCTION in instructions,
            {"instructions_chars": len(instructions)},
        )
    else:
        ok &= record("hermes.contract_injected", False, {"reason": "no captured body"})

    # 4. cancellation: never claim a stopped run without the host's word.
    cancel_run = f"pas-v012-hc-{int(time.time())}"
    task = asyncio.ensure_future(
        bridge.execute(
            _request(cancel_run),
            _pack(),
            [],
            instruction=(
                "Write a detailed 500-word essay about the number seven, then"
                " report that nothing needs attention."
            ),
        )
    )
    await asyncio.sleep(6.0)
    cancel_error = None
    try:
        level = await driver.cancel(cancel_run)
        settled = "cancelled"
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=60.0)
            settled = "completed-despite-cancel"
        except RunCancelled:
            settled = "cancelled"
        except asyncio.TimeoutError:
            settled = "still-running"
        except PASError as exc:
            settled = "errored"
            cancel_error = exc.code.value
        consistent = (
            (level == CANCEL_CONFIRMED and settled == "cancelled")
            or (level == CANCEL_REQUESTED)
        )
        ok &= record(
            "hermes.cancel",
            level in HOST_CANCEL_LEVELS and consistent,
            {"level": level, "settled": settled, "error": cancel_error},
        )
    except PASError as exc:
        ok &= record("hermes.cancel", False, {"error": exc.safe_message})
    finally:
        if not task.done():
            task.cancel()

    await driver.close()
    return ok


async def validate_pi() -> bool:
    node = os.environ.get("PAS_NODE_BIN", "/home/anoki1018/nodejs/bin/node")
    pi_entry = os.environ.get(
        "PAS_PI_ENTRY",
        "/home/anoki1018/nodejs/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js",
    )
    scratch = os.environ.get("PAS_PI_SCRATCH") or tempfile.mkdtemp(prefix="pas-v012-pi-")
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
    executor = PiWorkerExecutor(config)
    driver = PiHostDriver(executor, cwd=scratch)
    bridge = HostBridge(driver)

    try:
        info = await driver.capabilities()
    except PASError as exc:
        ok &= record("pi.initialize", False, {"error": exc.safe_message})
        return False
    ok &= record(
        "pi.initialize",
        isinstance(info.get("pi_version"), str),
        {
            "pi_version": info.get("pi_version"),
            "scratch": scratch,
            "keys": sorted(info) if info else [],
        },
    )

    # A plain instruction: the contract is injected by the bridge, flattened
    # into the single string a Pi turn accepts.
    run_id = f"pas-v012-p-{int(time.time())}"
    try:
        outcome = await bridge.execute(
            _request(run_id), _pack(), [], instruction=PLAIN_INSTRUCTION
        )
        ok &= record(
            "pi.bridge_envelope",
            outcome.decision.decision == "silent",
            {"decision": outcome.decision.decision, "usage": outcome.usage},
        )
    except PASError as exc:
        ok &= record("pi.bridge_envelope", False, {"error": exc.safe_message})

    # Cancellation: the driver must report the level it actually got.
    cancel_run = f"pas-v012-pc-{int(time.time())}"
    task = asyncio.ensure_future(
        bridge.execute(
            _request(cancel_run),
            _pack(),
            [],
            instruction=(
                "Write a detailed 500-word essay about the number seven, then"
                " report that nothing needs attention."
            ),
        )
    )
    await asyncio.sleep(6.0)
    try:
        level = await driver.cancel(cancel_run)
        settled = "unknown"
        error = None
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=60.0)
            settled = "completed-despite-cancel"
        except RunCancelled:
            settled = "cancelled"
        except asyncio.TimeoutError:
            settled = "still-running"
        except PASError as exc:
            settled = "errored"
            error = exc.code.value
        # Any outcome except a fabricated "stopped" is acceptable: the point
        # is that the level and the settled state agree.
        consistent = (
            (level == CANCEL_CONFIRMED and settled == "cancelled")
            or (level == CANCEL_REQUESTED and settled in ("cancelled", "errored", "still-running"))
        )
        ok &= record(
            "pi.cancel",
            level in (CANCEL_CONFIRMED, CANCEL_REQUESTED) and consistent,
            {"level": level, "settled": settled, "error": error},
        )
    except PASError as exc:
        ok &= record("pi.cancel", False, {"error": exc.safe_message})
    finally:
        if not task.done():
            task.cancel()

    await driver.close()
    return ok


async def main() -> int:
    ok = True
    print(f"python={sys.version.split()[0]}", flush=True)
    ok &= await validate_hermes()
    ok &= await validate_pi()
    summary = {
        "probes": len(RESULTS),
        "passed": sum(1 for r in RESULTS if r["ok"]),
        "failed": [r["probe"] for r in RESULTS if not r["ok"]],
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
