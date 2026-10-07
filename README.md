# Museion Agent SDK — 施工仓库

自用施工仓库（2026-10-06 整理，2026-10-07 完成 v0.1.1 与 v0.1.2 优化）。产品名与版本为 **Museion Agent SDK v0.1.2**；Python distribution/import 名称 `proactive-sdk`/`proactive_sdk`、CLI `pas` 和协议简称 PAS 暂沿用已有接口。P0–P7 已全部施工完成，`SPEC.md` §21 的 v0.1.1 九步与 §22 的 v0.1.2 十步优化也已实施；本仓库是施工现场，不是已发布的成品。

**兼容层状态（如实声明）**：Skills 兼容层完成了 88 个入口的审计一致导入（scan→sidecar→install），但端到端能力验证为 **0 个**（`e2e_verified` 均为 false，需真实授权逐项联调）。Gmail/Calendar/Webhook 通道只到传输层契约测试。不要把本包描述成"88 个能力全部实现"。详见 `docs/COMPATIBILITY.md` 与 `VALIDATION.md`。

## 阅读顺序

1. `SPEC.md`：总体设计、接口、状态机、施工阶段、验收与来源。
2. `AUDIT_AND_REUSE.md`：Muse 附件事实、88 个 Skill 的兼容缺口、可复用代码与授权边界。
3. `AGENTS.md`：交接约束。
4. `VALIDATION.md`：实际测试范围，区分 mock 与真实集成。
5. `docs/SEMANTIC_MATRIX.md`：五类任务语义、不变量与 v0.1.0 行为冻结清单
   （v0.1.1 第 1 步的产物）。
6. `docs/COMPATIBILITY.md` + `compatibility-lock.json`：锁定版本与兼容矩阵。
7. `deploy/README.md`：systemd / container / launchd 部署与备份操作。

## 仓库结构

```text
SPEC.md / AUDIT_AND_REUSE.md / AGENTS.md / VALIDATION.md
compatibility-lock.json
src/proactive_sdk/   契约与内核：contracts / pathsafe / schema_validate（P0）；
                     clock / store / scheduler / migrations（P1）；hooks（P2）；
                     model / tools / context / executor / coordinator（P3）；
                     policy / delivery（P4）；hermes / pi_worker / rpc（P5）；
                     skills / gws / net / connectors（P6）；
                     config / observability / backup / facade / daemon /
                     rpc_server / service（P7：配置、日志指标、备份、
                     公共 facade、daemon、控制面、CLI）；
                     windows / artifacts（v0.1.1：本地时窗与产物引用语法）
packages/client-ts/  JSON-RPC 控制面的 TypeScript 客户端（从 schemas 生成）
schemas/v1/          JSON Schema 2020-12 契约（10 个对象）+ 说明
examples/            参考切片与 demo：reference_core / agent_loop_demo /
                     policy_delivery_demo / skills_loop_demo / pas_app（serve 示例）
deploy/              systemd unit、Containerfile、compose、launchd 示例
tests/               标准库单元/契约测试（P0–P7）
audit/               88 个 Skill 的元数据审计与来源 hash（不含 Skill 正文）
reuse/               受限的私有提取与测试脚本
tools/               reproduce_audit / license_gate / validate_p5_real /
                     gen_client_ts / package_gate / gen_sbom / install_smoke
docs/                LICENSES / PROGRESS / COMPATIBILITY / SECURITY / CONTRIBUTING
private-vendor/      本地私有素材，不进入 git（见下）
muse-refer/          本地参考文档，不进入 git；不随 wheel 分发
```

`private-vendor/`（git 不跟踪，保留在本地）：

- `muse-sdk/` — 原始 Muse 附件解包，作为审计依据与 P6 Skill 导入的参照语料。保持原样、只读，不改动其中文件。
- `muse-reuse/` — 从原附件逐字节提取的 `hatch_hook_runtime.sh`（SHA-256 与使用说明见其 `README_PRIVATE.md`）。

## 安装与快速开始

```bash
# 从源码构建 wheel（无第三方运行时依赖）
python -m pip wheel . -w dist --no-deps
python -m pip install dist/proactive_sdk-*.whl

pas --help            # CLI 总览（serve / doctor / jobs / runs / backup …）
pas version
```

### 二十行起步（`examples/quickstart.py`）

不配任何东西就能跑：没有 API key 时用本地 stub 模型，让你先看清整个回路。

```python
import asyncio
from proactive_sdk import ProactiveAgent, OpenAICompatibleModel, Job

async def main():
    agent = ProactiveAgent(
        state_dir="./pas-state",
        model=OpenAICompatibleModel(base_url=..., api_key=..., model="gpt-4o-mini"),
        timezone="Asia/Shanghai",
    )
    grant = agent.grant("notify.self")        # 可信入口：只能由用户自己的代码调用

    agent.jobs_upsert(Job(                    # 每小时看一次，有事才说话
        id="watch-inbox", mode="task",
        schedule={"kind": "interval", "anchor": "...", "every_seconds": 3600},
        instruction="看看有没有需要我处理的事；没有就保持沉默。",
        grant_refs=(grant.grant_id,),
    ))

    await agent.tick()                        # 或 await agent.serve() 前台常驻

asyncio.run(main())
```

`model=` 会自动装配内置执行器与三个只读工具（`current_time` / `recall_memory` /
`list_recent_activity`）。其中两个需要 grant——**没授权就用不了**，这是刻意的：
便利装配不能把授权检查变成摆设。

自己加工具只要一个装饰器，schema 从类型注解推导：

```python
from proactive_sdk import tool

@tool(capability="calendar.read")
def todays_events(limit: int = 10) -> list[dict]:
    """今天的日程。"""
    return fetch(limit)
```

```bash
python3 examples/quickstart.py                  # 二十行起步
python3 examples/restart_after_three_days.py    # 关机三天后重启会发生什么
```

### 嵌入模式（自己拥有事件循环，SPEC §14.1）

```python
from proactive_sdk import ProactiveAgent, HostBridge

agent = ProactiveAgent(state_dir="./pas-state",
                       executor=HostBridge(my_host_driver),   # 把分析交给别的 agent
                       timezone="Europe/Berlin", locale="zh-CN", profile="personal")
grant = agent.create_grant_from_user_consent(          # 只能由可信 UI 调用
    capability="calendar.read", account_ref="account:primary",
    scope={"resource_ids": ["work"]}, consent_evidence_ref="consent:doc-1")
agent.jobs_upsert(Job(id="daily", mode="task",
                      schedule={"kind": "daily", "local_time": "09:00",
                                "timezone": "Europe/Berlin"},
                      instruction="总结今日日程，只通知本人。",
                      grant_refs=(grant.grant_id,)))       # 幂等 key 缺省按内容推导
await agent.tick()      # 或 await agent.serve() 前台常驻
```

daemon 模式（CLI 拥有事件循环，`--app` 指向装配模块，见 `examples/pas_app.py`）：

```bash
pas serve --app app.wiring:build_agent --config /etc/pas/pas.yaml
pas doctor --deep       # 环境/锁/socket/配置检查
pas status              # jobs/runs/outbox/health 摘要
```

## 快速验证

```bash
python3 -m unittest discover -s tests -v
python3 examples/reference_core.py
python3 examples/agent_loop_demo.py
python3 examples/policy_delivery_demo.py
python3 examples/skills_loop_demo.py
python3 examples/reminder_demo.py
python3 tools/reproduce_audit.py
python3 tools/license_gate.py
```

TypeScript / client-ts / 安装 smoke / SBOM 的检查方法见 VALIDATION.md 与
`tools/`（`install_smoke.py`、`package_gate.py`、`gen_sbom.py`）。

## v0.1.2：宿主接入、授权账本与开箱即用

- **宿主形态**：`ProactiveAgent(model=…)` 自动装配内置执行器；`HostBridge` + 驱动可接 Hermes / Pi / 任意库内对象 / 任意只接受一段文本的黑盒 CLI。
- **工具主权如实入账**：每个 run 在 `runs.tool_authority` 里记录该 run 的副作用是否受 PAS 授权约束（`pas_broker` / `host` / `unknown`），缺省是 `host`——**沉默不等于覆盖**。
- **非 job 唤醒的授权**：`agent.note_user_input(text, grant_refs=…)` 把已存在的grant 绑到这一次唤醒上。通用 `admit_event` 拿不到授权——写得了事件 ≠ 授权得了产出。
- **建议回路**：模型可以建议建立跟踪，落成待确认记录；**建议永远不会自行变成任务**，确认与拒绝都带操作者落账。
- **真实推送**：`tools/push_receiver.py` 是真人可读的真实接收端，用于验证投递链路、provider 幂等与 ACK 丢失对账。

## v0.1.1：确定时间直接提醒与可见活动

`SPEC.md` §21 的九步优化已实施。要点（每条都由 `tests/test_reminders.py`、
`tests/test_v011_semantics.py`、`tests/test_migration_v011.py` 断言）：

- **`mode="reminder"`（确定时间直接提醒）**：用户冻结正文 + 时间 + 时区 +
  owner channel + grant。到点由 store 在一个事务里完成「复核—入账—入
  owner outbox」，**零模型调用、零 run 行**，不经 AgentExecutor。
- **`obligation`（通知义务）**：`due`（到点即有义务，延后必须可见）与
  `opportunistic`。只由可信任务配置赋予；模型 proposal、Skill 文本与
  外部来源不能自报升级。
- **迟到与错过**：计划时间、实际时间、迟到毫秒与「仅已知原因」分别记录；
  超出 grace 或不补发时保留可查询原因，不静默丢弃。
- **时效来源刷新**：只有声明了 `refresh_sources` 的提醒才在投递前定向
  复读；来源不可用时记可恢复失败/延后，授权撤销或事实被取消则抑制——
  不凭旧快照硬发。纯冻结提醒不做任何来源请求。
- **近 24 小时语义查重**：`ContextPack.recent_notifications` 提供有界、
  脱敏（无正文）的已发送摘要；硬去重仍是 `business_key`/`delivery_key`。
- **活动视图**：`job_activity` 投影把执行结果与通知结果分开呈现，静默时
  仍能看到原因；`pas jobs activity` / `pas activity` 可读。
- **停止追踪**：`pas jobs delete` 对有审计历史的 job 只停止追踪（保留
  全部账目）并返回 `action=stopped`；`pas jobs stop` 是显式入口。
- **cadence 与产物引用**：`warm/balanced/gentle` 只是宿主偏好，SDK 不带
  任何小时数默认值；`artifact:<相对路径>` 引用必须可打开且出现在正文里，
  绝对路径/机器本地路径被明确拒绝。
- **数据生命周期**：新增的 `job_occurrences` / `job_activity` 一并纳入
  export 与 `delete-data`；清除按外键图先子后父并在提交前做
  `foreign_key_check` 校验（v0.1.1 修正了原先会静默残留 `events`/`jobs`
  的删除顺序缺陷）。

```bash
python3 examples/reminder_demo.py     # 0 模型调用 / 0 run / 1 条投递
pas jobs create standup --mode reminder --schedule-json '{"kind":"runonce","at":"2026-11-01T09:00:00Z"}' \
  --reminder-json '{"body":"10:00 站会","timezone":"Europe/Berlin"}' --grant-ref <grant_id>
pas jobs activity standup             # 做了什么、为何保持安静
```

## 数据安全（备份 / 恢复 / 删除）

```bash
pas backup /var/backups/pas-$(date +%F).bin          # 在线备份（0600 + sha256 sidecar）
pas restore /var/backups/pas-2026-10-07.bin --yes    # 校验 identity/schema 后原子替换
pas export dump.json                                 # 逻辑导出（JSON）
pas delete-data --yes                                # 清空 profile 数据（不可逆）
```

## 分发与许可

项目当前为自用，原始素材仅存在于本地 `private-vendor/`，构建产物由
`tools/package_gate.py` 对 wheel 本体做路径/hash/密钥扫描（不只扫 git
tracked files）。若日后转为公开发布：恢复 AGENTS.md 的分发约束，执行
SPEC P0/P7 门禁（license gate、SBOM、隐私扫描），且不得直接公开 Muse
原文件；第三方代码再分发授权核验仍是一项未完成的前置义务。

Museion Agent SDK v0.1.1 是本仓库的自用版本标识，不代表已在 PyPI/npm 发布或已完成真实用户流程回归。
