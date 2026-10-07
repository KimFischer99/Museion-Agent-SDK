"""Agent-facing tools for the PAS proactive plugin (SPEC §12.1).

Every handler lands in the PAS control plane; the plugin itself owns no
schedule state, no grants, and no approval authority. Tools exposed to
the model are the restricted §14.3 subset: propose schedules, read
status, pause/resume, and inspect Skill compatibility — never
grants.create or approvals.resolve.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any

from .pas_client import PasRpcClient, PluginRpcError

_JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _schema(name: str, description: str, properties: dict[str, Any],
            required: list[str] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    params["additionalProperties"] = False
    return {"name": name, "description": description, "parameters": params}


@lru_cache(maxsize=1)
def _client() -> PasRpcClient:
    url = os.environ.get("PAS_RPC_URL", "")
    token = os.environ.get("PAS_RPC_TOKEN", "")
    if not url or not token:
        raise PluginRpcError(
            "PAS is not configured: set PAS_RPC_URL and PAS_RPC_TOKEN before enabling "
            "the proactive plugin"
        )
    client = PasRpcClient(url, token)
    client.hello()
    return client


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def handle_proactive_schedule(args: dict[str, Any], **_kw: Any) -> str:
    """proactive.schedule: propose one persisted plan. All policy (grants,
    quiet hours, approval) is decided by PAS — a successful reply means the
    job is stored and scheduled, never that a notification was sent."""
    try:
        job = {
            "job_id": str(args["job_id"]),
            "mode": str(args["mode"]),
            "schedule": args["schedule"],
            "task": {"instruction": str(args["instruction"])},
            "enabled": True,
        }
    except KeyError as exc:
        return _err(f"missing required field {exc.args[0]!r}")
    if not _JOB_ID_RE.fullmatch(job["job_id"]):
        return _err("job_id must be 3..128 chars of [a-z0-9._-], starting alphanumeric")
    try:
        result = _client().jobs_create(job)
    except PluginRpcError as exc:
        return _err(f"PAS refused the schedule: {exc}")
    return _ok({"job": result})


def handle_proactive_status(args: dict[str, Any], **_kw: Any) -> str:
    try:
        result = _client().jobs_list()
    except PluginRpcError as exc:
        return _err(f"PAS status query failed: {exc}")
    jobs = result.get("jobs") if isinstance(result, dict) else None
    return _ok({"jobs": jobs if isinstance(jobs, list) else []})


def handle_proactive_pause(args: dict[str, Any], **_kw: Any) -> str:
    job_id = str(args.get("job_id", ""))
    if not _JOB_ID_RE.fullmatch(job_id):
        return _err("job_id is required")
    try:
        result = _client().jobs_pause(job_id)
    except PluginRpcError as exc:
        return _err(f"PAS pause failed: {exc}")
    return _ok({"job": result})


def handle_proactive_resume(args: dict[str, Any], **_kw: Any) -> str:
    job_id = str(args.get("job_id", ""))
    if not _JOB_ID_RE.fullmatch(job_id):
        return _err("job_id is required")
    try:
        result = _client().jobs_resume(job_id)
    except PluginRpcError as exc:
        return _err(f"PAS resume failed: {exc}")
    return _ok({"job": result})


def handle_proactive_skills_inspect(args: dict[str, Any], **_kw: Any) -> str:
    """skills.inspect: read-only compatibility/explain lookup. Import and
    audit stay control-plane only (SPEC §14.3)."""
    try:
        result = _client().skills_explain(args.get("skill_ref"))
    except PluginRpcError as exc:
        return _err(f"PAS skills.explain failed: {exc}")
    return _ok({"skill": result})


PROACTIVE_SCHEDULE_SCHEMA = _schema(
    "proactive_schedule",
    "Propose one persisted proactive plan (heartbeat, task, watch). Landed in PAS; "
    "delivery still obeys PAS policy: grants, quiet hours, approval. "
    "Scheduling accepted is NOT a notification sent.",
    {
        "job_id": _str("Stable plan id, 3..128 chars [a-z0-9._-]"),
        "mode": {"type": "string", "enum": ["heartbeat", "task", "watch"],
                 "description": "Plan kind; task plans need an explicit task instruction"},
        "schedule": {"type": "object", "description":
                     "PAS schedule object, e.g. {\"kind\":\"daily\",\"local_time\":\"09:00\","
                     "\"timezone\":\"Europe/Berlin\"} or {\"kind\":\"interval\","
                     "\"every_seconds\":1800}"},
        "instruction": _str("What the agent should analyse on each wake"),
    },
    ["job_id", "mode", "schedule", "instruction"],
)

PROACTIVE_STATUS_SCHEMA = _schema(
    "proactive_status",
    "Read the user's persisted PAS plans and their next due times.",
    {},
)

PROACTIVE_PAUSE_SCHEMA = _schema(
    "proactive_pause",
    "Pause one persisted PAS plan (missed occurrences follow the plan's misfire policy).",
    {"job_id": _str("Plan id to pause")},
    ["job_id"],
)

PROACTIVE_RESUME_SCHEMA = _schema(
    "proactive_resume",
    "Resume one paused PAS plan; PAS re-evaluates the next occurrence from now.",
    {"job_id": _str("Plan id to resume")},
    ["job_id"],
)

PROACTIVE_SKILLS_INSPECT_SCHEMA = _schema(
    "proactive_skills_inspect",
    "Look up a Skill's audit/compatibility status inside PAS (read-only).",
    {"skill_ref": _str("Skill reference; omit for the summary")},
)

TOOLS: list[tuple[str, dict[str, Any], Any]] = [
    ("proactive_schedule", PROACTIVE_SCHEDULE_SCHEMA, handle_proactive_schedule),
    ("proactive_status", PROACTIVE_STATUS_SCHEMA, handle_proactive_status),
    ("proactive_pause", PROACTIVE_PAUSE_SCHEMA, handle_proactive_pause),
    ("proactive_resume", PROACTIVE_RESUME_SCHEMA, handle_proactive_resume),
    ("proactive_skills_inspect", PROACTIVE_SKILLS_INSPECT_SCHEMA, handle_proactive_skills_inspect),
]
