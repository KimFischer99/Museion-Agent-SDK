#!/usr/bin/env python3
"""v0.1.2 step 6 on a real agent: the user-wake output flow.

Same prompt, same host, two entry points — that is the whole point:

  A. the *generic* manual event (what a hook or a naive caller produces)
  B. `ProactiveAgent.note_user_input` (the trusted entry)

Before v0.1.2 both were dead: a wake with no job reached policy with
`grant_refs = ()` and every proposal came back `grant_missing`. A should
still be refused; B must reach the owner inbox.

Run ON the host that runs Hermes. Uses the loopback Runs API key from the
validation profile's own config; the key is never printed.

The first version of this probe gave the host a note and a memory entry and
nothing else. The real model answered `silent` in both scenarios, and the
reason it gave was correct:

    "本轮无任何 PR #1234 的源数据或快照，未观察到合并事件；上下文中没有可
     引用的证据，不满足 notify_self 的证据要求，保持静默"

That is the evidence-closure rule doing its job: a note is not an event.
So the probe now gives the host something actually observable — a source
that reports the merge — and the thing under test stays the *authorization*
binding, not source connectivity. The source is scripted and labelled as
such; the host, the model, the policy engine and the ledger are all real.

Cost model: 4 model calls (two per scenario, including the earlier round).
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
    NOTIFY_SELF_CAPABILITY,
    HostBridge,
    MemoryEntry,
    ProactiveAgent,
    SystemClock,
)
from proactive_sdk.hermes import HermesRunsClient, HermesRunsExecutor  # noqa: E402
from proactive_sdk.host_drivers import HermesHostDriver  # noqa: E402
from proactive_sdk.contracts import SourceBatch, SourceItem  # noqa: E402

RESULTS: list[dict] = []

MEMORY_ID = "mem-pr-1234"
NOTE = (
    "帮我盯着 PR #1234。它是我的阻塞项，一旦合并就立刻通知我，并告诉我合并后需要做什么。"
)

SCENARIOS = {
    "generic_manual_event": "the ordinary manual wake (must stay unauthorized)",
    "trusted_note": "ProactiveAgent.note_user_input (must be authorized)",
}


def record(name: str, ok: bool | None, detail: dict | None = None) -> bool:
    entry = {"probe": name, "ok": ok}
    if detail:
        entry["detail"] = detail
    RESULTS.append(entry)
    label = "PASS" if ok else ("FAIL" if ok is False else "INCONCLUSIVE")
    print(f"{label} " + json.dumps(entry, ensure_ascii=False), flush=True)
    return ok is not False


def load_hermes_token() -> str:
    token = os.environ.get("PAS_HERMES_TOKEN", "")
    if token:
        return token
    cfg = Path(
        os.environ.get("PAS_HERMES_PROFILE_CONFIG", "~/.hermes/profiles/pas-p5/config.yaml")
    ).expanduser()
    if not cfg.is_file():
        return ""
    lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "api_server:"), None)
    if start is None:
        return ""
    indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if line.strip().startswith("key:"):
            return line.strip().split(":", 1)[1].strip().strip("\"'")
    return ""


class MergeSource:
    """Reports that PR #1234 was merged.

    A scripted source on purpose: the claim under test is the wake's
    authorization binding, and source connectivity has its own validation.
    The host, the model and the policy engine are the real ones.
    """

    source_id = "vcs"
    account_ref = "account:primary"
    required_capability = "vcs.read"

    def __init__(self, merged: bool = True) -> None:
        self.merged = merged
        self.calls = 0

    async def fetch_delta(self, request):
        self.calls += 1
        now = "2026-10-07T07:00:00Z"
        if not self.merged:
            return SourceBatch(
                source_id=self.source_id, account_ref=self.account_ref,
                observed_at=now, cursor_ref="c0", items=(),
            )
        return SourceBatch(
            source_id=self.source_id,
            account_ref=self.account_ref,
            observed_at=now,
            cursor_ref="c1",
            items=(
                SourceItem(
                    fact_id="pr-1234",
                    revision="merged",
                    content="PR #1234 was merged into main by alice just now.",
                    observed_at=now,
                ),
            ),
        )


def build_agent(state_dir: str) -> ProactiveAgent:
    url = os.environ.get("PAS_HERMES_URL", "http://127.0.0.1:8642/p/pas-p5")
    token = load_hermes_token()
    if not token:
        raise SystemExit("no Runs API key available on this host")
    executor = HermesRunsExecutor(
        url, token, poll_interval_s=3.0, cancel_timeout_s=90.0
    )
    driver = HermesHostDriver(executor)
    return ProactiveAgent(
        state_dir=state_dir,
        executor=HostBridge(driver, capabilities=frozenset({"vcs.read"})),
        sources=(MergeSource(),),
        clock=SystemClock(),
        timezone="Asia/Kolkata",
        locale="zh-CN",
        profile="v012-userwake",
    )


def seed(agent: ProactiveAgent) -> str:
    """A grant, a memory entry the host may cite, and nothing else."""
    grant = agent.create_grant_from_user_consent(
        capability=NOTIFY_SELF_CAPABILITY,
        account_ref="account:primary",
        scope={},
        consent_evidence_ref="consent:v012",
    )
    agent.create_grant_from_user_consent(
        capability="vcs.read",
        account_ref="account:primary",
        scope={},
        consent_evidence_ref="consent:v012-vcs",
    )
    asyncio.run(
        agent.pack_builder.memory.remember(
            MemoryEntry(
                memory_id=MEMORY_ID,
                content="用户在跟踪 PR #1234 的合并进展，这是他的阻塞项。",
                source="user",
            )
        )
    )
    return grant.grant_id


def verdicts_for(agent: ProactiveAgent, run_id: str) -> list[dict]:
    return [
        p["policy"]
        for p in agent.store.run_proposals(run_id)
        if p.get("policy")
    ]


def scenario(name: str, *, trusted: bool) -> None:
    with tempfile.TemporaryDirectory(prefix=f"pas-v012-{name}-") as state_dir:
        agent = build_agent(state_dir)
        try:
            grant_id = seed(agent)
            if trusted:
                event_id = agent.note_user_input(NOTE, grant_refs=(grant_id,))
                authorization = agent.store.get_event(event_id).authorization
                record(
                    f"{name}.event_bound",
                    authorization is not None
                    and authorization.get("destination_ref") == agent.owner_destination,
                    {"destination": (authorization or {}).get("destination_ref")},
                )
            else:
                event_id = agent.store.admit_event(
                    f"manual-generic-{int(time.time())}",
                    origin="manual",
                    payload={"reason": NOTE},
                    observed_at_ms=agent.store.clock.wall_now_ms(),
                    expires_at_ms=agent.store.clock.wall_now_ms() + 3_600_000,
                )
                record(
                    f"{name}.event_unbound",
                    agent.store.get_event(event_id).authorization is None,
                    {"authorization": None},
                )

            report = asyncio.run(agent.tick())
            entry = list(report["runs"])[0] if report["runs"] else None
            if entry is None:
                record(name, False, {"reason": "no run was processed"})
                return
            run_id = entry["run_id"]
            verdicts = verdicts_for(agent, run_id)
            reasons = [v.get("reason") for v in verdicts]
            outbox = agent.store.list_outbox()
            inbox = agent.store.list_inbox()

            detail = {
                "outcome": entry["outcome"],
                "policy_outcome": entry["policy_outcome"],
                "reason": entry["reason"],
                "proposals": len(verdicts),
                "verdict_reasons": reasons,
                "outbox": len(outbox),
                "inbox": len(inbox),
            }
            if not verdicts:
                # The host chose `silent`: the authorization path was not
                # exercised, so neither pass nor fail is honest.
                record(name, None, detail)
                return

            if trusted:
                record(
                    name,
                    "grant_missing" not in reasons and len(inbox) >= 1,
                    detail,
                )
            else:
                # The claim is narrower than "everything was refused": the
                # *deliverable* proposals must be refused for want of
                # authority, and nothing may reach the outbox. Local-record
                # kinds (`internal_record`) are verdict-labelled by kind and
                # are not deliveries.
                record(
                    name,
                    "grant_missing" in reasons and not outbox and not inbox,
                    detail,
                )
        finally:
            asyncio.run(agent.close())


def main() -> int:
    print(f"python={sys.version.split()[0]}", flush=True)
    for name, description in SCENARIOS.items():
        print(f"--- {name}: {description}", flush=True)
        try:
            scenario(name, trusted=(name == "trusted_note"))
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - report, do not hide
            record(name, False, {"error": f"{type(exc).__name__}: {exc}"})
    failed = [r["probe"] for r in RESULTS if r["ok"] is False]
    inconclusive = [r["probe"] for r in RESULTS if r["ok"] is None]
    print(
        "SUMMARY "
        + json.dumps(
            {
                "probes": len(RESULTS),
                "passed": sum(1 for r in RESULTS if r["ok"]),
                "failed": failed,
                "inconclusive": inconclusive,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
