# 施工进度（P0–P7）

按 SPEC §15.1 阶段推进；每阶段完成后在此登记，汇报格式遵循 AGENTS.md。
"完成"以该阶段验收条件全部通过为准，不以代码写完为准。

| 阶段 | 状态 | 完成日期 | 说明 |
|---|---|---|---|
| P0 边界与来源 | **done** | 2026-10-06 | contracts/schema、许可清单与门禁、审计复现命令、zip-slip 防护、单 profile 边界；详见下 |
| P1 持久化与时钟 | **done** | 2026-10-06 | store、migrations、Clock、五类 schedule、misfire、claim/fencing、jobs API；验收测试见下 |
| P2 Hooks 与事件 | not started | — | 沙盒 runner、legacy parser、staging + CAS、hook 状态机 |
| P3 独立 Agent 闭环 | not started | — | Source/Memory ports、ContextPack 运行链路、ToolLoopExecutor、ModelPort |
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
