# V3 实现记录

日期：2026-10-18（America/Los_Angeles）。本轮按用户明确指令先补各阶段代码，禁止运行测试；计划中的测试门全部待验收，不记为通过。

## Baseline

- 唯一代码基线：`fdbebdc76fb709129890b91da44dc5e5d3197686`。
- 基线提交：`fix: recover interrupted delivery and preserve failed analysis deltas`。
- 基线版本：Python 包 `0.1.3`、数据库 schema `14`；新增迁移后目标 schema `17`，没有改写旧迁移。
- 本机读取环境：Darwin arm64、Python 3.14.4；没有据此声称 Python 3.11 兼容性已验证。
- 开工时 tracked 工作区无差异，只有未跟踪的 `Museion PAS 主动层 v3 改进计划.md`；该计划原文保留。
- `tests/test_review_recovery.py` 原有内容保留；没有创建或运行行为测试。

## 各阶段代码与可观察结果

|阶段|实现入口|待验收的可观察结果|
|---|---|---|
|P0|本记录及现有装配链|固定上述代码基线；测试基线未执行|
|W1|`contracts.py`、`context.py`、`coordinator.py`、`facade.py`|候选门之后读取异步 `ConversationPort`；默认 12 小时 / 12 轮 / 每轮 500 字 / 总计 4000 字 / 2 秒；按时序截断，失败返回空并只记录一次计数事件；`conversation:<turn_id>` 进入本轮证据集合；持久化 pack 只保留 ID 和正文 hash 前缀；pending 摘要最多 20 条且区分 `delivery_unknown`|
|W2|迁移 015、`store.py`、决策解析、可信 facade/CLI|按 run/proposal/content hash 幂等收集候选，不同有效 ref 达两条才转为 `confirmed_twice`；保留旧 confidence；TTL 默认 30 天、容量继续 256，到期候选正文在巩固或现有 tick/daemon 维护时清除，列表只读投影会隐藏过期正文并保留来源 hash/ref；forget 阻止旧候选再转正，可信新记忆可解锁；自动巩固在决策提交后运行|
|W3|迁移 016、`store.py`、可信 facade/CLI|目标与追加记录；active/completed/archived；事实性 progress/stale；新建或变更绑定要求 active；旧绑定缺失或非 active 仍按真实状态进入有界摘要；目标操作不触碰 outbox 或级联删除 job|
|W4a|迁移 017、`coordinator.py`、`run_stages.py`|仅 task 且 policy/job 双开关开启 worker→compose；worker 只接受 silent/internal_record；报告、工具证据来源/hash、产物 hash 和阶段输出持久化；compose 仍经决策与投递策略；两段共用原 run 的模型/工具/截止预算|
|W4b|同上及 `executor.py`、`host_bridge.py`、`model.py`|固定原会话 cutoff，比对完整输入身份、权限、executor identity、上阶段输出及本地产物；匹配后从下一阶段开始；跨 claim 累计最多两次恢复，超限 failed；proposed 标记与 proposal/cursor 提交同事务；不改变 delivery_unknown 重试规则|
|P5|Python/JSON Schema/生成 TS、示例、契约 fixture|公开字段与 RPC 同步；补充离线宿主接缝与示例数据；行为验收、旧库升级和故障注入均未执行|

## 接入与开关

配置值使用现有 YAML 布尔语法 `true/false`；以下新行为默认全部关闭：

```yaml
policy:
  conversation_context: false
  memory_consolidation: false
  goal_context: false
  handoff: false
  run_resume: false
  max_resumes: 2
  goal_stale_days: 14
  memory_ttl_days: 30
```

会话读取预算通过 `CoordinatorConfig` 调整。宿主通过 `ProactiveAgent(conversation=...)` 注入已按当前 profile/用户/任务允许范围过滤的只读端口；`examples/scoped_conversation.py` 不接真实账号。记忆巩固使用 profile 的 SQLite 记忆，调用方若替换 MemoryPort，须让它读取同一个 profile 存储。

可信 CLI 增加 `memory list|candidates|forget|consolidate` 和 `goals list|show|create|update|progress|archive`；`goals update --status active` 可恢复归档。`jobs create --goal-id ... --handoff` 表达显式绑定与 task handoff 意图。RPC 只新增 `memory.list`、`memory.candidates`、`goals.list`、`goals.get`；没有给模型新增记忆/目标写工具。

跨进程恢复要求 executor 提供稳定 `checkpoint_identity`。内置 OpenAI-compatible 模型及工具循环提供配置身份 hash；HostBridge 接受宿主明确提供的版本/模型/工具配置身份，宿主配置变化必须更新它。自定义 executor 的阶段提交 key 需区分 run、stage 和 attempt；内置 HostBridge 已处理。缺少身份时显式开启的恢复路径拒绝执行，避免复用无法识别的模型结果。

`artifact:` 引用在 facade 的 profile `artifacts/` 目录下核验；每项限 16 MiB、每份报告最多 32 条。宿主产物须放在此受控目录，缺失、越界或 hash 改变拒绝复用。checkpoint 不复制会话输入原文；模型生成的报告/proposal 可能引用用户内容，继续受既有输出处理边界约束。恢复只跳过已持久化完成的阶段，不承诺模型调用 exactly-once。

## 实际检查与未完成验收

执行了代码/调用关系读取、baseline 与工作区核对、JSON Schema→TypeScript 代码生成、最终 diff 静态检查。没有执行 unit/contract、构建、lint、迁移演练、故障注入、远端模型或投递调用。`tests/fixtures/v3_context.json` 是未执行的契约样例，不能作为通过证据。

实现完成不代表 V3 计划的完成定义已满足：空库与 v14 升级、旧行为回归、开关关闭语义、阶段恢复、真实宿主过滤及投递结果均仍待实际验收。未改运行中数据库，未部署、提交、推送或发布；关闭开关不回滚 schema 或此前已写入的数据。

## 轻量记忆排序补充（2026-10-19）

会话注入链路已存在：`ProactiveAgent(conversation=...)` → `ConversationPort` → coordinator 有界读取 → `ContextPack` → 模型输入。宿主提供当前 profile/任务授权范围内的片段；具体宿主自动采集属于 port 的实现，不应把它与已有的会话注入能力混为一谈。

`ContextPackBuilder` 现在用本轮任务指令、已注入会话和 goal summary，在最多 256 条有效记忆上按词项重合排序；英文大小写归一，中文使用相邻二元组，稀有词权重更高。同分优先较新条目，选入最多 32 条；查询为空或完全无匹配时保持原来的最近 32 条及顺序。仅作词面检索，不处理同义词或跨语言语义匹配。

实现使用标准库，未导入上游源码、增加依赖或变更 MemoryPort/持久化契约。继续通过原端口读取，保留 SQLite 的 TTL/forget 过滤、memory.read 权限门及原有 evidence_refs/confidence。新增 3 个行为用例，覆盖旧中英文相关记忆入选及失效/遗忘过滤、稀有词权重与同分顺序、无匹配回退。本段所述“仅静态检查”只描述当时阶段；下方验收记录已运行这些用例。

## 验收记录（2026-10-19，v0.1.4）

- 测试修正：`tests/test_rpc.py` 冻结方法表 26→30（V3 新增 4 个 RPC 方法）；`examples/agent_loop_demo.py` 的空来源首轮改为带真实候选、`notify_self` 补 `reason`；`examples/skills_loop_demo.py` 的 mail 提案补 `reason`。两个示例在干净 `HEAD`（v0.1.3）上失败相同，属遗留 fixture 陈旧，不是 V3 回归；修后 4 个示例全部通过。
- 新增用例：`tests/test_migration_v014.py` 覆盖 schema 14→17 升级（旧 run 保留、`context_ref` 非空的 run 标记 `stage=proposed`、新表可写、重开不重复迁移）；`tests/test_v3_switch_off.py` 覆盖默认关闭路径（不读会话、不注入 goal、不走 handoff、不采集候选）与 `run_resume` 缺 `checkpoint_identity` 时 fail closed。
- 全量结果：隔离 Linux 测试机（Trism，Python 3.12.3，全新 venv）978 项 — 20 error（无 bubblewrap 环境）、1 失败（`test_thousand_jobs_scan_is_bounded` 单跑 41 s > 30 s，慢主机性能守卫）、9 skip，其余通过；除上述环境项外没有失败。
- 真实宿主 e2e：Hermes gateway 与 Pi 1.0.4 各 16/16 断言通过，各 2 次真实模型调用（`pricing_basis=measured`），schema 17；首 tick 新队列按设计不投递，下一 tick 入本地 inbox；证据保留于 `artifacts/v3-e2e-20261019T073507Z/`。
- 发布物（本地，未发布）：`release/v0.1.4/` 构建成功；wheel `proactive_sdk-0.1.4-py3-none-any.whl` 327710 字节；`package_gate` PASS；SBOM 生成；`install_smoke` PASS（`pas version` 报 v0.1.4）。
- 仍未验收：跨进程恢复成功路径、开启开关下的故障注入、host-side activity probe、Python 3.11 的 v3 全量套件；当前工作区尚未提交，也没有 tag 或发布。

## 增量：A5 对照改造（2026-10-25）

依据 A5 0.3.1 拆解（工作目录 `A5-主动性拆解-20261025/`，未入库）修订后的三项改造，落在 v3 之后：

- **P2 运行层到期时间（migration 018，schema 18）**：`runs.next_eligible_at_ms` 只在两类有诚实墙钟答案的抑制时写入——`quiet_window`（`quiet_end_ms` 窗口终点）与 `user_active`（last_activity + `idle_min_minutes`）；其余 9 类 silence_reason 一律 NULL，不造数。字段只读派生，判定逻辑不读回。`RunReport`、`facade.tick` 报告、`runs.get/list`、`store.get_run` 均透出。投递层既有 `not_before_ms` 补进 pending 通知摘要（pack 校验与 schema 同步允许可选字段）。
- **P3b 反馈上下文注入**：新增 `policy.feedback_context`（默认 off，config/coordinator/装配全链透传）。开启时每次组上下文注入最近至多 5 条投递反馈的脱敏投影（fact_digest 16hex + topic ≤120 + signal 四值枚举 + at_ms；无 message_id、无正文），进 `ContextPack.delivery_feedback` 与持久化 pack；账本记 `feedback_context` 事件；不进 evidence universe、不扩授权。`store.recent_delivery_feedback()` 为唯一取数口。
- **P3a 无代码改动**：`ignored_24h` 宿主回填自 v2 W6 已存在（`store.backfill_ignored_24h` + daemon tick，开关 `policy.feedback_ignored_backfill` 默认 off）。部署侧开开关即可。

测试：新增 `tests/test_a5_followup.py` 6 个行为用例（P2 三项、P3b 注入开关与红线、契约校验）；`test_migration_v014` schema 断言 17→18；TS 类型再生成。基线 983 全绿 → 改动后 989 全绿（本机 Python 3.14.4）。合成验收 20/20（离线，脚本与记录在 A5 工作目录）：记忆巩固单/双证据→转正→recall 注入；静默窗到期字段及到点复跑；回填幂等与 delivery_attempts 联查；注入脱敏与无联查键；投递层 not_before 透出。未做：真实模型 token 对比、木木实例部署与 schema 17→18 升级演练、P1 人工抽查项。
