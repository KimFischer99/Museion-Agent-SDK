"""PAS proactive plugin for Hermes (SPEC §12.1).

Registers the proactive.* tool family through the official
``register(ctx)`` entry. All operations land in the PAS control plane via
JSON-RPC 2.0 (PAS_RPC_URL / PAS_RPC_TOKEN); this plugin stores nothing
and decides nothing. Configuration errors fail closed at tool-call time
with a safe message, never at import time (AGENTS.md: no import-time
side effects).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re

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
    if callable(getattr(ctx, "register_hook", None)):
        ctx.register_hook("pre_gateway_dispatch", _trusted_input)


def _trusted_input(*, event, gateway, **_kwargs):
    """Route only a configured owner DM; this Hermes hook runs before gateway auth."""
    from .tools import _client

    source = event.source
    platform = getattr(getattr(source, "platform", None), "value", None)
    required = (os.environ.get("PAS_HERMES_PLATFORM"), os.environ.get("PAS_HERMES_USER"),
                os.environ.get("PAS_HERMES_CHAT"), os.environ.get("PAS_HERMES_PROFILE"))
    actual = (platform, source.user_id, source.chat_id, source.profile)
    if not all(required) or actual != required or source.chat_type != "dm" or not event.message_id:
        return None
    if not gateway._is_user_authorized(source):
        return None
    text = event.text
    if not isinstance(text, str) or not re.search(r"提醒我|(?:每天|每周|每月|每隔).*(?:研究|跟进)", text):
        return None
    adapter = gateway._adapter_for_source(source)
    if adapter is None:
        return None
    request_id = hashlib.sha256("\0".join((*required, str(event.message_id))).encode()).hexdigest()

    async def dispatch():
        try:
            result = await asyncio.to_thread(lambda: _client().proactive_input(text, request_id))
            reply = result["reply"]
        except Exception:
            reply = "PAS 请求未确认，请查看任务状态后再重试。"
        await adapter.send(source.chat_id, reply)

    asyncio.get_running_loop().create_task(dispatch())
    return {"action": "skip", "reason": "pas_trusted_input"}
