# 语义矩阵（SPEC §21.1 第 1 步）

本文档冻结 v0.1.1 的五类任务语义，作为实现与测试的共同基线。矩阵只描述
**机制**，不描述任何宿主产品、模型供应商或某一行为参考文档的后端实现。

## 1. 五类语义

| 语义 | 触发来源 | schedule | 分析（模型调用） | 通知义务 | 取消/暂停语义 |
| --- | --- | --- | --- | --- | --- |
| 用户日程（外部日历） | 外部日历来源（`Source`），由宿主或连接器提供 | 机会型 heartbeat 或显式 task | heartbeat 需来源变化才调用模型；task 按任务语义调用 | `opportunistic`：按 §10 策略决定是否通知 | 暂停 job、撤销 grant、静音话题、来源不可用 |
| 确定时间直接提醒 | 用户冻结的 job 定义（可信 `jobs_upsert`） | `runonce` / `daily` / `weekly` / `monthly` / `interval` | **零模型调用**（不经 `AgentExecutor`） | `due`：到点即有通知义务 | 暂停（暂停期间不补发）、删除（停止追踪但保留审计）、mute、grant 撤销 |
| 一次性判断 | 显式 task job 或 manual trigger | `runonce` 或一次性 task | 按任务语义调用（不因无来源而跳过） | `opportunistic` | 暂停/取消 run |
| 持续跟踪 | 长时间存活的 task/heartbeat job | `interval` | heartbeat：来源变化才调用；task：按任务语义 | `opportunistic` | 暂停（保留游标）、停止追踪（保留审计） |
| 机会型 heartbeat | 周期唤醒 | `interval`（默认 `coalesce_latest`） | **无信号时零模型调用**（L0 硬门禁） | `opportunistic` | 暂停、静音、降噪偏好 |

## 2. 不变量

1. **单 owner**：一个任务只有一个 scheduler owner。`jobs.scheduler_owner ∈
   {pas, host}`；`due_job_ids()` 只返回 `pas` 且 `enabled=1` 的 job。
   宿主接管时必须显式切换 ownership，PAS 不再为同一目标启动定时器。
2. **四本账分离**：唤醒（`events`/`job_occurrences`）、分析完成
   （`runs`/`run_events`/`context_packs`）、动作入队（`actions`/`outbox`）、
   通知投递（`delivery_attempts`/`inbox`）各自记账，互不折叠。
3. **零模型调用是硬门禁**：机会型 heartbeat 在 L0 判定为「无来源 / 无变化 /
   未授权」时以 `suppressed` 结束，`model_turns` 必须为 0。直接提醒根本不进入
   该循环。
4. **模型输出不授予权限**：task 与通知优先级只能来自可信入口
   （`jobs_upsert`、`GrantManager`、`OwnerChannelRegistry`）。proposal、
   Skill 文本、来源正文都不能自报升级为 `due` 义务或新目的地。
5. **业务键硬去重**：`profile + goal + fact + revision + destination + kind`
   （`actions.business_key` / `outbox.delivery_key` 双重 UNIQUE）。
   语义查重（近 24 小时摘要）只降低重复概率，不替代硬去重。
6. **状态 lease ≠ 旧 worker 已停止**：副作用每次执行前重新检查
   fence / grant / mute / cancel。
7. **`delivery_unknown` 不盲重试**：只有 provider 权威答复或真实回执才推进。

## 3. v0.1.0 行为冻结（不得回归）

以下行为由 `tests/test_semantics_matrix.py` 固定：

- heartbeat 无来源 → `suppressed` / `l0_nothing_to_check`，模型调用 0。
- heartbeat 来源无变化 → `suppressed` / `l0_no_source_change`，模型调用 0。
- heartbeat 来源未授权 → `suppressed` / `l0_source_unauthorized`。
- 显式 task 有来源但全部失败 → `failed` + `provider_unavailable`（不是静默抑制）。
- 显式 task 无来源 → 仍然运行（纯推理）。
- `host` owner 的 job 不被 PAS 调度。
- 默认 misfire policy：heartbeat → `coalesce_latest`，task → `grace_once`。
- 同一 occurrence 重复接纳幂等，不产生第二个 event / run。
- 既有 `task` / `heartbeat` 路径的默认值不因新增 `reminder` 模式而改变。

## 4. v0.1.1 新增语义（已实现）

| 项 | 语义 |
| --- | --- |
| `mode="reminder"` | 冻结正文 + 时间 + 时区 + owner channel + grant；到点直接入 owner outbox；零模型调用 |
| `obligation` | `due`（到点义务，延后必须可见）vs `opportunistic`（可抑制） |
| 迟到 | 计划时间/实际时间/仅已知原因分别记录；超窗不可补发时保留可查询原因 |
| 时效来源 | 投递前复核取消/完成/已处理/freshness；来源不可用 → 可恢复失败/延后 |
| 近 24h 摘要 | 机会型任务的 `ContextPack.recent_notifications`，有界、脱敏 |
| 活动视图 | `job_activity` 投影：执行结果与通知结果分别可见 |
| cadence | `warm/balanced/gentle` 仅影响机会型节奏，不突破授权/免打扰/去重 |
| artifact refs | 通知要求用户打开产物时，正文必须携带有效引用；缺失/不可解析被明确拒绝 |

第 4 节各行的实现与断言位置：

| 项 | 实现 | 断言 |
| --- | --- | --- |
| `mode="reminder"` | `contracts.validate_reminder`、`store.admit_reminder_occurrence`、`Scheduler._admit_reminder`、迁移 `m007` | `tests/test_reminders.py` |
| `obligation` | `contracts.JobSpec.effective_obligation`、`policy.PolicyEngine` | `tests/test_v011_semantics.py::ObligationTests` |
| 迟到/错过 | `store.admit_reminder_occurrence`、`record_missed_reminder` | `tests/test_reminders.py`、`test_v011_semantics.py` |
| 时效来源 | `coordinator._targeted_entries`、`delivery.OutboxDispatcher._pre_delivery_refresh` | `tests/test_v011_semantics.py::PreDeliveryRefreshTests` |
| 近 24h 摘要 | `contracts.RecentNotification`、`store.recent_sent_notifications`、`context.render_recent_notifications` | `tests/test_v011_semantics.py::RecentNotificationTests` |
| 活动视图 | `store.job_activity`、`facade.activity_list`、CLI/RPC | `tests/test_v011_semantics.py::ActivityProjectionTests` |
| cadence | `PolicyConfig.cadence_*`、`config.PolicySection` | `tests/test_v011_semantics.py::CadenceTests` |
| artifact refs | `proactive_sdk.artifacts`、`policy.PolicyEngine._artifact_refs` | `tests/test_v011_semantics.py::ArtifactRefTests` |
| 迁移 | `m007_p011_reminders.sql` + `store._migrate` 的 FK 关闭/校验 | `tests/test_migration_v011.py` |
