"""The tools a fresh agent already has (SPEC §22.1 item 9).

Three tools, chosen by a narrow rule: read-only, credential-free, and
useful to *any* agent on day one. Nothing here reaches the network, and
nothing here needs configuring, which is what makes them safe to enable
without asking.

They are not decorative. PAS's whole domain is time — schedules, quiet
hours, misfires, "was this already sent" — and a model asked to reason
about those without a clock invents one. Being able to look at its own
memory and its own recent activity is the difference between an agent that
can answer "why didn't you tell me?" and one that cannot.

Two of the three declare a capability (`memory.read`, `state.read`), so
they only work when a real grant exists. That is deliberate: it keeps
`required_capability` meaning something on the default path instead of
being bypassed by convenience.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .toolkit import Tool, tool

__all__ = ["builtin_tools", "CAPABILITY_MEMORY_READ", "CAPABILITY_STATE_READ"]

CAPABILITY_MEMORY_READ = "memory.read"
CAPABILITY_STATE_READ = "state.read"


def builtin_tools(agent: Any) -> tuple[Tool, ...]:
    """The default toolset, bound to one agent.

    Bound rather than global: these read the agent's own clock, memory and
    ledger, so they have to belong to a specific agent instance.
    """

    @tool(name="current_time", description="Current local time for the user, and UTC.")
    def current_time() -> dict[str, Any]:
        """The user's current local date and time, plus UTC.

        Use this rather than assuming a date: schedules, quiet hours and
        "has this already happened" all depend on it.
        """
        now_ms = agent.store.clock.wall_now_ms()
        utc = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        try:
            from zoneinfo import ZoneInfo

            local = utc.astimezone(ZoneInfo(agent.timezone))
        except Exception:  # noqa: BLE001 - an unusable tz must not break the tool
            local = utc
        return {
            "local": local.isoformat(timespec="seconds"),
            "timezone": agent.timezone,
            "utc": utc.isoformat(timespec="seconds"),
            "weekday": local.strftime("%A"),
        }

    @tool(
        name="recall_memory",
        capability=CAPABILITY_MEMORY_READ,
        description="Read the most recent things the agent has remembered about the user.",
    )
    async def recall_memory(limit: int = 8) -> dict[str, Any]:
        """Recent memory entries, newest last.

        Read-only: this inspects what is already remembered and never adds
        to it.
        """
        bounded = max(1, min(int(limit), 32))
        entries = await agent.pack_builder.memory.recall(limit=bounded)
        return {
            "count": len(entries),
            "entries": [
                {
                    "memory_id": entry.memory_id,
                    "content": entry.content,
                    "source": entry.source,
                    "confidence": entry.confidence,
                }
                for entry in entries
            ],
        }

    @tool(
        name="list_recent_activity",
        capability=CAPABILITY_STATE_READ,
        description="Look at what this agent recently planned, stayed quiet about, or sent.",
    )
    def list_recent_activity(limit: int = 10) -> dict[str, Any]:
        """Recent job activity, newest first.

        Each row says whether it is an execution outcome (``analysis``) or a
        notification outcome (``action``/``delivery``); those are separate
        facts and are never merged.
        """
        bounded = max(1, min(int(limit), 50))
        rows = agent.store.job_activity(limit=bounded)
        return {
            "count": len(rows),
            "activity": [
                {
                    "job_id": row.get("job_id"),
                    "phase": row.get("phase"),
                    "state": row.get("state"),
                    "reason": row.get("reason"),
                    "at_ms": row.get("actual_at_ms") or row.get("slot_ms"),
                }
                for row in rows
            ],
        }

    return (current_time, recall_memory, list_recent_activity)
