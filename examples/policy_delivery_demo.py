"""策略与投递 demo（P4 / SPEC §15.1 P4 行）。

五个场景串起 P4 的核心语义，全部使用显式测试替身：
- 本地 scripted webhook 服务器（真实 loopback HTTP，非进程内 mock），
  用于演示真实通知 sink 的 ACK 丢失与对账；
- ScriptedModel / ScriptedSource 不冒充真实 provider（同 P3 demo）。

场景：
1. quiet_hours  —— 夜间提案：被推迟到静默时段结束，不投递（夜间不发）；
2. once_inbox   —— 早晨派发：本地收件箱一次入箱；同一事实重提案被
                   业务去重键抑制；
3. feedback     —— 用户 mute 话题后，该话题提案被抑制；
4. ack_lost     —— webhook 已收到请求但 ACK 丢失：delivery_unknown，
                   不盲发；对账 authoritative not-delivered 后以同一幂等
                   key 重试成功；
5. revoke       —— 撤销授权立即生效：队列中的消息被抑制，不再投递。

demo 只访问本机 loopback 与临时目录；任何断言失败以非零退出码结束。

运行：python3 examples/policy_delivery_demo.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from p4_fixtures import (  # noqa: E402
    OWNER_CHANNEL,
    ScriptedWebhookServer,
    berlin_ms,
    make_proposed_run,
    notify_proposal,
    rfc3339,
    webhook_channel,
)

from proactive_sdk import (  # noqa: E402
    ApprovalManager,
    DispatchConfig,
    FakeClock,
    FeedbackManager,
    GrantManager,
    NOTIFY_SELF_CAPABILITY,
    OutboxDispatcher,
    OwnerChannelRegistry,
    PolicyConfig,
    PolicyEngine,
    Store,
)

DAY_14 = berlin_ms("2026-10-20", 14)
NIGHT_2 = berlin_ms("2026-10-20", 2)
QUIET_END = berlin_ms("2026-10-20", 8)

QUIET_POLICY = {
    "quiet_hours": {"start": "22:00", "end": "08:00", "timezone": "Europe/Berlin"},
}


def build_stack(
    clock: FakeClock,
    *,
    delivery_policy=None,
    channels=None,
    grant_capabilities=(NOTIFY_SELF_CAPABILITY,),
    job_id="hb-1",
    task_instruction="检查来源变化，只通知本人。",
    idempotency_key="demo-job-v1",
):
    store = Store(":memory:", profile="demo", owner_destination=OWNER_CHANNEL, clock=clock)
    registry = OwnerChannelRegistry(store)
    registry.register(channel_ref=OWNER_CHANNEL, kind="local_inbox", now_ms=clock.wall_now_ms())
    for channel in channels or []:
        registry.register(now_ms=clock.wall_now_ms(), **channel)
    grants = GrantManager(store)
    grant_refs = tuple(
        grants.create(
            capability=capability,
            account_ref="account:primary",
            scope={"resource_ids": ["cal-a"]},
            consent_evidence_ref=f"consent:demo-{capability}-v1",
            now_ms=clock.wall_now_ms(),
        ).grant_id
        for capability in grant_capabilities
    )
    policy = PolicyEngine(store, channels=registry, config=PolicyConfig())
    from proactive_sdk import JobSpec

    job = JobSpec(
        job_id=job_id,
        mode="heartbeat",
        schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
        task={"instruction": task_instruction},
        grant_refs=grant_refs,
        delivery_policy=delivery_policy or {},
    )
    store.upsert_job(job, idempotency_key=idempotency_key)
    return store, registry, policy, job, grants


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def scenario_quiet_hours() -> None:
    print("1) 夜间提案：推迟到静默结束，不投递（夜间不发）")
    clock = FakeClock(wall_ms=NIGHT_2)
    store, _, policy, job, _ = build_stack(clock, delivery_policy=dict(QUIET_POLICY))
    run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-night")])
    report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
    message = store.list_outbox()[0]
    check("run 进 actions_queued", report.outcome == "actions_queued")
    check("not_before=08:00 本地时间", message["not_before_ms"] == QUIET_END)
    dispatch = asyncio.run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
    check("02:00 派发：零投递", dispatch == [] and store.list_inbox() == [])
    clock.set_wall(QUIET_END + 60_000)
    dispatch = asyncio.run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
    check("08:01 派发：恰好一次入箱",
          [r.state for r in dispatch] == ["stored_in_inbox"] and len(store.list_inbox()) == 1)
    store.close()


def scenario_once_inbox_and_dedup() -> None:
    print("2) 同一事实重复提案：业务去重键抑制，不重复通知")
    clock = FakeClock(wall_ms=DAY_14)
    store, _, policy, job, _ = build_stack(clock)
    first = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-dup", revision="1")])
    policy.apply_to_run(first, now_ms=clock.wall_now_ms())
    second = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-dup", revision="1")])
    report = policy.apply_to_run(second, now_ms=clock.wall_now_ms())
    check("第二次提案被抑制", report.suppressed == 1
          and any("duplicate_business_key" in r for r in report.reasons))
    check("outbox 只有 1 条消息", len(store.list_outbox()) == 1)
    store.close()


def scenario_feedback() -> None:
    print("3) 用户反馈 mute 话题：后续该话题提案被抑制")
    clock = FakeClock(wall_ms=DAY_14)
    store, _, policy, job, _ = build_stack(clock)
    FeedbackManager(store).record(
        kind="mute_topic", scope={"topic": "politics"}, actor="ui-admin",
        now_ms=clock.wall_now_ms(),
    )
    run_id = make_proposed_run(
        store, clock, job,
        proposals=[notify_proposal("fact-topic", arguments={"topic": "politics"})],
    )
    report = policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
    check("topic_muted 抑制", report.suppressed == 1
          and any("topic_muted" in r for r in report.reasons))
    check("outbox 为空", store.list_outbox() == [])
    store.close()


def scenario_ack_lost() -> None:
    print("4) webhook ACK 丢失：delivery_unknown 不盲发；对账后同 key 重试")
    server = ScriptedWebhookServer().start()
    try:
        clock = FakeClock(wall_ms=DAY_14)
        channel = webhook_channel(endpoint={"url": server.url, "status_url": server.status_url})
        store, _, policy, job, _ = build_stack(
            clock,
            channels=[channel],
            delivery_policy={"notification_profile": "push:demo"},
        )
        run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-push")])
        policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
        dispatcher = OutboxDispatcher(store, config=DispatchConfig(max_attempts=3))
        server.set_post_script({"drop": True})  # 服务端已处理，但连接无响应
        reports = asyncio.run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        provider_key = store.list_outbox()[0]["provider_key"]
        check("delivery_unknown（不是 failed）",
              [r.state for r in reports] == ["delivery_unknown"])
        for _ in range(5):
            clock.advance_wall(3_600_000)
            reports = asyncio.run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        check("lease 过期也不重发（服务器只收到 1 次请求）", len(server.requests) == 1)
        server.set_get_script({"status": 200, "body": {"delivered": False}})
        reports = asyncio.run(dispatcher.reconcile_unknowns(now_ms=clock.wall_now_ms()))
        check("权威 not-delivered → 重新排队",
              [r.state for r in reports] == ["pending"])
        server.set_post_script({"status": 200, "body": {"external_id": "ext-ok"}})
        reports = asyncio.run(dispatcher.dispatch_due(now_ms=clock.wall_now_ms()))
        keys = {r["headers"]["Idempotency-Key"] for r in server.requests}
        check("重试用同一幂等 key 且成功",
              [r.state for r in reports] == ["provider_accepted"] and keys == {provider_key})
        store.close()
    finally:
        server.close()


def scenario_revoke() -> None:
    print("5) 撤销授权立刻生效：队列中的消息被抑制")
    clock = FakeClock(wall_ms=DAY_14)
    store, _, policy, job, grants = build_stack(clock)
    run_id = make_proposed_run(store, clock, job, proposals=[notify_proposal("fact-revoke")])
    policy.apply_to_run(run_id, now_ms=clock.wall_now_ms())
    check("撤销前消息待发", store.list_outbox()[0]["state"] == "pending")
    for grant in grants.active(now_ms=clock.wall_now_ms()):
        grants.revoke(grant.grant_id, now_ms=clock.wall_now_ms())
    message = store.list_outbox()[0]
    check("撤销后消息立即被抑制",
          message["state"] == "suppressed" and message["reason"] == "grant_revoked")
    dispatch = asyncio.run(OutboxDispatcher(store).dispatch_due(now_ms=clock.wall_now_ms()))
    check("派发：零投递", dispatch == [] and store.list_inbox() == [])
    store.close()


def scenario_approval_freeze() -> None:
    print("6) 外部动作冻结参数审批：批准绑定 canonical request hash")
    clock = FakeClock(wall_ms=DAY_14)
    channel = webhook_channel(endpoint={})  # 批准后才会派发，demo 不真的发送
    store, _, policy, _, _ = build_stack(
        clock,
        channels=[channel],
        delivery_policy={"external_action_target": "push:demo"},
        grant_capabilities=("calendar.write",),
        job_id="task-1",
        task_instruction="在日历上创建评审事件。",
        idempotency_key="demo-job-write-v1",
    )
    ext_run = make_proposed_run(store, clock, store.get_job("task-1"), proposals=[{
        "kind": "request_external_action", "fact_id": "fact-ext", "revision": "1",
        "arguments": {"capability": "calendar.write", "action": "create_event",
                      "resource_id": "cal-a"},
        "evidence_refs": ["snapshot:snap-demo"],
    }])
    report = policy.apply_to_run(ext_run, now_ms=clock.wall_now_ms())
    check("run 进 waiting_for_approval", report.outcome == "waiting_for_approval")
    approval = store.list_approvals(state="pending")[0]
    resolved = ApprovalManager(store).resolve(
        approval.approval_id, approve=True, actor="ui-admin", now_ms=clock.wall_now_ms()
    )
    check("批准记录 authenticated actor", resolved.resolved_by == "ui-admin")
    queued = policy.promote_approved(now_ms=clock.wall_now_ms())
    check("批准后动作入队", queued == 1)
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        del tmp  # stores run in-memory; the dir only stands in for state_dir
        scenario_quiet_hours()
        scenario_once_inbox_and_dedup()
        scenario_feedback()
        scenario_ack_lost()
        scenario_revoke()
        scenario_approval_freeze()
    print("全部场景通过。")


if __name__ == "__main__":
    main()
