"""PAS proactive plugin for Hermes (SPEC §12.1).

Registers the proactive.* tool family through the official
``register(ctx)`` entry. All operations land in the PAS control plane via
JSON-RPC 2.0 (PAS_RPC_URL / PAS_RPC_TOKEN); this plugin stores nothing
and decides nothing. Configuration errors fail closed at tool-call time
with a safe message, never at import time (AGENTS.md: no import-time
side effects).
"""

from __future__ import annotations

from .tools import (
    TOOLS,
    handle_proactive_pause,
    handle_proactive_resume,
    handle_proactive_schedule,
    handle_proactive_skills_inspect,
    handle_proactive_status,
)

__all__ = [
    "TOOLS",
    "handle_proactive_pause",
    "handle_proactive_resume",
    "handle_proactive_schedule",
    "handle_proactive_skills_inspect",
    "handle_proactive_status",
    "register",
]


def register(ctx) -> None:
    """Called once by the Hermes plugin loader."""
    for name, schema, handler in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="proactive",
            schema=schema,
            handler=handler,
        )
