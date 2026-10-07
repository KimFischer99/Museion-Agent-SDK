"""Skills 兼容闭环 demo（P6 / SPEC §15.1 P6 行，§11.5 首版范围）。

打通 §11.5 要求的四条真实链路——邮件读取、日历读取、公开资料跟踪、
本人通知——全部落在 PAS 记账上：

1. import     —— legacy importer 扫描 Muse 目录（private-vendor 存在时
                 用真实 88 入口语料并与 audit/skills.json 断言一致；缺席
                 时用内置 mini fixture，显式注明），写 skill_installs；
2. mail       —— GmailMailSource（scripted GwsConnector，标注 fake）→
                 coordinator L0→L1 → notify_self 提案 → 策略 → outbox →
                 本地 inbox 一次入箱（本人通知闭环）；
3. calendar   —— CalendarAgendaSource 心跳：内容未变 → suppressed，
                 零模型调用；
4. material   —— PublicMaterialSource（EgressBroker 子类 stub，仅演示
                 修订语义；真实抓取路径见 tests/test_net.py 的回环 TLS）：
                 内容变化才产出新 revision 提案。

授权边界：GwsConnector 为显式声明的脚本替身；未连接语义、envelope、
记账与真实实现共用同一代码路径。demo 不访问真实账户，delivered 只经
本地 inbox（无外发）。

运行：python3 examples/skills_loop_demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from p3_fixtures import ScriptedModel, T0, make_broker, make_executor, rfc3339  # noqa: E402

from proactive_sdk import (  # noqa: E402
    ContextPackBuilder,
    EphemeralMemoryPort,
    JobSpec,
    NOTIFY_SELF_CAPABILITY,
    ProactiveCoordinator,
    PolicyEngine,
    SourceRegistry,
    Store,
)
from proactive_sdk.connectors import CalendarAgendaSource, GmailMailSource, PublicMaterialSource  # noqa: E402
from proactive_sdk.gws import GwsAdapter, GwsConnector  # noqa: E402
from proactive_sdk.net import EgressBroker  # noqa: E402
from proactive_sdk.skills import LegacySkillImporter, audit_consistency  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
INSTRUCTION = "检查跟踪的邮件、日历与公开资料，有值得本人知道的变化时只通知本人。"


# --------------------------------------------------------------------------- #
# 1. Import lane
# --------------------------------------------------------------------------- #


def _mini_fixture() -> Path:
    tmp = Path(tempfile.mkdtemp())
    gmail = tmp / "skills" / "gmail"
    gmail.mkdir(parents=True)
    (gmail / "SKILL.md").write_text(
        '---\nname: "gmail"\ndescription: "Work with mail."\n'
        'metadata: { "includeInPrompt": true }\n---\nhatch_gws_cli gmail status\n',
        encoding="utf-8",
    )
    cal = tmp / "skills" / "google-calendar"
    cal.mkdir(parents=True)
    (cal / "SKILL.md").write_text(
        '---\nname: "google_calendar"\ndescription: "Calendar ops."\n'
        'metadata: { "includeInPrompt": false }\n---\nhatch_gws_cli calendar status\n',
        encoding="utf-8",
    )
    return tmp


def scenario_import(store: Store) -> dict:
    vendor = REPO / "private-vendor" / "muse-sdk"
    if vendor.is_dir():
        importer = LegacySkillImporter(root=vendor)
        skills = importer.scan()
        report = importer.report(skills)
        consistency = audit_consistency(report, json.loads((REPO / "audit" / "skills.json").read_text()))
        source_label = "private-vendor/muse-sdk (real 88-entry corpus)"
        assert consistency["match"], consistency
    else:
        importer = LegacySkillImporter(root=_mini_fixture())
        skills = importer.scan()
        report = importer.report(skills)
        consistency = {"match": None, "note": "audit corpus not present on this machine"}
        source_label = "mini fixture (private-vendor absent; 88-entry audit run skipped, not faked)"
    for skill in skills:
        if skill.canonical_name in ("gmail", "google-calendar"):
            store.record_skill_install(
                canonical_name=skill.canonical_name,
                source_hash=skill.sha256,
                sidecar=skill.sidecar(),
                technical_status="parsed",
                distribution_status="permission_unverified",
                now_ms=T0,
            )
    install = store.get_skill_install("google-calendar")
    assert install is not None
    assert install["sidecar"]["requirements"]["capabilities"] == ["calendar.read"]
    return {
        "scenario": "import",
        "source": source_label,
        "count": report["count"],
        "issue_counts": report["issue_counts"],
        "audit_match": consistency["match"],
        "stored_installs": len(store.list_skill_installs()),
        "google_calendar_grants_required": install["sidecar"]["requirements"]["grants"],
    }


# --------------------------------------------------------------------------- #
# Provider stub (labeled fake) behind the real GwsAdapter grammar
# --------------------------------------------------------------------------- #


class ScriptedGwsConnector(GwsConnector):
    """Labeled test double; the adapter/connector contract is the real one."""

    def __init__(self) -> None:
        self.replies: dict[tuple[str, tuple[str, ...]], dict] = {}

    def call(self, service: str, args: list[str]) -> dict:
        return self.replies[(service, tuple(args))]


def _connected(service: str) -> dict:
    return {"connected": True, "accounts": [{"account_id": "a1", "display_name": "Owner"}]}


# --------------------------------------------------------------------------- #
# 2-4. Mail / calendar / material lanes through the coordinator
# --------------------------------------------------------------------------- #


def _decision(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False)


async def scenario_mail(store: Store) -> dict:
    gws = ScriptedGwsConnector()
    gws.replies[("gmail", ("status",))] = _connected("gmail")
    gws.replies[
        ("gmail", ("+triage", "--query", "is:unread newer_than:1d", "--max", "20", "--format", "json"))
    ] = {
        "messages": [
            {"id": "m-1", "from": "bank@example.com", "subject": "New statement",
             "snippet": "Your October statement is ready."}
        ]
    }
    adapter = GwsAdapter(connector=gws)
    source = GmailMailSource(adapter=adapter, account_ref="account:primary")
    model = ScriptedModel(
        [
            {"tool_calls": [("c1", "read_evidence", {"fact_id": "gmail:m-1"})]},
            {
                "content": _decision(
                    {
                        "decision": "propose",
                        "summary": "有一封新的银行对账单邮件",
                        "proposals": [
                            {
                                "kind": "notify_self",
                                "fact_id": "gmail:m-1",
                                "revision": "sha256:" + "0" * 32,
                                "body": "bank@example.com 发来新对账单（摘要按用户 locale 生成，"
                                        "覆盖 gmail skill 的固定英文输出假设）。",
                                "evidence_refs": ["tool:read_evidence:c1"],
                                "expires_at": rfc3339(T0 + 3_600_000),
                            }
                        ],
                    }
                )
            },
        ]
    )
    report, _ = await _run_lane(store, "mail-watch", source, model, capability="gmail.read")
    inbox = store.list_inbox()
    return {
        "scenario": "mail_loop",
        "outcome": report.outcome,
        "policy_outcome": report.policy_outcome,
        "inbox_entries": len(inbox),
        "locale_note": "summary generated in user locale, not the skill's fixed English",
    }


async def scenario_calendar(store: Store) -> dict:
    gws = ScriptedGwsConnector()
    gws.replies[("calendar", ("status",))] = _connected("calendar")
    gws.replies[
        ("calendar", ("+agenda", "--days", "1", "--format", "json"))
    ] = {"events": [{"id": "e-1", "summary": "Standup", "start": "09:00", "end": "09:30"}]}
    adapter = GwsAdapter(connector=gws)
    source = CalendarAgendaSource(adapter=adapter, account_ref="account:primary")
    model = ScriptedModel(
        [
            {
                "content": _decision(
                    {"decision": "silent", "summary": "今日日程无值得打扰的变化", "proposals": []}
                )
            }
        ]
    )
    report, _ = await _run_lane(store, "calendar-heartbeat", source, model, capability="calendar.read")
    # Tick 2 with identical content: L0 sees no change → suppressed, zero LLM.
    report2, _ = await _run_lane(store, "calendar-heartbeat", source, model, capability="calendar.read",
                                 second_tick=True)
    return {
        "scenario": "calendar_loop",
        "tick1_outcome": report.outcome,
        "tick2_outcome": report2.outcome,
        "tick2_reason": report2.reason,
        "model_calls_total": len(model.calls),  # tick1 only; tick2 is zero-LLM
    }


async def scenario_material(store: Store) -> dict:
    class StubBroker(EgressBroker):
        def __init__(self) -> None:
            super().__init__(allowed_hosts=("example.com",))
            self.bodies = ["<feed>version 1</feed>"]

        def fetch_text(self, url: str) -> str:
            return self.bodies.pop(0)

    broker = StubBroker()
    source = PublicMaterialSource(
        account_ref="account:primary", broker=broker, url="https://example.com/feed"
    )
    model = ScriptedModel(
        [
            {
                "content": _decision(
                    {
                        "decision": "propose",
                        "summary": "跟踪的公开资料有更新",
                        "proposals": [
                            {
                                "kind": "internal_record",
                                "fact_id": "material:" + "a" * 24,
                                "revision": "sha256:" + "1" * 32,
                            }
                        ],
                    }
                )
            }
        ]
    )
    report, registry = await _run_lane(
        store, "material-watch", source, model, capability="public.read"
    )
    broker.bodies = ["<feed>version 1</feed>"]  # unchanged: empty delta
    report2, _ = await _run_lane(store, "material-watch", source, model, capability="public.read",
                                 registry_override=registry, second_tick=True)
    return {
        "scenario": "material_loop",
        "tick1_outcome": report.outcome,
        "tick2_outcome": report2.outcome,
        "tick2_reason": report2.reason,
    }


def _grants_for(capability: str | None, store: Store, now_ms: int):
    from proactive_sdk import NOTIFY_SELF_CAPABILITY, GrantManager

    grants = GrantManager(store)
    capabilities = [NOTIFY_SELF_CAPABILITY]
    if capability is not None:
        capabilities.append(capability)
    return [
        grants.create(
            capability=cap,
            account_ref="account:primary",
            scope={"resource_ids": ["primary"]},
            consent_evidence_ref=f"consent:demo-{cap}",
            now_ms=now_ms,
        ).grant_id
        for cap in capabilities
    ]


def _lane_broker(capability: str | None):
    """Broker for one lane; the evidence tool requires exactly the lane's
    granted capability (the P3 fixture hardcodes calendar.read)."""
    from proactive_sdk.tools import LocalToolBroker, ToolSpec

    async def read_evidence(arguments):
        return f"evidence for {arguments.get('fact_id', '?')}"

    broker = LocalToolBroker(capabilities=frozenset((capability,) if capability else ()))
    if capability:
        broker.register(
            ToolSpec(
                name="read_evidence",
                description="Read one stored fact (read-only demo tool).",
                parameters={"properties": {"fact_id": {"type": "string"}}, "required": ["fact_id"]},
                required_capability=capability,
            ),
            read_evidence,
        )
    return broker


async def _run_lane(store: Store, job_id: str, source, model, *, capability, registry_override=None,
                    second_tick=False):
    from proactive_sdk import OutboxDispatcher, OwnerChannelRegistry, PolicyConfig

    registry = registry_override or SourceRegistry()
    if registry_override is None:
        registry.register(
            source_id=source.source_id,
            account_ref=source.account_ref,
            source=source,
            required_capability=capability or "public.read",
        )
    broker = _lane_broker(capability)
    executor = make_executor(model, broker=broker)
    builder = ContextPackBuilder(locale="zh-CN", timezone="Europe/Berlin", memory=EphemeralMemoryPort())
    channels = OwnerChannelRegistry(store)
    channels.register(channel_ref="local-inbox:demo", kind="local_inbox", now_ms=T0)
    policy = PolicyEngine(store, channels=channels, config=PolicyConfig())
    grants = _grants_for(capability, store, T0)
    job = JobSpec(
        job_id=job_id,
        mode="heartbeat",
        schedule={"kind": "interval", "anchor": "2025-10-09T00:00:00Z", "every_seconds": 1800},
        task={"instruction": INSTRUCTION},
        grant_refs=tuple(grants),
    )
    store.upsert_job(job, idempotency_key=f"demo-{job_id}-v1")
    slot = T0 if not second_tick else T0 + 1_800_000
    store.admit_event(
        f"job:{job_id}:1:{slot}",
        origin="scheduler",
        payload={"job_id": job_id, "mode": "heartbeat"},
        observed_at_ms=slot,
        expires_at_ms=slot + 7 * 24 * 3600 * 1000,
        job_id=job_id,
        job_revision=1,
    )
    coordinator = ProactiveCoordinator(
        store, registry=registry, pack_builder=builder, executor=executor, policy_engine=policy
    )
    report = await coordinator.process_pending_run()
    # 本人通知闭环的最后一跳：outbox → 本地 inbox（P4 dispatcher）。
    dispatcher = OutboxDispatcher(store, policy=policy)
    await dispatcher.dispatch_due(now_ms=T0 + 60_000)
    return report, registry


async def _run_all() -> list[dict]:
    from proactive_sdk import FakeClock

    # 与 fixtures 的 T0 对齐的时钟：expires_at/quota 等策略判定可复现。
    store = Store(
        ":memory:", profile="demo", owner_destination="local-inbox:demo",
        clock=FakeClock(wall_ms=T0),
    )
    results = [scenario_import(store)]
    results.append(await scenario_mail(store))
    results.append(await scenario_calendar(store))
    results.append(await scenario_material(store))
    store.close()
    return results


def main() -> int:
    results = asyncio.run(_run_all())
    for result in results:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(
        json.dumps(
            {
                "boundary": "GwsConnector 为显式标注的脚本替身；未连接/授权语义走真实代码路径；"
                "本人通知经本地 inbox，无外发；88 入口审计断言仅在 private-vendor 存在时执行",
                "delivered_external": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
