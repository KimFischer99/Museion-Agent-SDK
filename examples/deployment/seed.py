"""Trusted, operator-invoked setup. No model or external notification calls."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from proactive_sdk import JobSpec


def seed(agent, *, enable_context: bool = False) -> None:
    notify = agent.grant("notify.self", evidence="consent:deployment-example")
    public = agent.grant("public.read", account_ref="account:public",
                         evidence="consent:deployment-example")
    agent.jobs_upsert(JobSpec(
        job_id="source-daily", mode="heartbeat",
        schedule={"kind": "daily", "local_time": "09:00", "timezone": "Asia/Shanghai"},
        task={"instruction": "检查已授权期刊页面；只有实质更新且有证据时通知本人。",
              "refresh_source_ids": ["journal-a-current", "journal-b-index"]},
        grant_refs=(notify.grant_id, public.grant_id),
    ))
    if enable_context and (agent.coordinator.conversation is None or
                           not agent.coordinator.config.conversation_context):
        raise ValueError("context requires an explicit conversation port and conversation_context=true")
    memory = agent.grant("memory.read", evidence="consent:deployment-example") if enable_context else None
    existing = agent.jobs_get("proactive-hourly")
    revision = existing.revision + 1 if existing and bool(existing.enabled) != enable_context else (existing.revision if existing else 1)
    agent.jobs_upsert(JobSpec(
        job_id="proactive-hourly", mode="task", enabled=enable_context, revision=revision,
        schedule={"kind": "interval", "anchor": "2026-10-19T00:00:00Z", "every_seconds": 3600},
        task={"instruction": "根据已授权的对话和记忆判断是否值得主动联系；无依据时保持静默。",
              "always_run": True},
        grant_refs=(notify.grant_id, memory.grant_id) if memory else (notify.grant_id,),
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--consent", required=True, action="store_true",
                        help="authorize notify.self and public.read for this profile")
    parser.add_argument("--context", action="store_true",
                        help="also authorize memory.read and enable the configured context job")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
    from app import build_agent
    from proactive_sdk.config import load_config

    agent = build_agent(load_config(args.config))
    try:
        seed(agent, enable_context=args.context)
    finally:
        asyncio.run(agent.close())
