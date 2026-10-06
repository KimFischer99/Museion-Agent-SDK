# 施工进度（P0–P7）

按 SPEC §15.1 阶段推进；每阶段完成后在此登记，汇报格式遵循 AGENTS.md。
"完成"以该阶段验收条件全部通过为准，不以代码写完为准。

| 阶段 | 状态 | 完成日期 | 说明 |
|---|---|---|---|
| P0 边界与来源 | **done** | 2026-10-06 | contracts/schema、许可清单与门禁、审计复现命令、zip-slip 防护、单 profile 边界；详见下 |
| P1 持久化与时钟 | **done** | 2026-10-06 | store、migrations、Clock、五类 schedule、misfire、claim/fencing、jobs API；验收测试见下 |
| P2 Hooks 与事件 | **done** | 2026-10-06 | 沙盒 runner、legacy parser、staging + CAS、hook 状态机；验收测试见下 |
| P3 独立 Agent 闭环 | **done** | 2026-10-06 | Source/Memory ports、ContextPack、ToolLoopExecutor、OpenAI 兼容 ModelPort、L0/L1 coordinator；验收测试见下 |
| P4 策略与投递 | not started | — | grants、审批、outbox、本人 inbox、真实通知 sink、对账 |
| P5 宿主适配 | not started | — | Hermes Runs / Pi worker，锁定版本真实联调 |
| P6 Skills 能力 | not started | — | legacy importer、aliases、依赖闭包、Gmail/Calendar 最小兼容 |
| P7 产品化与发布 | not started | — | daemon、备份恢复、SBOM、license gate、兼容矩阵 |

## P0 记录（2026-10-06）

- 契约冻结：`schemas/v1/`（10 个 2020-12 schema）+ `src/proactive_sdk/contracts.py`
  （14 个统一错误码、canonical JSON/hash、RFC 3339 规则、Schedule/Decision 跨字段
  校验、单 profile 边界、五个核心 Protocol）。
- 路径安全：`src/proactive_sdk/pathsafe.py`（safe_join / ensure_within /
  validate_zip_member），zip-slip 与符号链接逃逸测试。
- 许可门禁：`docs/LICENSES.md`（gate 标记）+ `tools/license_gate.py`
  （路径前缀 + 全量 hash 双重检查；status≠pass 即阻断）。
- 审计复现：`tools/reproduce_audit.py` 对照 `audit/skills.json`（88 条）与
  `audit/selected-source-manifest.json`（12 文件 hash/bytes/lines）逐项复算。
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
`jobs_due` 索引（EXPLAIN 验证）+ 1000 任务扫描烟测。命令与数字见 VALIDATION.md。

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
  未做 Linux 端到端联调（VALIDATION.md 记录）。
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
  usage）有断言；真实适配器仅本地脚本化 HTTP 服务器契约测试，VALIDATION.md
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
