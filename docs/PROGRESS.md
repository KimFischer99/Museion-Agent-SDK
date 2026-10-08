# Museion Agent SDK v0.1.2 施工进度

按 SPEC §15.1 阶段推进；每阶段完成后在此登记，汇报格式遵循 AGENTS.md。
"完成"以该阶段验收条件全部通过为准，不以代码写完为准。

| 阶段 | 状态 | 完成日期 | 说明 |
|---|---|---|---|
| P0 边界与来源 | **done** | 2026-10-06 | contracts/schema、许可清单与门禁、审计复现命令、zip-slip 防护、单 profile 边界；详见下 |
| P1 持久化与时钟 | **done** | 2026-10-06 | store、migrations、Clock、五类 schedule、misfire、claim/fencing、jobs API；验收测试见下 |
| P2 Hooks 与事件 | **done** | 2026-10-06 | 沙盒 runner、legacy parser、staging + CAS、hook 状态机；验收测试见下 |
| P3 独立 Agent 闭环 | **done** | 2026-10-06 | Source/Memory ports、ContextPack、ToolLoopExecutor、OpenAI 兼容 ModelPort、L0/L1 coordinator；验收测试见下 |
| P4 策略与投递 | **done** | 2026-10-06 | grants、冻结参数审批、owner channels、outbox 派发（attempt journal/幂等 key/unknown 对账）、本人 inbox、真实 webhook 通知 sink、feedback；验收测试见下 |
| P5 宿主适配 | **done** | 2026-10-07 | Hermes Runs executor、Pi worker 桥、JSON-RPC 2.0 控制面协议 + client-ts 生成、Hermes proactive 插件；锁定版本真实服务验证 8/8 PASS；验收测试见下 |
| P6 Skills 能力 | **done** | 2026-10-07 | legacy importer（88/88 审计一致）、canonical id/aliases、依赖闭包、sidecar、GWS 受限 grammar + 邮件/日历连接器、出站 broker、公开资料跟踪、四链路闭环 demo、Pi extension；验收测试见下 |
| P7 产品化与发布 | **done** | 2026-10-07 | 公共 facade（ProactiveAgent）、常驻 daemon（单实例锁/恢复/drain 停机/磁盘满报警/health.json）、Unix socket 控制面 + 认证、CLI（pas serve/doctor/jobs/…）、备份恢复/导出/删除、结构化日志与指标、systemd/container/launchd 示例、wheel 构建产物门禁 + SBOM、兼容矩阵；安装 smoke PASS；验收测试见下 |
| v0.1.1 优化（SPEC §21） | **done** | 2026-10-07 | 确定时间直接提醒（`mode=reminder`，零模型调用）、通知义务 vs 机会型分流、迟到/错过诚实语义、投递前时效来源复读、近 24h 语义查重摘要、可见活动投影与停止追踪、cadence 偏好与产物引用校验；迁移 007（重建 `jobs`/`actions`）原地升级 v0.1.0 库；608 测试全过；验收记录见 VALIDATION §6 |
| v0.1.2 | **done** | 2026-10-07 | 十步全部完成：执行器 seam、决策契约一等化、通用宿主桥（真实 8/8）、宿主形态补齐、工具主权入账、非 job 唤醒授权（真实 4/4）、建议确认回路、真实推送接收端（真实 5/5）、开箱即用装配、整体验收；793 测试通过，版本统一 0.1.2；详见 `01/VALIDATION.md` §7.4–§7.6 |

## P0 记录（2026-10-06）

- 契约冻结：`schemas/v1/`（10 个 2020-12 schema）+ `src/proactive_sdk/contracts.py`
  （14 个统一错误码、canonical JSON/hash、RFC 3339 规则、Schedule/Decision 跨字段
  校验、单 profile 边界、五个核心 Protocol）。
- 路径安全：`src/proactive_sdk/pathsafe.py`（safe_join / ensure_within /
  validate_zip_member），zip-slip 与符号链接逃逸测试。
- 许可门禁：`docs/LICENSES.md`（gate 标记）+ `tools/license_gate.py`
  （路径前缀 + 全量 hash 双重检查；status≠pass 即阻断）。
- 审计复现：`tools/reproduce_audit.py` 对照 `tests/fixtures/skills.json`（88 条）与
  `tests/fixtures/selected-source-manifest.json`（12 文件 hash/bytes/lines）逐项复算。
  平台标记启发式列为信息列不复算（审计端词法启发式，token 表不可从输出反推）。

## P1 记录（2026-10-06）

实现（对应 SCHED-01 / STORE-01 / 工单 A）：

- **`src/proactive_sdk/clock.py`**：`Clock` 协议 + `SystemClock` / `FakeClock`。
  wall（UTC epoch ms，持久化，可回拨注入测试）与 monotonic（进程内）分离。
- **`src/proactive_sdk/store.py`**：单 profile Store。pragma
  （foreign_keys / busy_timeout / DELETE journal / synchronous=FULL）；
  迁移 runner（包内 SQL、checksum 记账、事务原子、拒绝旧二进制开新库）；
  profile identity 绑定；jobs CRUD（idempotency_key 幂等 + 乐观 revision）；
  事件准入（dedupe key 幂等、同 key 异内容 conflict、64 KiB payload 预算）；
  run claim/complete/fail（fence 单调递增、过期 lease 回收、旧 fence 拒提交）。
- **`src/proactive_sdk/migrations/`**：m001 = examples/schema.sql 基线快照；
  m002 = P1 增量（jobs 记账列、events.payload_json、`job_occurrences`
  occurrence 台账、`jobs_idempotency`）。
- **`src/proactive_sdk/scheduler.py`**：五类 schedule 数学。interval 锚定槽
  （UTC ms，occurrence id = job+revision+slot，抖动不改身份）；daily/weekly/
  monthly 本地时刻解析（fold=0/1 转 UTC round-trip 校验，春令时跳空跳过、
  回拨按 fold_policy 折叠、月内不存在日期跳过）；runonce 一次性；misfire 三策略
  （coalesce_latest / grace_once / expire，错过 episode 只物化最近一槽并带
  episode 计数 reason）；`admit_due` 逐 job 短事务（事务内复核 revision+enabled）。
- **契约同步**：`contracts.py` 新增 `JobSpec`（§4.1 十字段校验）；
  `job_spec.json` schedule 增加 `fold_policy`（enum earliest/latest，可选默认
  earliest）并同步 `schemas/README.md`。TypeScript 侧尚未生成（P5 client-ts，
  已知缺口）。

关键语义决定（与 SPEC 的对应）：

- 同 occurrence 单入队由 `job_occurrences` 主键 + `events.idempotency_key`
  唯一双保险；重试走 `INSERT OR IGNORE` 幂等路径。
- 停机恢复只物化最近一个错过槽：heartbeat 合并、task 在 grace 内补跑一次、
  超窗记 `expired` + 可查询 reason（§5.2 "保留可查询的未执行原因"）。
- 暂停冻结 next_due；恢复重置为 now 交给 misfire 策略裁决，不静默补跑多次。
- 编辑（revision+1）重置 next_due=now，立即按新版本重估。
- 删除有准入历史的 job 被拒绝（暂停代替），审计链不级联抹除（§13.2）。
- 首个 slot 从创建之后起算（interval 取创建所在锚定槽的下一槽），
  新任务不追溯创建前的时刻。

验收（§15.1 P1 行）：时区/DST（gap/fold、转换日偏移）/跨月（31 日、闰年 2/29）/
停机一周/重启/真实进程 kill/双连接并发 claim 测试通过；同 occurrence 单入队、
旧 fence 拒提交、时钟回拨不重放、pause 赢过在途准入、due 扫描走
`jobs_due` 索引（EXPLAIN 验证）+ 1000 任务扫描烟测。命令与数字见 01/VALIDATION.md。

已知缺口（不阻塞 P2，按阶段补）：

- jobs API 仅 store 层；公共 facade `api.py` / `pas serve` / JSON-RPC 控制面在
  P3+/P7。
- run 状态机保留参考切片三态（queued/running/planned）+ failed；§4.3 完整状态
  随 P3 executor 落地。
- outbox/grants/approvals 表已建（m001），写入路径属 P4。
- WAL 仍未启用（沿用参考环境 DELETE journal 决定）；`doctor` 检查在 P7。

## P2 记录（2026-10-06）

实现（对应 SPEC §6 / 工单 HOOK-01）：

- **`src/proactive_sdk/hooks.py`**：
  - *legacy parser*：`parse_hook_result` 只接受退出码 0、恰好一个终结
    `HATCH_HOOK_RESULT` 行（必须是最后一个非空行）、严格 UTF-8/JSON（重复键、
    NaN 拒绝）；未知字段、非法 decision/reason/disable_after_run、payload 超
    16 KiB 都按协议错误处理。`parse_hook_logs` 收集 `HATCH_HOOK_LOG` 结构化
    诊断（有界、不进模型上下文）。limit 起值（stdout/stderr 64 KiB、超时
    5 s、payload 16 KiB）可配置，标注为建议起值而非附件事实。
  - *沙盒层*：`HookSandbox` 协议 + `SeatbeltSandbox`（macOS sandbox-exec，
    deny network* + deny file-write* 仅放行 staging 子树，懒探测、fail-closed）
    + `BubblewrapSandbox`（Linux bwrap --unshare-net，本机未联调）+
    `PlainSubprocessSandbox`（无隔离，需显式 `require_isolation=False` 才可用，
    否则构造即拒绝）。runner 管道边读边限流、超时杀整个进程组（SIGTERM→
    SIGKILL）、rlimit（FSIZE/CPU）兜底、子进程环境为白名单（不继承宿主凭据）。
  - *staging + CAS 状态机*：invocation 前从 DB 物化 canonical state 到一次性
    staging 目录，`HATCH_HOOK_STATE_DIR` 指向 staging；运行后 staging 状态文件
    必须是普通文件（symlink 拒绝）、有界、JSON object——损坏/篡改按
    `hook_state_invalid` 报错，绝不静默当 `{}`（防重新触发首次检测）。
    `commit_hook_invocation` 单事务：invocation 去重（同 id 同内容幂等重放、
    异内容 conflict）+ fence/version/enabled 复核 + wake event 准入 + 状态
    更新 + disable_after_run。
  - *错误记账*：失败（超时/超限/非零退出/协议错误/状态损坏）只记错误计数与
    冷却（指数退避 30 s→1 h 封顶），从不 wake；`force` 供管理员诊断绕过冷却，
    诊断读数不受冷却节流。
  - *dry-run*：子进程带 `HATCH_HOOK_DRY_RUN=1` 且完全不写库；文档明确它不是
    安全边界（§6.3）。
- **`src/proactive_sdk/migrations/m003_p2_hooks.sql`**：hooks 增量列
  （poll/timeout/definition_json/错误记账五列）+ `hooks_due` 索引；全部 additive。
- **`src/proactive_sdk/store.py`**：hook 持久化 API（register 幂等 + definition
  hash 校验、due 扫描、claim 租约/fence、commit CAS、record_error、defer、
  enable/disable、delete 保护）；事件插入抽为事务内共享助手供 hook commit 复用。

关键语义决定（与 SPEC 的对应）：

- 一次性 watch：disable_after_run 与 wake event 同事务持久化——先崩则 hook 仍
  enabled + 旧 state（重试重新检测），后崩则 event + queued run 已在库；
  停探针不删已入队的通知（disable 不级联 events/runs）。
- invocation_id = `hook_id-v<state_version>`（逻辑 ID 确定性派生）；崩溃后
  coordinator 重试天然沿用同 ID；同 ID 异内容 conflict 防双入队。
- 子进程超时用真实单调时钟（基础设施时间），注入 Clock 只管持久化时间——
  FakeClock 冻结不会拉长挂死子进程的超时。
- disable/pause 赢过在途 commit（与 job 暂停语义一致）；fence 失守的提交返回
  skipped 而非报错。
- 每 hook 串行写状态由 claim fence 保证；runner 实例非并发安全（单飞），文档注明。

验收（§15.1 P2 行）：

- **原 helper 兼容**：字节一致的原 `hatch_hook_runtime.sh`（SHA-256 校验）经
  完整 P2 管线（含 seatbelt 沙盒）跑通 silent / wake+disable / 状态 roundtrip /
  dry-run / log 捕获 / 非法 payload 报错。helper 缺席时测试显式 skip 并说明。
- **失败不唤醒**：失败路径全部零 event、零 state 推进、错误计数与退避可查询
  （runner 级实测：超时、stdout/stderr 超限、非零退出、无结果、payload 超限、
  状态损坏/symlink/非对象；parser 级：多结果、结果非末行、非法字段/JSON）。
- **一次性 watch 不丢通知**：disable+event 原子性、崩溃前/崩溃后恢复、幂等重放、
  停探针保留 queued run 各有专门测试。
- **无网络/文件越权**：seatbelt 端到端——hook 内 connect 本地监听端口被拒、
  staging 外写文件被拒且不落盘；PlainSubprocessSandbox 无隔离声明 + require_
  isolation 拒绝（fail-closed）有测试。

已知缺口（不阻塞 P3，按阶段补）：

- hook 常驻轮询循环/守护进程在 P7；`run_due_hooks` 由调用方驱动。
- BubblewrapSandbox 按 bwrap 文档实现并做了 argv/探测测试，但本机是 macOS，
  未做 Linux 端到端联调（01/VALIDATION.md 记录）。
- 网络 broker（域名范围/DNS/redirect 检查）属 P3/P4；当前网络控制只有沙盒级
  deny，不做域名白名单。
- hook 定义尚无公开 schema 对象（§4.1 十对象不含 HookSpec），P3 冻结 facade
  时再定。

## P3 记录（2026-10-06）

实现（对应 SPEC §8 / §15.1 P3 行 / 工单 B + EXEC-01）：

- **`src/proactive_sdk/contracts.py`（扩展）**：类型化记录 RunBudget / RunRequest /
  ContextPack / ContextSource / SourceRequest / SourceItem / SourceBatch /
  MemoryEntry / ActionProposal / Decision / ExecutorCapabilities / ModelToolCall；
  Decision 增加证据闭包校验入口；`validate_context_pack` 跨字段规则与
  `schemas/v1/context_pack.json` 对齐；ModelResponse 增加 `reasoning` 字段
  （显式声明永不解析为动作、不持久化）。
- **`src/proactive_sdk/model.py`**：`OpenAICompatibleModel` —— 唯一真实 provider
  实现（OpenAI Chat Completions 形状）。HTTP transport 可注入；API key 经
  `api_key_provider` 注入（可接 OS keychain，不落日志）；错误映射到统一错误
  词表（401/403→auth_required、429→rate_limited+Retry-After、5xx→
  provider_unavailable、4xx→invalid_config），响应体与凭据永不进错误消息；
  usage 规范化为 §4.1 Usage（provider 未报则 unknown，不写零）；
  adapter_namespace 承载供应商专有参数，不污染公共协议；reasoning 原地丢弃。
- **`src/proactive_sdk/tools.py`**：`ToolSpec`（P3 只接受 read_only=True，写工具
  在注册即拒，fail-closed）+ `LocalToolBroker`。`AuthorizedToolCall` 只能由
  broker 在复检（注册表 ⊆ run allowlist ⊆ 能力快照、参数 schema、剩余预算）
  之后构造——模型输出永远是 attempt，不能反序列化成授权对象；输出有界截断
  并带 evidence ref；拒绝路径全部可计数。
- **`src/proactive_sdk/context.py`**：`MemoryPort`/`EphemeralMemoryPort`（§7.3
  默认：有界、带证据的短条目，明确标注不持久化）；`SourceRegistry`（绑定
  source_id+account+所需能力，逐批校验返回形状）；`SnapshotMaterializer`
  （内容寻址快照，重观察刷新元数据）；`ContextPackBuilder`（不可变 pack，
  fresh_until 双时间戳，`allow_stale=False` 时过期即 stale_context 阻塞）；
  `render_context_blocks` 把来源内容渲染为 data-only 块。
- **`src/proactive_sdk/executor.py`**：`ToolLoopExecutor`——每回合按 §8.2 顺序：
  deadline/cancel/预算检查 → ModelPort.generate（asyncio 硬超时防挂死）→
  工具调用形状校验 → broker 复检执行 → data-only 结果回传 → 直到合法
  Decision 或预算耗尽。Decision 校验：严格 JSON（重复键/NaN 拒）+ 未知字段
  拒绝 + kind 枚举 + 类型化字段校验 + 证据闭包（提案只能引用本 run 的快照/
  记忆/工具证据）+ 提案数预算；解析失败恰好一次有预算修复，再失败 run 失败。
  预算四项（turns/tools/wall_time/proposals）+ 墙钟 deadline 全部生效；
  wall_time 用注入 Clock 的 monotonic（基础设施时间，FakeClock 冻结不会拉长
  挂死调用）。usage 聚合：任一回合 unknown ⇒ 整体 unknown 且字段 None。
- **`src/proactive_sdk/coordinator.py`**：`ProactiveCoordinator`——领取单个
  queued run（单飞）后：**L0**（零模型）：来源注册检查、逐源能力/授权检查、
  delta 拉取（错误分类记因）、cursor 比较判定变化；心跳模式无变化/无来源/
  未授权/来源故障 ⇒ `record_run_suppressed`（机器原因可查询），显式任务与
  hook wake 按语义直进 L1（授权缺失则是可见 failed 而非静默）。**L1**：
  快照物化 + ContextPack 构建（require_fresh_sources 任务过期即阻塞）→
  executor 闭环 → 决策/提案/usage/事件在**单事务**内以 lease fence 提交
  （§13.2），run 终态 `proposed`。
- **`src/proactive_sdk/store.py` + `migrations/m004_p3_executor.sql`**：additive
  迁移（runs.usage_json/decision_summary/proposal_count、context_packs、
  run_proposals）；store 新增 EventRecord/get_event、source_state 读写
  （版本自增）、快照 upsert/查询、run_events 追加（fence 复核 + 安全摘要
  上限）、record_run_suppressed / record_run_decision（fence+状态复核、
  context pack 内容 hash、提案落库）、run_proposals/run_events/get_run/
  list_runs 读路径。P1 的 `planned` 参考路径保持不动。
- **`examples/agent_loop_demo.py`**：独立 Agent demo（工单 B 要求），三场景
  断言式输出；测试替身全部显式声明（fake/scripted），delivered 恒 0。

关键语义决定（与 SPEC 的对应）：

- “没变化零 LLM”是结构性质而非提示词性质：L0 用 cursor 比较 + watermark
  判定，suppressed run 携带机器原因（§5.3/§16.3 hard gate），测试断言
  `model.calls == 0`。
- 恶意来源不改权限靠三层结构门禁：broker 能力集合为代码写死；工具
  allowlist 显式传入 RunRequest（空 = 无工具，不是"全部"）；提案证据必须
  属于本 run 证据闭包。测试用"配合注入的脚本模型"模拟最坏情况。
- hook wake 无 job 可依：以 hook reason 为任务指令、goal_id=`hook:<id>`，
  按显式信号进 L1（§6.2 hook 的 wake 本就是显式检测信号）。
- 分析完成（proposed）与动作执行/通知投递严格分账：提案落 `run_proposals`
  （非 actions 表，无 grant/approval 字段——那是 P4 策略产物）。
- 心跳的未授权/来源故障记 suppressed（机会检查不告警），显式任务记 failed
  （用户必须看见失败）——同一检查两种口径，按 mode 语义分流。

验收（§15.1 P3 行）：

- **没变化零 LLM**：coordinator 测试 + demo 场景 1 双覆盖（0 模型调用、
  cursor 已推进、原因可查询）。
- **显式任务会运行**：task 模式无变化照常进 L1；hook wake 同理。
- **恶意来源不改权限**：注入内容 + 配合注入模型 ⇒ 提案拒绝、run failed、
  0 提案落库、能力集合不变、注入文本始终为数据框架；未注册工具调用被拒。
- **deadline/预算有效**：turns/tools/wall_time/proposals 四预算 + 墙钟
  deadline + asyncio 硬超时 + cancel flag，各有测试。
- **真实模型 fixture 不冒充**：FakeModel 标签（provider=fake、无 measured
  usage）有断言；真实适配器仅本地脚本化 HTTP 服务器契约测试，01/VALIDATION.md
  明确记录未做在线 provider 联调、不做兼容声明。

已知缺口（不阻塞 P4，按阶段补）：

- 在线模型 provider 的锁定版本真实联调属 P5/P7 门禁；token 级预算预留、
  profile 级预算账本未实现（现仅 run 级四项上限）。
- MemoryPort 默认实现不持久化；真实 Source 连接器（日历/邮件）随 P4/P6；
  快照正文 blob store 随 P4。
- run 状态机至 `proposed` 为止；grants/approvals/outbox 写入路径与
  policy_evaluated/actions_queued/completed 状态随 P4。
- 公共 facade（api.py）、`pas serve`、JSON-RPC 控制面随 P4/P7；runs.cancel
  控制面 API 未实现（executor 已支持 cancel flag，等待控制面接入）。
- hook 定义公开 schema（§4.1 之外的对象）仍待 facade 冻结时定。

## P4 记录（2026-10-06）

实现（对应 POLICY-01 / SEND-01 / 工单 B 的策略部分）：

- **`src/proactive_sdk/migrations/m005_p4_policy.sql`**（additive）：grants/approvals
  记账列、run_proposals.policy_json（§4.3 phase A 裁决持久化）、outbox 交付
  记账列（provider_key 幂等键、payload、attempts、reason）、`inbox`（与 outbox
  同事务一次入箱）、`owner_channels`（可信配置绑定的本人通道）、`topic_mutes`、
  `feedback` handled-fact 表达式索引、共享 `blobs` 内容寻址存储。
- **store.py P4 段**：grants CRUD + 撤销级联（同一事务：version+1、pending
  approvals→revoked、未执行 actions→cancelled、未投递 outbox→suppressed）、
  审批冻结/解析（actor 必需、过期先提交后报错）、§4.3 两阶段
  （record_policy_verdicts：proposed→policy_evaluated；queue_run_actions：
  单事务建 approvals/actions/outbox + run 终态）、outbox claim（事务内 grant
  复验）/finish（attempt journal 先行，fence 失守只记账不动状态）/reconcile/
  apply_receipt（晚到/重复回执幂等）、expire/promote 扫描、inbox、feedback。
- **policy.py**：GrantManager（能力快照供给 broker）、OwnerChannelRegistry
  （local_inbox 必须等于 store.owner_destination）、ApprovalManager（冻结请求
  = kind/账户/接收者/规范化参数/附件 hash/证据/payload，哈希由 store 对存储的
  canonical JSON 统一计算，审批与动作绑定同源）、PolicyEngine 硬策略顺序：
  模型点名接收者→拒绝；grant 检查（账户切换/scope 扩大）；过期/已处理/话题
  静音；业务去重键（profile+goal+fact_id+revision+destination+kind，不 hash
  文案）；静默时段推迟（过期则抑制）；每日配额推迟；promote_approved 重新
  计算静默时段；payload 锁屏去敏（webhook 仅摘要，本地 inbox 保正文）。
- **delivery.py**：OutboxDispatcher（网络在事务外；claim 复验 fence；结果
  事务 journal+状态迁移；重试退避封顶；unknown 永不自动重发；reconcile 仅认
  显式 provider 答复，404 不算权威）；WebhookNotificationSink（真实 HTTP：
  Idempotency-Key 头、有界响应、禁跟随重定向、2xx/408/429/5xx/其他 4xx/
  传输错误→accepted/retryable/terminal/unknown 映射）；FeedbackManager。
- **coordinator**：可选注入 policy_engine——决策提交后连跑 §4.3 两阶段；
  策略失败不改写分析结果（run 保持 proposed 可重试，RunReport 单列
  policy_outcome）。

关键语义决定：

- 审批绑定哈希唯一来源：store 对 canonical request JSON 计算 sha256，
  approval 与 action 各自写入同一值——两侧永不漂移；参数/附件任何变化都会
  改变该哈希（重新审批），promote 时哈希不匹配的动作 fail 拒不入队。
- provider 幂等 key 按消息（pas-{action_id}）而非按尝试：重试同 key；
  delivery_unknown 独立状态，只有权威答复（显式 delivered true/false）
  能移动它；"没找到消息"（404/无 status_url）不算权威。
- 本地收件箱：inbox 行与 outbox stored_in_inbox 同一事务（可靠一次入箱）；
  本地记录类提案（draft/internal_record/suggest_watch）不进 actions 表
  （m001 的 actions.grant_id 为 NOT NULL 外键），以 run_events 记账为本地台账。
- 撤销立即生效是级联事务 + 派发前复验双保险：在途消息的 attempt 仍会被
  journal（账面诚实），但消息状态由撤销事务决定，fence 失守的结果提交被拒。

验收（§15.1 P4 行 + §16.1 Policy/Delivery 行）：41 项 policy 测试 +
22 项 delivery 测试覆盖夜间不发（含推迟/过期抑制/白日直发）、撤销立刻生效
（级联/在途/claim 复验）、冻结参数审批（同请求幂等/参数变化重批/篡改拒绝/
拒绝取消/过期/actor 必需）、本人目标不可替换（模型点名接收者抑制/local inbox
绑定校验/未知 profile 拒绝）、ACK 丢失不盲发（5 次派发不重发/权威 not-
delivered 后同 key 重试/无权威源永远停摆/权威 delivered 收口）、晚到与重复
回执、未发送消息的回执视为 unsolicited、重试预算耗尽、双连接不可双取。
另有 2 项 coordinator 端到端（L0→L1→策略→入箱；静音话题优先级不可绕过）。

已知缺口（不阻塞 P5，按阶段补）：

- WebhookNotificationSink 已做真实 loopback HTTP 传输测试；真实 push/邮件
  provider 联调与兼容声明仍属 P5/P6/P7 门禁。
- approval/resolve 目前以 authenticated actor 字符串记账；bearer/token 级
  控制面认证随 P7 JSON-RPC 层。
- 网络出站 broker（域名 allowlist/DNS rebinding/redirect/SSRF 检查）未实现，
  webhook sink 目前仅禁跟随重定向 + 默认 TLS 验证；属 P4/P6 之间的网络层工作。
- profile 级配额只有每日条数口径；预算账本（token/费用）仍待 P5+。
- 快照正文已入 blobs；大附件的分块/外置 blob 存储策略在 P6 随连接器补齐。

## P6 记录（2026-10-07）

实现（对应 SPEC §11 / §9 网络边界 / 工单 C 的适配部分）：

- **`src/proactive_sdk/skills.py`**：Muse legacy importer（BYO、原目录只读）。
  零依赖 frontmatter 解析（inline JSON / 块映射 / 块列表 / 续行）；
  canonical id（下划线→连字符、碰撞加短 hash 后缀无碰撞）+ aliases
  （原名与目录名都可检索）；依赖闭包（SKILL.md 目录树 + 正文引用的
  共享 references/scripts，嵌套 artifacts 层共享资产入闭包，不复制单个
  SKILL.md）；§11.3 sidecar（source/canonical_name/aliases/requirements/
  compatibility/distribution + legacy 扩展记录）；`audit_consistency`
  对照 tests/fixtures/skills.json。符号链接逃逸在扫描与闭包两侧都被拒绝
  （pathsafe ensure_within against 扫描基）。
- **store**：`record_skill_install` / `get_skill_install` /
  `list_skill_installs`——同 (name, hash) 幂等、状态可推进
  （parsed→contract_tested→…）、不同 hash 冲突拒绝（§11.3 技术状态与
  再分发状态独立，installed ≠ e2e_verified）。
- **`src/proactive_sdk/gws.py`**：`hatch_gws_cli` 受限 grammar。argv 列表
  only；只读子集（gmail status/+triage/+read、calendar status/+agenda）；
  其余（含全部写操作）→ `unsupported_command`，不猜测不放行——写入走
  PAS 审批链；not_connected 时原样转发 provider 的 connect_url，没有 URL
  就如实 unavailable（不伪造授权或链接）；auth 错误 → reauth_required；
  provider 内部异常只泄类名。
- **`src/proactive_sdk/connectors.py`**：`GmailMailSource` /
  `CalendarAgendaSource` / `PublicMaterialSource`（§11.5 首版闭环的三个
  读取源）。Delta 统一走 cursor：页面 (fact_id, revision) 的 hash 存
  source_state，相同即空批 → L0 抑制零模型；gmail/calendar 未连接如实
  AUTH_REQUIRED（连接是用户的动作）；公开资料 sensitivity=public，内容
  hash 变化才有新 revision，fact_id 稳定。
- **coordinator（加量）**：`_source_request` 现在把存量 cursor_ref 传给
  连接器（SourceRequest 语义本来如此），补齐连接器的增量判定基础。
- **`src/proactive_sdk/net.py`**：出站 broker 第一块（P4 遗留缺口）。
  HTTPS-only、显式域名 allowlist（精确+子域）、本地解析并钉扎 IP
  （SNI=allowlisted host）、私网/回环/链路本地拒绝（SSRF guard）、
  禁跟随重定向、有界响应、内容类型白名单、自定义 trust anchor 可注入
  （永不关闭校验）。残余风险如实记录（TOCTOU 双解析比对未做）。
- **`examples/pas_pi_extension/`**：Pi 方式 B extension（registerTool
  官方入口）注册 proactive_schedule/status/pause/resume/skills_inspect，
  全部落 PAS RPC；自含最小客户端（回环/https 校验、无凭据泄漏）；
  未配置 fail closed。契约测试 6 项（stub pi + loopback 脚本服务器）。
- **`examples/skills_loop_demo.py`**：四链路闭环——import（真实语料
  88/88 审计一致 + installs 记账）、mail（提案→策略→outbox→本地 inbox
  一次入箱，摘要按用户 locale 而非 skill 固定英文）、calendar（tick1
  proposed、tick2 零模型抑制）、material（内容变化才新 revision）。

关键语义决定（与 SPEC 的对应）：

- `includeInPrompt` 只进 sidecar，永不解释为每轮全文注入（§11.2）。
- 技术状态从 `parsed` 起步：本阶段 gmail/google-calendar 通过 grammar
  契约测试（contract_tested 语义），`e2e_verified` 留给有真实授权的
  部署——导入/测试不构成授权。
- grammar 只按 argv 白名单放行；"+send 等写命令 unsupported" 是能力
  缺口陈述而非故障（§11.4 首版缺失时明确 adapter_required/blocked）。
- L0 变更判定补齐 cursor 透传后，连接器统一"页面 hash = cursor"的
  delta 语义；无 delta 语义的源会每拍进 L1（与 L0 的 `bool(items)`
  分支一致），连接器默认不做这种事。

验收（§15.1 P6 行）：

- **88 入口审计一致**：`audit_consistency` 在真实 private-vendor 语料上
  match=true（count 88、issue 计数 43/41/88/1、全部 sha256/名称/路径）。
- **邮件/日历/资料跟踪/本人通知闭环**：skills_loop_demo 四链路全绿
  （mail: proposed→actions_queued→inbox 1 条；calendar/material: 变化
  proposed、不变 suppressed 零模型）。
- **其余缺口透明**：report 的 issue_counts/compatibility 分布、sidecar
  requirements/compatibility/distribution 字段、PROGRESS/VALIDATION
  已知缺口清单。
- **不得模拟已授权**：not_connected/unavailable 语义测试、无 URL 不编造、
  distribution 恒 permission_unverified、写命令一律 unsupported。

已知缺口（不阻塞 P7，按阶段补）：

- gmail/calendar 的 `e2e_verified` 与真实 Google 授权联调未做（无授权
  凭据；需要部署方提供 connect 流程）；outlook 系同理。
- Hermes 插件/Pi extension 仍未安装进活跃宿主 profile（操作者决定 +
  PAS daemon P7）。
- EgressBroker 的重定向目标主机不重评（直接拒绝）、TOCTOU 双解析比对
  未做；cookie/凭据注入类宿主场景不在首版范围。
- 大附件分块/外置 blob 策略仍留 P6→P7（当前附件不落 PAS 存储，gmail
  +read 只回 inventory 元数据）。

## P5 记录（2026-10-07）

实现（对应 SPEC §12 / §14.2 / 工单 B 宿主部分）：

- **`src/proactive_sdk/hermes.py`**：Hermes Runs executor（方式 C，PAS 驱动
  宿主）。硬限制传输（HTTPS 或字面回环 HTTP、无代理、禁跟随重定向、2 MiB
  响应上限、错误原文不入异常）；能力探测 fail closed（缺 run_submission /
  run_status / run_stop / runs_idempotency 即拒绝启动）；`start` 只代表受理
  （提交≠完成），完成仅来自轮询确认；`waiting_for_approval`/`stopping` 不算
  终态；`cancel` 先 stop 再轮询到宿主确认终态，超时上报
  `cancellation_unconfirmed`（取消≠已停，不伪装 cancelled，不启动替换
  worker）；`reconcile` 用同一 operation key + 同 payload 重放对账，409
  冲突永不静默改写；usage 映射 usage.json（未报告则 pricing_basis=unknown，
  不造零）；events 只产出安全摘要（type+seq），完成判定不依赖事件流；
  run_handle 对齐 schemas/v1/run_handle.json。
- **`src/proactive_sdk/pi_worker/pi_worker.ts` + `src/proactive_sdk/pi_worker.py`**：
  Pi worker（Node 进程）+ Python JSON-RPC 2.0 stdio 桥。worker 是纯
  executor：每 run 一个 `SessionManager.inMemory()` 隔离会话；vetted factory
  显式 cwd/agentDir/工具白名单，prompt 前用 `getActiveToolNames()` 复核
  （名字是额外检查而非只读证明，白名单属部署方）；`prompt()` resolve 只是
  ACK，idle + 终稿 envelope 才算完成；abort 后等 idle，未停如实上报
  `cancellation_unconfirmed`；envelope 校验与 examples/pi_executor.ts 同规则；
  桥对超长行/非 JSON 行判定协议失真并整 worker 替换；并发 run 超限拒绝
  （BUDGET_EXCEEDED），预算诚实。
- **`src/proactive_sdk/rpc.py`**：控制面 JSON-RPC 2.0 信封（§14.2）。冻结
  方法集 20 个；`system.hello` 版本协商（major 不符拒绝），未协商会话调用
  业务方法返回 auth_required；通知不产生可靠写入；PAS 错误码走
  `data.code`，标准 wire code 对外；单帧 1 MiB 上限；handler 异常只泄
  异常类名。
- **`tools/gen_client_ts.py` + `packages/client-ts`**：TS 侧类型从
  schemas/v1 生成（含 P1 的 `fold_policy` 同步，client-ts 缺口关闭）；
  `rpc.ts` 提供 Endpoint 注入式 `PasRpcClient`（hello 协商、方法集冻结、
  1 MiB 上限、通知不可靠语义注释）。源码分发；npm 发布与 facade 结果
  类型绑定属 P7。测试断言生成物与 schema 同步（漂移即失败）。
- **`examples/pas_hermes_plugin/`**：Hermes 插件（方式 B），官方入口
  `register(ctx)` 暴露 proactive_schedule/status/pause/resume/
  skills_inspect 五工具，全部落 PAS RPC；自包含最小客户端（回环/TLS、
  无重定向、有界回复、hello 协商）；未配置 fail closed；不暴露
  grants.create/approvals.resolve（§14.3）。**Hermes 官方
  `plugins validate` 门 PASS**（含隔离 register 探测、工具声明一致、
  安全扫描），未安装进任何活跃 profile（需操作者决定 + PAS daemon，属 P7）。
- **`tools/validate_p5_real.py`**：锁定版本真实服务验证套件（8 探针），
  在服务器本地执行；证据见 01/VALIDATION.md §5e。

关键语义决定（与 SPEC 的对应）：

- 完成判定唯一来源是宿主状态轮询：SSE/事件流只作观察（§12.1）；本版
  Hermes `hermes serve` 网关不挂 Runs 面，真实提供者是 gateway 的
  api_server platform——按实际安装核对后接 `/v1/runs` 全套路由。
- 宿主隔离走 profile：服务器上为验证建 `pas-p5` 专用 profile（clone
  tokenrhythm 配置），`platform_toolsets.api_server: []` 显式零工具
  （Hermes #82010 fail-closed 语义），api_server 仅绑 127.0.0.1:8642，
  profile 级 `API_SERVER_KEY` 独立于主监听器（非 default profile 不继承
  宿主密钥）；Pi 走 `SessionManager.inMemory()` + mkdtemp scratch cwd。
- 幂等对账与服务端语义逐条对齐真实实现：同 key 同 payload 重放返回原
  run（`replayed: true`），同 key 异 payload HTTP 409
  `idempotency_key_conflict`；idempotency 保留期 86400s 记入 capability
  探测，超期对账不在本版自动化（unknown 停摆语义与 P4 outbox 一致）。
- 跨语言 schema 单一来源：TS 类型从 schemas/v1 生成而非手抄；wire 字段
  保持 snake_case。

验收（§15.1 P5 行）：锁定版本（Hermes v0.21.5+8493.g9b38eb1、
pi-coding-agent 1.0.4、node v22.19.0、tokenrhythm/glm-5.3-flash）真实
服务 8/8 探针 PASS：Hermes capabilities/提交完成态（usage measured
688+14 tokens）/幂等重放对账/取消确认/key 冲突；Pi 初始化/完成 envelope
（usage measured 46+33，cache 1280）/取消未停如实上报。提交≠完成、
取消≠已停、隔离 session、受控工具（零工具集）、版本不支持 fail closed
均有真实验证 + 单测双覆盖。命令与数字见 01/VALIDATION.md §5e。

已知缺口（不阻塞 P6，按阶段补）：

- Hermes 插件未安装进活跃 profile（安装即改变宿主环境，需操作者决定；
  且 PAS 常驻 daemon 属 P7）。pas-p5 网关为验证期会话进程，未装 systemd。
- runs.events 只做了轮询观察路径；SSE 流式 adapter 未实现（SPEC 允许：
  断线以状态查询恢复，SSE 本就只作观察）。
- client-ts 为源码分发；facade 结果类型与 npm 发布随 P7；PAS RPC 服务端
  facade（jobs.create 等的持久化实现）属 P7 daemon，插件与 client 已按
  冻结方法集对接。
- Pi 侧方式 B（Pi extension 注册 proactive.* 工具）未开工，随 P6 skills。
- 验证期真实 provider 调用约 9 次微小 run（glm-5.3-flash，合计约
  2.5k 输入 tokens 量级）；token/费用级预算账本仍未实现（P3 缺口延续）。

## P7 记录（2026-10-07）

实现（对应 §14 / §17 / OPS-01 / 工单集成负责人）：

- **`src/proactive_sdk/facade.py`**：`ProactiveAgent` 公共 facade（§14.1）。
  tick（准入→L0/L1→策略→派发，无到期任务零模型调用）、jobs CRUD +
  pause/resume + manual trigger、runs get/list/cancel（取消请求持久化
  m006，排队 run 原子转 suppressed、在途 run 通知 cancel_event、终态
  如实报 not_cancellable）、grants（仅可信 UI 入口）/approvals/feedback/
  inbox、skills audit/import/explain（explain 输出 capabilities 缺口与
  授权状态）、hooks 注册/运行、status/health、export/delete-data、
  backup/restore、start/stop/close + async context manager；stop 区分
  drain 与 force（force 中断的 run 保持 running+lease，由下一实例以
  attempt+1 恢复，绝不改写为 completed）。
- **`src/proactive_sdk/daemon.py`**：§17.1 生命周期。启动 = 单实例锁
  （PID 锁，死进程 stale 接管、不可读 fail-closed）→ 恢复（cancel flag
  清扫、过期投递清扫、deferred 提升、hook staging 清理、重算到期）→
  serve。停机 = 停止准入 → drain（grace 内等在途 run）→ 强制第二次信号
  → 关连接，停止报告如实区分 drained/interrupted。磁盘满两级报警
  （warn 降级 / critical 停止准入但保持读服务）。health.json 每轮原子
  刷新（readiness 无需 RPC 客户端）。
- **`src/proactive_sdk/rpc_server.py`**：§14.2 方法集绑定 facade，Unix
  socket（0600）+ newline-JSON 帧。认证 fail-closed：同 UID peer 凭据
  （Linux SO_PEERCRED / macOS LOCAL_PEERCRED）或 token_file bearer
  （constant-time 比较）；非 hello 首帧拒绝并断连；token 不进日志。
- **`src/proactive_sdk/service.py`**：`pas` CLI（pyproject console
  script）。serve/tick（--app module:factory 装配）/doctor/status/jobs/
  runs/approvals/notifications/skills/hooks/backup/restore/export/
  delete-data/config/rpc/version；全部子命令帮助文本；破坏性命令需
  `--yes`；操作类命令直连 store（与 daemon 共用 fencing，无第二
  scheduler owner）。
- **`src/proactive_sdk/config.py`**：§14.3 配置（自包含 YAML 子集解析，
  零依赖）。未知键拒绝（顶层+嵌套）、tab/重复键/语法错误拒绝、
  timezone 校验；`redacted()` 打印视图隐藏家目录路径与 secret 引用；
  profile 时区来自配置，不推断开发机。
- **`src/proactive_sdk/observability.py`**：§17.2。结构化 JSON 日志
  （run_id/event_id/action_id/job_id/phase/duration + 脱敏：bearer/
  API key/私钥块/长熵串）；SPEC 命名指标（wake/suppressed/model_calls/
  tool_denied/outbox_pending/delivery_unknown/grant_revoked/
  scheduler_lateness）；health = liveness vs readiness（磁盘低/
  delivery_unknown/来源故障/调度停滞 → degraded + 机器原因）。
- **`src/proactive_sdk/backup.py`**：在线备份（SQLite backup API，
  0600 + sha256 sidecar）；restore 五重校验（sidecar/hash/integrity/
  schema≤二进制/identity）+ 原子替换 + daemon 锁拒绝。
- **store**：m006（runs.cancel_requested）；`request_run_cancel`/
  `resolve_cancel_requested`/`run_state_counts`/`outbox_state_counts`/
  `integrity_check`/`schema_version`/`export_profile_data`/
  `wipe_profile_data`。
- **部署与发布**：`deploy/`（systemd 加固 unit、Containerfile + compose
  含 healthcheck、launchd plist、README）；`01/compatibility-lock.json`（历史记录）+
  `docs/COMPATIBILITY.md`（Hermes 0.21.5 / Pi 1.0.4 锁定、e2e_verified
  如实为 false）；`tools/package_gate.py`（对 wheel/sdist 本体做禁止
  路径/hash/密钥扫描，PEM 检测为完整块结构避免误报）；`tools/gen_sbom.py`
  （CycloneDX 1.5，逐成员 sha256）；`tools/install_smoke.py`（全新 venv
  安装 → CLI 流程 → 嵌入 tick）→ **INSTALL SMOKE: PASS**；
  `docs/SECURITY.md`（威胁模型边界 + 已知缺口）、`docs/CONTRIBUTING.md`
  （提交前门禁清单）。

验收（§15.1 P7 行）：

- `python3 -m unittest discover -s tests` → **532 项全部通过**（交接 52
  + P1 125 + P2 76 + P3 58 + P4 63 + P5 61 + P6 51 + P7 新增 46：
  product 27、daemon/RPC/CLI 19）。
- 重启故障（真实子进程 kill -9 在途 run → 重启实例以 attempt+1 回收，
  不伪造 completed）、「取消请求 ≠ 成功完成」（queued→suppressed/
  running→requested/终态→not_cancellable）、drain vs force、备份五重
  校验（identity/schema/篡改/hash/daemon 锁）、认证拒绝（非 hello 首帧/
  错 token）、配置未知键、日志脱敏、CLI 破坏性命令确认——均有断言。
- 全量 conformance 命令照旧：参考 demo 输出不变；审计复现 OK；
  license gate PASS；`tsc -p packages/client-ts` 通过；package_gate
  PASS；SBOM 生成；install smoke PASS。


## v0.1.1 记录（2026-10-07）

按 `SPEC.md` §21.1 的九步推进，顺序与依赖关系未改变既有安装、CLI 与测试。

实现：

- **语义矩阵（第 1 步）**：`docs/SEMANTIC_MATRIX.md` 冻结五类语义、七条
  不变量与 v0.1.0 行为清单；`tests/test_semantics_matrix.py` 把这些行为
  固化成断言（零模型调用、host owner 隔离、pause 跳过、业务键去重等）。
- **直接提醒（第 2 步）**：`JobSpec.mode` 增加 `reminder` 与 `validate_reminder`；
  迁移 `m007_p011_reminders.sql` 重建 `jobs`（放宽 `mode` CHECK、加
  `reminder_json`/`obligation`/`stopped_at_ms`）与 `actions`（`run_id` 可空、
  加 `source`/`occurrence_id`/`obligation`），新增 `job_activity` 投影表；
  `store.admit_reminder_occurrence` 在单事务内复核 job/grant/channel/mute/
  静默/配额并写入 occurrence + action + outbox；`Scheduler` 按 mode 分流。
  迁移期间 `PRAGMA foreign_keys=OFF` + `PRAGMA foreign_key_check` 校验，
  保持单事务原子性。
- **义务分流（第 3 步）**：`obligation`（`due`/`opportunistic`）只由可信
  配置赋予；`store._reminder_gate_tx` 把静默时段与日配额变成**可见延后**；
  新增 `src/proactive_sdk/windows.py`（`quiet_end_ms` / `local_day_start_ms`
  / `local_day_end_ms`，日历重解，DST 日不按 86 400 000 ms 计算），
  policy 改为复用同一实现。
- **迟到与错过（第 4 步）**：记录 planned/actual/lateness 与仅已知原因；
  超窗记 `missed_beyond_grace` 且保留可查询 reason；修正 runonce 在其目标
  时刻之后创建时被静默停放的问题。
- **时效来源（第 5 步）**：`task.refresh_source_ids` 定向读取；
  `OutboxDispatcher._pre_delivery_refresh` 对声明了 `refresh_sources` 的
  动作在效果前定向复读（来源不可用→可恢复延后，授权撤销/tombstone→
  抑制，freshness 已过→延后），纯冻结提醒不发起来源请求。
- **近 24h 语义查重（第 6 步）**：`RecentNotification` + `ContextPack.recent_notifications`
  有界脱敏摘要；`store.recent_sent_notifications` 只统计真正投递成功的
  消息（半开窗口）；`ContextPackBuilder` 与 executor 上下文渲染接入。
- **任务管理与活动（第 7 步）**：`store.stop_job` / `facade.jobs_stop` /
  `jobs_delete` 返 `deleted|stopped`；`job_activity` 投影按 phase 分离执行
  与投递结果（`analysis` 阶段对 heartbeat/task 也投影，静默时给出
  `l0_*` 原因，因此"为何保持安静"在所有 job 类型上都可见）；
  CLI `pas jobs stop|delete|activity`、`pas activity`、RPC
  `jobs.stop` / `jobs.activity` 与 client-ts 同步。
- **宿主体验与产物（第 8 步）**：`PolicyConfig.cadence` + 宿主提供的
  `cadence_min_gap_seconds` / `cadence_max_per_day`（无数字即无门禁，SDK
  不提供默认小时数）；新增 `src/proactive_sdk/artifacts.py`，
  proposal 与 reminder 的 `artifact_refs` 必须可打开且出现在正文。
- **数据生命周期修正**：`wipe_profile_data` 原先按错误的父子顺序删除并
  吞掉 `sqlite3.Error`，导致有 occurrence 的 profile 在 `delete-data`
  之后 `events`/`jobs` 仍残留；改为按 FK 图严格先子后父 + 提交前
  `foreign_key_check` 校验，并把新增账本纳入 export/wipe。
- **窗口解析修正**：`quiet_window` 现在识别 `quiet_hours_timezone`（facade
  写入的 profile 级默认值），此前该默认值被接受后被静默忽略。
- **公共契约同步**：`schemas/v1/job_spec.json`、`schemas/v1/context_pack.json`
  更新；`tools/gen_client_ts.py` 重新生成；新增
  `tests/test_client_ts_drift.py` 防止生成类型与 Python/TS 方法表漂移；
  版本统一为 v0.1.1（`pyproject.toml` / `__init__.__version__` /
  `packages/client-ts/package.json`）。

验收（命令与结果见 `01/VALIDATION.md` §6）：

- `python3 -m unittest discover -s tests` → **608 项全部通过**。
- 迁移升级：真实 v0.1.0 库（schema 6 + 代表性行）原地升级，行数不丢、
  `foreign_key_check` 为空、外键恢复启用、重开不再重复迁移。
- 全量门禁照旧：示例脚本输出不变、审计复现 OK、license gate PASS、
  Pi TypeScript 契约检查 4 + 6 通过、client-ts strict 编译通过、
  package_gate PASS、SBOM 生成、install smoke PASS（`pas version` 报
  v0.1.1）。

v0.1.1 未做/边界（不计为完成）：真实日历/通知渠道授权联调未做（第 5 步
用脚本来源驱动真实代码路径）；真实用户流程人工回归未做；性能与模型调用
数未标环境（只断言"零模型调用"这一离散事实）；cadence 数值由宿主提供，
未做产线调参；本轮未重跑 P5 锁定版本真实服务探针；client-ts 仍未发布到
npm。

P7 未做/边界（如实记录，不作为已完成能力）：

- **真实用户流程人工回归未做**——SPEC §15.1 P7 行的最后一项验收
  （真人按 README 用例操作）需要操作者执行，本阶段仅机器门禁。
- 未发布到 PyPI/npm（自用项目，无发布动作；wheel/sdist 构建与产物
  门禁就绪）。npm 发布与 client-ts 的 registry 分发未做。
- 控制面远程 TLS HTTP 未实现（SPEC §14.2：本地 socket 默认，远程才
  需要 TLS——留待部署需要时）。
- `event_retention_days` 应用于运行日志账本（delivery_attempts/
  hook_invocations 等清扫）未实现；事件/run 决策账本保留至
  export/delete（边界在 SECURITY.md 声明）。
- 大附件分块/外置 blob 策略未实现（P6 缺口延续，gmail +read 只回
  inventory 元数据）。
- runs.cancel 对 actions_queued 之后的消息不生效（outbox 撤销走
  grant 撤销/feedback 路径，文档已注明语义边界）。
- skills.explain 的 `binaries` 字段依赖 sidecar 记录；运行时 PATH 探测
  未实现（技术状态到 parsed 为止）。
