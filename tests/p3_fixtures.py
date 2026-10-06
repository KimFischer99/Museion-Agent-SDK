"""Shared P3 test fixtures.

Everything here is an explicitly-labelled test fixture: ``ScriptedModel``
is a deterministic model double (provider ``fake``), NOT a real model,
and it reports usage with ``pricing_basis: unknown`` so nothing can
mistake it for measured provider usage. ``StaticSource`` replays scripted
deltas for the Source port.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import (
    ContextPackBuilder,
    EphemeralMemoryPort,
    ExecutorConfig,
    ExecutorCapabilities,
    FakeClock,
    LocalToolBroker,
    ModelResponse,
    ModelToolCall,
    RunBudget,
    SourceBatch,
    SourceItem,
    SourceRegistry,
    ToolSpec,
)

T0 = 1_760_000_000_000  # fixed wall time, same convention as P1/P2 tests


def rfc3339(ms: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class ScriptedModel:
    """Deterministic ModelPort double. Replays scripted turns in order.

    Each scripted turn is a dict with optional keys:
      content (str|None), tool_calls [(call_id, name, args)], usage (dict),
      reasoning (str). Turns are consumed per generate() call; an empty
      script raises AssertionError (test bug, not a model failure).
    """

    provider = "fake"
    model_name = "scripted-fixture"

    def __init__(self, turns: list[dict] | None = None):
        self.turns = list(turns or [])
        self.calls: list[dict] = []
        self._lock = asyncio.Lock()

    def push(self, turn: dict) -> None:
        self.turns.append(turn)

    async def generate(self, request):
        async with self._lock:
            if not self.turns:
                raise AssertionError("ScriptedModel ran out of scripted turns")
            turn = self.turns.pop(0)
        self.calls.append(
            {
                "messages": [dict(m) for m in request.messages],
                "tool_schemas": [dict(s) for s in request.tool_schemas],
                "deadline": request.deadline,
            }
        )
        tool_calls = tuple(
            ModelToolCall(call_id=cid, name=name, arguments=args)
            for cid, name, args in turn.get("tool_calls", [])
        )
        usage = turn.get("usage")
        return ModelResponse(
            content=turn.get("content"),
            tool_calls=tool_calls,
            usage=usage,
            reasoning=turn.get("reasoning"),
        )

    def decision(self, decision: str, summary: str, proposals: list[dict] | None = None) -> dict:
        import json

        body = {"decision": decision, "summary": summary, "proposals": proposals or []}
        return {"content": json.dumps(body, ensure_ascii=False)}


class ScriptedSource:
    """Source double replaying queued SourceBatch results (or raising)."""

    def __init__(self, batches: list[SourceBatch | Exception]):
        self.batches = list(batches)
        self.requests: list[object] = []

    async def fetch_delta(self, request):
        self.requests.append(request)
        if not self.batches:
            raise AssertionError("ScriptedSource ran out of scripted batches")
        item = self.batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def static_batch(
    items: list[tuple[str, str, str]] | None = None,
    *,
    cursor: str | None = "cursor-1",
    observed_ms: int = T0,
    fresh_ms_ahead: int = 30 * 60 * 1000,
    source_id: str = "calendar",
    account: str = "account:primary",
) -> SourceBatch:
    return SourceBatch(
        source_id=source_id,
        account_ref=account,
        observed_at=rfc3339(observed_ms),
        cursor_ref=cursor,
        fresh_until=rfc3339(observed_ms + fresh_ms_ahead),
        items=tuple(
            SourceItem(fact_id=fid, revision=rev, content=content, observed_at=rfc3339(observed_ms))
            for fid, rev, content in (items or [])
        ),
    )


def make_broker(*, capabilities=("calendar.read",), with_evidence_tool=True):
    async def read_evidence(arguments):
        return f"evidence for {arguments.get('fact_id', '?')}"

    broker = LocalToolBroker(capabilities=frozenset(capabilities))
    if with_evidence_tool:
        broker.register(
            ToolSpec(
                name="read_evidence",
                description="Read one stored fact (read-only test tool).",
                parameters={"properties": {"fact_id": {"type": "string"}}, "required": ["fact_id"]},
                required_capability="calendar.read",
            ),
            read_evidence,
        )
    return broker


def make_executor(model, broker=None, *, budget=None, clock=None, repair_attempts=1):
    from proactive_sdk import ToolLoopExecutor

    return ToolLoopExecutor(
        model=model,
        broker=broker if broker is not None else make_broker(),
        capabilities=ExecutorCapabilities(
            external_tool_broker=True, read_only_enforcement=True, usage_reporting=True
        ),
        config=ExecutorConfig(budget=budget or RunBudget(), repair_attempts=repair_attempts),
        clock=clock or FakeClock(wall_ms=T0),
    )


def make_pack_builder(clock=None):
    return ContextPackBuilder(
        locale="zh-CN",
        timezone="Europe/Berlin",
        memory=EphemeralMemoryPort(),
    )


def make_run_request(**overrides):
    from proactive_sdk import RunRequest

    defaults = dict(
        run_id="run0001-abcd",
        attempt=1,
        fence=1,
        context_ref="ctx:run0001-abcd",
        budget=RunBudget(),
        deadline=rfc3339(T0 + 300_000),
        tool_allowlist=("read_evidence",),
    )
    defaults.update(overrides)
    return RunRequest(**defaults)


def make_registry(source_id="calendar", account="account:primary", source=None, capability="calendar.read"):
    registry = SourceRegistry()
    if source is not None:
        registry.register(
            source_id=source_id,
            account_ref=account,
            source=source,
            required_capability=capability,
        )
    return registry


class P3TestCase(unittest.TestCase):
    """Async harness + temp store plumbing shared by P3 tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "p3.db"

    def tearDown(self):
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)
