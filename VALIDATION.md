# 验证记录

日期：2026-10-06（P0–P7）；2026-10-07 追加 v0.1.1 优化（§6）与 v0.1.2 优化（§7，已完成）。只记录本次实际完成的检查；以下数字不代表成品 SDK 的完整测试覆盖率。

## 环境

| 工具 | 实测版本 |
|---|---|
| Python | 3.14.4（P1–P3 复测时；交接时为 3.13.5） |
| Python 链接的 SQLite | 3.50.4（P1–P3 复测时；交接时为 3.46.1） |
| Node.js | 24.14.0（交接时为 22.16.0） |
| TypeScript compiler | 5.8.3（经 npx 固定版本调用） |
| 原 helper 依赖 | Bash、jq，环境中可用 |
| macOS sandbox-exec | /usr/bin/sandbox-exec 存在且探测通过（P2 实测） |

参考 SQLite ledger 明确使用 DELETE journal；没有在此环境启用需要另外核验修复版本的 WAL。目标 Python 3.11+ 是设计范围，本次没有跑 3.11/3.12 的版本矩阵。
P1 复测环境与交接时不同（Python/SQLite/Node 均升级）；DELETE journal 决定在新版本下仍然成立（PRAGMA 实测值见下文 P1 一节），WAL 修复核验在启用 WAL 前仍是前置条件。

## 1. 原创 Python 参考代码

实际命令：

```bash
python -m unittest discover -s tests -v
python examples/reference_core.py
```

**52 项测试通过。** 其中 44 项覆盖参考核心，8 项覆盖 Hermes transport 的本地模拟契约。

覆盖严格 hook 解析、非法 JSON/输出大小/退出码、interval coalescing、静默时间窗口、重复本地时刻的投递规则、hook 状态 CAS、dry-run、失败回滚、重启读取、run lease/fencing、事务 outbox、同事实去重、通知过期、unknown 状态与晚到回执、固定本人目标、profile 拒绝混用等。

不涵盖真实多进程并发压测、断电文件系统耐久、完整 daily/weekly/monthly 发生器、生产政策服务、sandbox、真实 Google/通知 API 或 LLM。

demo 输出：

```json
{"delivered_notifications":0,"events":1,"pending_notifications":1,"runs":1}
```

没有创建虚假的“已投递”成功记录。

## 2. Pi TypeScript 示例

实际命令：

```bash
tsc --strict --target ES2022 --module commonjs --lib ES2022,DOM \
  --outDir /tmp/pas-ts-build \
  examples/pi_executor.ts tests/pi_contract_test.ts
node /tmp/pas-ts-build/tests/pi_contract_test.js
```

**TypeScript strict 编译通过；4 项结构接口检查通过。** 覆盖等待完成后读取结果并 dispose、拒绝 silent 携带提案、拒绝未允许工具、拒绝预先取消的任务。

这里使用结构接口与 mock session，**没有安装/加载真实 Pi 包**。也没有对完整 ActionProposal schema、extension 隔离或在途取消进行端到端验证。`parseDecision` 只是 envelope 校验，不能替代生产 action validator。

## 3. 附件原脚本

对 17 个原始 Python 文件逐个完成 `ast.parse`。对 21 个原始 shell 文件逐个完成 `bash -n`。全部通过。这是语法检查，不是正确性或安全认证。

只对指定 SHA-256 的 `hatch_hook_runtime.sh` 执行了受控功能测试：

```bash
python reuse/test_original_helper.py /private/path/hatch_hook_runtime.sh -v
```

**8 项测试通过。** 分别为 silent、wake/disable、stderr log、初始状态、状态 roundtrip、dry-run 不写状态、非法 state 类型和非法 payload 拒绝。

每次使用临时目录、显式环境变量与子 Bash 进程。没有执行附件中的 home-init、tripwire、日志裁剪、凭据客户端或任何实际连接器。

## 4. 数据库设计与提取工具

- `examples/schema.sql` 在内存 SQLite 中成功建立 **18 张表**。
- 这仅验证 DDL 可执行；没有实现完整迁移、所有状态 CHECK 约束、schema 回滚或生产 DAO。
- `reuse/extract_original.py` 对原 ZIP/hash 验证通过，并成功提取 byte-identical helper 到新私有目录。
- 原 helper SHA-256：`c87af221181a1559e3adcb0cdd601f5be5d4bd91eec2fe0597c09dc27558e741`。

## 5. P1 持久化与时钟（新增，2026-10-06）

实际命令：

```bash
python3 -m unittest discover -s tests -v
python3 examples/reference_core.py
npm exec --yes --package=typescript@5.8.3 -- tsc --strict --target ES2022 \
  --module commonjs --lib ES2022,DOM --outDir /tmp/pas-ts-build \
  examples/pi_executor.ts tests/pi_contract_test.ts
node /tmp/pas-ts-build/tests/pi_contract_test.js
python3 tools/reproduce_audit.py
python3 tools/license_gate.py
```

**177 项 Python 测试全部通过**（交接包原有 52 项 + P1 新增 125 项），参考 demo
输出与交接时一致（`{"delivered_notifications":0,"events":1,"pending_notifications":1,"runs":1}`）；
TypeScript strict 编译 + 4 项结构检查通过；审计复现 OK；license gate PASS。P1 新增覆盖（对应 SPEC §16.1 Scheduling / Transactions / Operations 行）：

- **迁移**：全量建表与 checksum 记账、重复打开幂等、注入坏 SQL 时事务整体回滚、
  库版本新于二进制时拒绝打开。实测 PRAGMA：journal_mode=delete、synchronous=2(FULL)、
  foreign_keys=1（FK 违规真实抛错）。
- **调度**：interval 锚定槽与漂移（槽后 500ms 内准入）、时钟回拨不重放已准入
  slot、runonce 过期窗口、Berlin 2026-03-29 春令时跳空跳过 / 2026-10-25 回拨
  折叠到 earliest（fold_policy=latest 取第二次）、转换日 daily 偏移 1h 变化、
  每月 31 日短月跳过不钳到月底、闰年 2/29、ISO 周几。
- **misfire**：停机 7 天后 heartbeat 恰好补 1 次（不是 336 次）；grace_once 超
  窗记 expired + `episode_slots=336` 可查询 reason、窗内补跑一次；expire 策略
  只在健康 tick 容差内准入；连续错过 episode 计数不重算；job deadline 到期阻断。
- **事务/并发**：同 occurrence 重试单入队（event/run/occurrence 各 1 条）；两个
  独立 SQLite 连接抢同一 run 恰好一胜、4 个 run 被两连接无重复认领；**真实子进程**
  claim 后 `os._exit(1)` 模拟崩溃，父进程重启回收过期 lease 并以新 fence 续作，
  死进程旧 fence 提交被拒；lease 存活期间第二进程拿不到租约。
- **jobs API**：idempotency 重放返回原记录、同 key 异内容 conflict、乐观 revision
  （必须 +1）、暂停/恢复 revision 语义、删除有准入历史的 job 被拒。
- **扫描效率**：due 查询 EXPLAIN QUERY PLAN 走 `jobs_due` 索引；1000 任务注册
  + 全量准入 < 30s（本机 ~2s，环境相关，非基准声明）。

P1 未做/边界（继续成立）：未做跨进程多 writer 长时间压测与断电耐久测试；未做
WAL 启用；store 层 jobs API 不等于公共 facade/daemon（P3+/P7）；run 状态机为
参考切片三态 + failed，§4.3 完整状态随 P3；outbox/grants/approvals 表已建但
写入路径属 P4；`packages/client-ts` 未生成，fold_policy 的 TS 侧同步待 P5。

## 5b. P2 Hooks 与事件（新增，2026-10-06）

实际命令（同 P1 一节的全量命令，结果为）：

- `python3 -m unittest discover -s tests -v` → **253 项全部通过**（交接 52 + P1 125 + P2 新增 76），
  参考 demo 输出不变；`python3 examples/reference_core.py`、审计复现、license gate、
  TypeScript strict + 4 项结构检查全部照旧通过。
- P2 的 76 项中：70 项不依赖原 helper；26 项经真实子进程执行（本机全部落在
  seatbelt 沙盒下，含沙盒可用性探测）；6 项原 helper 兼容测试在本机全部执行
  （private-vendor 存在时；缺席则显式 skip 并注明原因，不冒充通过）。

P2 新增覆盖（对应 SPEC §6 / §15.1 P2 行 / HOOK-01）：

- **legacy parser**：恰好一个终结结果行且为最后非空行、UTF-8/严格 JSON
  （重复键、NaN 拒绝）、诊断文本前缀容忍、未知字段/非法 decision/reason/
  disable_after_run/payload 超限全部拒绝；HATCH_HOOK_LOG 诊断有界收集、
  畸形日志不致命。
- **staging + CAS**：DB→staging 状态物化、staging 状态必须为普通文件
  （symlink/非对象/损坏按 `hook_state_invalid` 报错而非静默 `{}`）、
  claim fence/version/enabled 复核、fence 失守与版本漂移提交被拒、同
  invocation 幂等重放返回原 event、同 ID 异内容 conflict。
- **hook 状态机/错误冷却**：错误计数、指数退避（30s→2 倍→封顶 1h）、冷却内
  defer、管理员 force 绕过、成功清零；disable/pause 赢过在途 commit；
  删除有提交历史的 hook 被拒。
- **崩溃恢复**：真实子进程 + 注入崩溃——commit 前崩溃：canonical state 与
  检测水位不推进、重试重新检测且单入队；commit 后崩溃：event + queued run
  已持久化、同内容重放不双入队。
- **沙盒（本机实测）**：macOS sandbox-exec `(deny network*)` 实测拒绝子进程
  connect 本地监听端口；`(deny file-write*)` + staging 子树放行实测拒绝
  staging 外写文件（文件未落盘）、staging 内写正常；超时杀进程组实测回收
  `sleep` 后代（pgrep 复核）；管道边读边限流（200 KB 输出被杀而非缓冲后
  判断）；PlainSubprocessSandbox 无隔离声明 + `require_isolation=True` 构造
  即拒（fail-closed）、显式降级才可运行。
- **原 helper 兼容**：字节一致 helper（SHA-256 校验）经完整管线（含 seatbelt）
  silent / wake+disable_after_run（payload {"version":2} 透传、hook 自禁用、
  queued run 保留）/ 状态 roundtrip（{"n":1}→{"n":2}）/ dry-run 不写状态不写
  库 / log 捕获 / 非法 payload 报错不 wake。

P2 未做/边界（如实记录）：hook 常驻轮询 daemon 属 P7，`run_due_hooks` 需调用方
驱动；`BubblewrapSandbox` 完成 argv/探测测试但本机为 macOS，未做 Linux 端到端
联调；网络控制仅沙盒级 deny，域名范围/DNS/redirect broker 属 P3/P4；seatbelt
为 Apple 已弃用接口，若未来失效 runner 会探测失败并 fail-closed（不会静默降级
为无隔离运行）；沙盒 profile 未做内存/CPU 硬隔离（rlimit 兜底 FSIZE/CPU），
容器/cgroup 方案按 SPEC 属 P7 运维层。

## 5c. P3 独立 Agent 闭环（新增，2026-10-06）

实际命令（同前两节的全量命令，另加 demo）：

- `python3 -m unittest discover -s tests -v` → **311 项全部通过**（交接 52 +
  P1 125 + P2 76 + P3 新增 58），参考 demo 输出不变；审计复现 OK；license gate
  PASS；TypeScript strict + 4 项结构检查照旧通过。
- `python3 examples/agent_loop_demo.py` → 三个场景断言全过，退出码 0：
  `no_change`（第二拍 `l0_no_source_change`，该拍 0 次模型调用）、
  `task`（run=proposed、1 条提案、usage 记账 1 次工具调用、delivered=0）、
  `malicious`（伪造证据提案被拒，run=failed/invalid_config，权限集合不变、
  0 条提案落库）。

P3 新增覆盖（对应 SPEC §8 / §15.1 P3 行 / EXEC-01）：

- **executor（23 项）**：Decision 严格解析（重复键/NaN/未知字段/未知 kind/
  接收者字段拒绝）；silent⇒空提案、propose⇒≥1、notify_self⇒证据+过期时间；
  伪造证据（不在本 run 证据闭包内）整条 Decision 拒绝；恰好一次有预算的
  修复后仍失败则 run 失败，不从文本猜动作；工具调用回环（call_id/参数
  schema 校验、结果以 data-only 消息回传、证据进入闭包）；未注册/不在
  allowlist/能力缺失/参数非法四类拒绝路径均 tool_denied 记账且 run 继续；
  预算（max_model_turns、最后一回合被工具占用、max_tool_calls、
  wall_time_s 单调钟口径）全部生效；run 墙钟 deadline 过期拒绝且零模型
  调用；cancel event 生效；usage 聚合（measured 求和；任一回合未知→整体
  unknown 且字段为 None 不写零）；reasoning 链路不进消息、不持久化。
- **model adapter（9 项）**：OpenAI 兼容适配器对**本地脚本化 HTTP 服务器**
  （真实 loopback HTTP，非 in-process mock transport）验证请求形状
  （model/messages/tools、Authorization 头、adapter_namespace 合并）、
  tool_calls/usage 解析、缺 usage ⇒ unknown 不写零、401/429/500/400 错误
  映射（Retry-After 解析、响应体与凭据不进错误消息）、非 JSON/坏 tool
  arguments ⇒ provider_unavailable、连接拒绝映射、reasoning 字段丢弃；
  FakeModel fixture 的 provider="fake" 标签与无 measured usage 有断言
  （不冒充真实 provider）。
- **context（14 项）**：ContextPack `to_dict()` 对照冻结的
  `schemas/v1/context_pack.json` 通过；data_only 常量不可改；来源过期 +
  `require_fresh` ⇒ stale_context 阻塞，允许过期时双时间戳随 pack 呈现；
  证据闭包 = 快照 + 记忆 + 本 run 工具证据；快照内容寻址（同内容同 ref、
  重观察刷新元数据）；注入文本以数据块框架渲染（框架文本是纵深防御，
  真正的门禁是 broker/证据闭包，有测试同时覆盖两者）。
- **coordinator（12 项）**：心跳无变化 ⇒ suppressed + `l0_no_source_change`
  + 该拍零模型调用 + cursor 已推进；心跳有变化 ⇒ proposed + ContextPack/
  提案/usage/run_events 单事务落库且 seq 单调；显式任务无视变化照常进
  L1；任务 + 授权缺失 ⇒ failed/permission_denied（可见失败而非静默）、
  任务 + 来源全部故障 ⇒ failed/provider_unavailable、心跳 + 未授权 ⇒
  suppressed `l0_source_unauthorized`；hook wake（无 job）按显式信号运行
  且 reason 成为任务指令；**恶意来源端到端**：注入文本 + 配合注入的脚本
  模型（最坏情况）伪造证据 ⇒ 两次修复后 run failed、0 提案落库、能力
  集合不变、模型请求里注入文本始终以数据框架出现；模型调用未注册工具
  （send_email）⇒ 拒绝记账、run 继续；真实 lease 过期 + 第二 claimant
  接管 + 旧 fence 决策提交被拒；coordinator 对崩溃 run 以 attempt+1 重收
  且不产生重复 run。

P3 未做/边界（如实记录）：**真实模型只做了传输层契约**——
`OpenAICompatibleModel` 通过本地脚本化 HTTP 服务器验证请求/响应逻辑，
未对任何在线 provider 的锁定版本做真实联调（该验证属 P5/P7 门禁，此处
不做兼容声明）；usage 的 token 级预留未实现（仅回合/工具/墙钟/提案四项
上限，§8.3 记为缺口）；MemoryPort 默认实现为进程内有界条目、**不持久化**
（§7.3 持久后端后补）；Source 连接器（日历/邮件）为测试 fixture，真实
连接器随 P4/P6；`snapshot_content` 仅返回 ref，正文 blob store 属 P4；
profile 级预算与 runs.cancel 控制面 API 未实现（executor 已支持 cancel
flag）；run 状态机推进到 `proposed`，`policy_evaluated/actions_queued/
completed` 属 P4；公共 facade（api.py）与 JSON-RPC 控制面未开工。

## 5d. P4 策略与投递（新增，2026-10-06）

实际命令（同前几节的全量命令，另加 demo）：

- `python3 -m unittest discover -s tests` → **374 项全部通过**（交接 52 +
  P1 125 + P2 76 + P3 58 + P4 新增 63），`python3 examples/reference_core.py`
  输出不变；`python3 examples/agent_loop_demo.py` 三场景照旧；审计复现 OK；
  license gate PASS；TypeScript strict + 4 项结构检查照旧通过。
- `python3 examples/policy_delivery_demo.py` → 六场景断言全过，退出码 0：
  夜间推迟/一次入箱/业务去重/反馈静音/ACK 丢失对账重试/撤销级联/冻结审批。

P4 新增覆盖（对应 SPEC §9 / §10 / §15.1 P4 行 / POLICY-01 / SEND-01）：

- **policy（41 项）**：静默时段（夜间推迟至 quiet end、期间过期则抑制不
  补发、白天直发、HH:MM/时区配置错误报错）；本人目标不可替换（模型在
  arguments 点名 destination/receiver/to 一律抑制而非转发；local_inbox 通道
  必须等于 store.owner_destination；未知 notification profile 拒绝）；撤销
  立刻生效（同一事务级联 pending approvals→revoked、actions→cancelled、
  outbox→suppressed，派发零投递；在途发送的 attempt 仍入 journal 但消息
  提交被拒；能力快照随撤销收窄；撤销幂等）；冻结参数审批（外部动作
  waiting_for_approval 且 pending 期间派发零请求；同冻结请求幂等返回原
  审批；参数/附件变化产生新 hash 需重新审批；哈希篡改在 promote 时 fail
  拒绝入队；拒绝取消动作并结算 run；过期先落盘再拒绝、不可解析；resolve
  必须带 authenticated actor 并记录 resolved_by）；账户切换与 scope 扩大
  （account_mismatch/resource_id 越界/actions 白名单越界均抑制）；两层去重
  的业务层（同 fact+revision 跨 run 抑制、revision 升级放行）；反馈抑制
  （mute_topic 经 feedback 事务生效、unmute 恢复、handled 事实抑制、配置
  muted_topics 为硬策略、feedback 强制 actor 与已知 message）；每日配额
  （推迟不丢弃，deferred 是 queued 子集口径）；过期消息派发前清扫为
  expired；本地记录类提案走 run_events 不进 actions/outbox；锁屏去敏
  （webhook payload 仅摘要、本地 inbox 保正文）；blobs 内容寻址 roundtrip、
  快照正文经 blob 解析、通道重注册冲突检测。
- **delivery（22 项）**：真实 loopback HTTP 上验证 Idempotency-Key/Content-Type
  头与 canonical JSON body；2xx→provider_accepted（receipt external_id）、
  408/429/5xx→failed_retryable、其余 4xx→failed_terminal、302 不跟随且计
  terminal；服务端已处理但连接死亡→delivery_unknown（不是 failed）；本地
  inbox 一次入箱（重复派发不双入箱、attempt journal 带 fence）；stale fence
  只 journal 不迁移状态、真 claimant 可正常完成；**ACK 丢失不盲发**——
  unknown 后推时钟 5 次派发零重发、attempt 仍 1 条；权威 not-delivered 后
  重新排队并以同一 provider_key 重试成功（服务器所见幂等键集合为 1）；
  无 status_url/404/非布尔答复均不算权威、unknown 永远停摆不自动重发；
  权威 delivered 收口为 reconciled_delivered；晚到回执把 failed_terminal
  对账为 reconciled_delivered、重复回执幂等；从未发送的消息收到回执视为
  unsolicited 状态不变；重试退避（not_before 推后、指数封顶、预算耗尽转
  failed_terminal）；双连接不可双取同一消息；撤销介于 claim 与 finish 之间
  时提交被拒。
- **coordinator 端到端（2 项）**：L0→L1→策略→outbox→本地 inbox 全链
  （report.policy_outcome=actions_queued、inbox 落 fact/body、run 终态
  actions_queued）；静音话题提案（urgency=urgent）端到端被策略拦截、
  零投递——优先级不可绕过明确禁区。

P4 未做/边界（如实记录）：真实通知 provider（push/邮件/日历写入）未联调，
webhook sink 只有 loopback 传输层契约（兼容声明属 P5–P7 门禁）；出站网络
broker（域名 allowlist/DNS rebinding/SSRF/redirect 检查）未实现，sink 仅禁
重定向 + 默认 TLS 验证；approval 解析的认证目前是 authenticated actor 字符串
记账，bearer/token 级控制面认证随 P7；配额仅每日条数口径；request_external_
action 批准后经 webhook 派发"授权自动化请求"，真实外部动作执行器属 P5/P6；
delivery 派发循环由调用方驱动（常驻 daemon 属 P7）。

## 5e. P5 宿主适配（新增，2026-10-07）

实际命令（本机，门禁）：

- `python3 -m unittest discover -s tests` → **435 项全部通过**（交接 52 +
  P1 125 + P2 76 + P3 58 + P4 63 + P5 新增 61：hermes 30、pi worker 13、
  rpc 9、pas plugin 9），`python3 examples/reference_core.py`、
  `agent_loop_demo.py`、`policy_delivery_demo.py` 输出不变；审计复现 OK；
  license gate PASS（75 tracked files）。
- `tsc --strict --target ES2022 --module commonjs --lib ES2022,DOM
  --outDir /tmp/pas-ts-build examples/pi_executor.ts tests/pi_contract_test.ts`
  → 4 项结构检查照旧通过；`tsc -p packages/client-ts/tsconfig.json`
  （strict + noUncheckedIndexedAccess + exactOptionalPropertyTypes）通过；
  `python3 tools/gen_client_ts.py` 幂等（重跑 unchanged）。

锁定版本真实服务验证（2026-10-07，服务器本地执行 `python3
tools/validate_p5_real.py all`，非本机 mock）：

- 环境：Azure 印度主机（Ubuntu 24.04）；Hermes Agent
  v0.21.5+8493.g9b38eb1 (2026.9.24)；@earendil-works/pi-coding-agent
  1.0.4；node v22.19.0；provider tokenrhythm（glm-5.3-flash，
  chat_completions）。Hermes 侧走 gateway `api_server` platform
  （127.0.0.1:8642），专用 `pas-p5` profile：
  `platform_toolsets.api_server: []`（显式零工具，fail-closed）、profile
  级 API_SERVER_KEY 独立（非 default profile 不继承宿主密钥，Hermes
  fail-closed 语义）。Pi 侧 worker 进程内 `SessionManager.inMemory()`，
  scratch cwd = mkdtemp，工具白名单 read/grep/find/ls。
- 探针结果（8/8 PASS）：
  1. `hermes.capabilities` — features.run_submission/run_status/run_stop
     = true，runs_idempotency {supported, durable, retention_seconds:
     86400}；
  2. `hermes.submit_complete` — POST /v1/runs（Idempotency-Key）→
     status: started（受理）→ 轮询 completed；output 字符串；usage
     {input 688, output 14, elapsed_ms 2502, pricing_basis: measured}；
  3. `hermes.idempotency_replay` — 同 key 同 payload 重放返回原 run_id
     （服务端 replayed: true，不产生第二次模型调用）；
  4. `hermes.cancel_confirmed` — POST stop → stopping → 轮询至
     cancelled（completed:false, interrupted:true）；
  5. `hermes.key_conflict` — 同 key 异 payload → HTTP 409
     idempotency_key_conflict，适配器映射 CONFLICT，永不改写；
  6. `pi.initialize` — worker 经 `node --experimental-strip-types` 加载
     真实 Pi SDK，pi_version 1.0.4；
  7. `pi.run_envelope` — createAgentSession（inMemory + 工具白名单）→
     prompt → idle → 终稿 envelope {decision: silent, proposals: []}；
     usage {input 46, output 33, cache_read 1280, pricing_basis:
     measured}；
  8. `pi.cancel` — 长 run 中途 abort → 会话未 idle，如实上报
     cancellation_unconfirmed（取消≠已停）。
- Hermes 插件真实入口校验：`hermes plugins validate
  examples/pas_hermes_plugin` → **Validation passed**（requires_env/
  loadable/capability probe register() 隔离运行/declared tools 一致/
  安全扫描/无 core override 全部 ✓）。未安装进活跃 profile。
- 成本口径：验证全程真实 provider 调用共约 9 次微小 run（含服务器端
  人工探针），glm-5.3-flash 约 2.5k 输入 tokens 量级。

P5 未做/边界（如实记录）：Hermes 插件未安装进活跃 profile（改变宿主
环境需操作者决定；PAS daemon 属 P7）；pas-p5 网关为验证期会话进程未装
systemd；runs.events 仅轮询观察，SSE 流式未实现（SPEC 允许：SSE 只作
观察、断线状态查询恢复）；client-ts 源码分发、facade 结果类型与 npm
发布随 P7；PAS RPC 服务端 facade（§14.2 方法的持久化实现）属 P7；
Pi 方式 B（extension 注册 proactive.*）随 P6；idempotency 超期（>86400s）
对账未自动化；token/费用预算账本未实现。

## 5f. P6 Skills 能力（新增，2026-10-07）

实际命令（本机）：

- `python3 -m unittest discover -s tests` → **486 项全部通过**（交接 52 +
  P1 125 + P2 76 + P3 58 + P4 63 + P5 61 + P6 新增 51：skills 13、gws 19、
  net 12、connectors 7），参考 demo 输出不变；审计复现 OK；license gate
  PASS（96 tracked files）。
- `python3 examples/skills_loop_demo.py` → 四场景断言全过：import（真实
  88 语料 audit_match=true、installs=2）、mail_loop（proposed→
  actions_queued→本地 inbox 1 条）、calendar_loop（tick1 proposed、tick2
  suppressed `l0_no_source_change`、模型调用共 1 次）、material_loop
  （tick1 proposed、tick2 suppressed 零模型）。
- TS：`tsc --strict --outDir /tmp/pas-ts-build examples/pi_executor.ts
  tests/pi_contract_test.ts tests/pi_extension_contract_test.ts
  examples/pas_pi_extension/index.ts` → 4 + 6 项契约检查通过；
  `tsc -p packages/client-ts/tsconfig.json` 照旧通过。

88 入口审计一致性（private-vendor Muse 快照存在时执行；缺席则显式 skip）：

- `audit_consistency(importer.scan→report, audit/skills.json)` →
  **match=true**：count 88；issue_counts {invalid_name 43,
  name_directory_mismatch 41, metadata_values_not_all_strings 88,
  invalid_description 1} 与审计完全一致；全部 source_path/sha256/
  original_name/canonical_name_proposed 逐项一致；零多余路径。
- gmail → capabilities [gmail.read]、tools [hatch_gws_cli]、grants
  [selected_mail_account]；google-calendar → [calendar.read]/…
  [selected_calendar_account]（§11.3 sidecar）。

P6 新增覆盖：

- **skills（13）**：frontmatter 三种形态 + 续行 + 缺失记录；canonical
  无碰撞（gmail/gmail-2）与 aliases；嵌套 artifacts 共享 references 进
  闭包；sidecar 结构与 distribution 独立（permission_unverified）；store
  幂等推进与异 hash 冲突；符号链接逃逸拒绝；真实语料审计一致性。
- **gws（19）**：非 hatch/未知 service/未知命令/写命令（+send、
  events.insert）→ unsupported；+triage 需 query、--max 限 50（成本
  口径）、+read 需 id、坏 flag 类型化拒绝；not_connected 转发 provider
  connect_url、无 URL → unavailable 且 `connect_url` 恒 None（不编造）；
  `--for-command` 透传；provider auth 错误 → reauth_required；连接器
  崩溃只泄类名。
- **net（12）**：allowlist 强制、http/非 443/URL 凭据拒绝、无显式
  allow_loopback 时回环拒绝（SSRF guard）、子域策略通过但传输仍受门；
  回环 TLS 快乐路径（openssl 自签 + CA 装载，校验开启）经钉扎 IP 取回
  正文；重定向/超尺寸/不允许 content-type 拒绝。
- **connectors（7）**：not_connected 无 URL → AUTH_REQUIRED（"unavailable"，
  异常文本不含任何 https://，即不编造 URL）；带 URL 时如实报告存在；
  delta cursor：相同页面 → 空批、变化页 → 同 fact_id 新 revision；
  账户绑定校验；公开资料 sensitivity=public。
- **pi extension TS（6）**：官方 registerTool 注册 5 工具；schedule 落
  PAS（jobs.create 到达脚本服务器）；status/skills inspect 正常；未配置
  fail closed 且安全消息无 URL 泄漏。

P6 未做/边界（如实记录）：gmail/calendar/outlook 的 `e2e_verified` 与
真实 Google/Microsoft 授权联调未做（无授权凭据；连接流程属部署方）；
Hermes 插件与 Pi extension 未安装进活跃 profile（操作者决定 + PAS
daemon P7）；EgressBroker 不跟随重定向（目标主机重评不需要）、TOCTOU
双解析比对未做；大附件分块/外置 blob 策略留 P7（gmail +read 只回
inventory 元数据，附件字节不入 PAS）；`pas skills import/explain` CLI
壳属 P7 facade； Muse 全量目录不内置（BYO 模式）。

## 5g. P7 产品化与发布（新增，2026-10-07）

实际命令（本机 macOS，Python 3.14.4 / SQLite 3.50.4）：

- `python3 -m unittest discover -s tests` → **532 项全部通过**（交接 52 +
  P1 125 + P2 76 + P3 58 + P4 63 + P5 61 + P6 51 + P7 新增 46：
  test_p7_product 27 + test_p7_daemon 19）。参考 demo（reference_core /
  agent_loop_demo / policy_delivery_demo / skills_loop_demo）输出不变；
  审计复现 OK；license gate PASS。
- `python3 -m pip wheel . -w dist --no-deps` → wheel 构建成功；
  `python3 tools/package_gate.py dist/*.whl` → **PACKAGE GATE: PASS**
  （对 wheel 成员做禁止路径（private-vendor/muse 系）、Muse 审计 hash、
  密钥形文件名、完整 PEM 私钥块（header+base64 body+END，避免把本包
  自身的脱敏正则误判为密钥）扫描——扫的是产物本体，不是 git）。
- `python3 tools/gen_sbom.py dist/*.whl -o dist/sbom.cdx.json` →
  CycloneDX 1.5 SBOM 生成（包自身 + optional extras + 逐成员 sha256；
  运行时依赖如实记录为 stdlib only）。
- `python3 tools/install_smoke.py dist` → **INSTALL SMOKE: PASS**（全新
  venv → 装 wheel → `pas --help`/`version` → `doctor --json` 全绿 →
  jobs create/list/backup/restore/export → 嵌入模式 tick（脚本化
  executor，零模型调用）→ 全生命周期关闭）。
- `tsc -p packages/client-ts/tsconfig.json`（strict + 
  noUncheckedIndexedAccess + exactOptionalPropertyTypes）通过；
  pi_executor/pi_extension 契约检查照旧通过。

P7 新增覆盖（对应 SPEC §14 / §15.1 P7 行 / §17 / OPS-01）：

- **facade（§14.1）**：tick 全链（准入→L0/L1→策略→派发；无到期任务零
  模型调用；同一时钟推进一步只跑一个槽）；jobs CRUD + pause（暂停拒绝
  手动触发）/resume/manual trigger；runs.cancel 三态如实（queued→
  suppressed 原子、running→cancel_requested、终态→not_cancellable，
  cancel_requested 落库）；grants 仅可信入口、profile 不匹配 reopen
  拒绝；export/delete（确认短语强制）；context manager 关闭 store。
- **daemon（§17.1）**：单实例锁（活锁拒绝/死 pid 接管/不可读
  fail-closed）；嵌入 start/stop 与 serve 同锁仲裁；**重启故障——真实
  子进程 kill -9 在途 run，重启实例 tick 以 attempt+1 回收且不产生
  completed**；force stop 报告 interrupted 且无 run 被改写为 completed；
  health.json 每轮原子刷新、ready 无降级原因。
- **控制面（§14.2）**：Unix socket 0600；20 个 §14.2 方法全绑定（jobs
  create/list/pause/resume/delete、runs list、notifications.list、
  system.hello/health 在测试中逐条应答）；非 hello 首帧 → auth_required
  并断连；token 认证单测（正确 token → principal token: 前缀、错
  token/缺 token 拒绝，constant-time 路径）；同 UID peer 凭据信任。
- **备份恢复（OPS-01）**：roundtrip 行数一致 + 0600 权限 + sha256
  sidecar；restore 拒绝：profile 不匹配、daemon 锁存在、meta 声称
  schema 比数据库新（999）、payload 单字节篡改（hash 不符）。
- **可观察性（§17.2）**：日志行为结构化 JSON 且 bearer/sk- 密钥被
  [REDACTED]（写前脱敏，测试含"不落盘未脱敏"探针）；指标名与 SPEC
  对齐；health 降级原因（disk_low/delivery_unknown/scheduler_stale）
  可机读。
- **config（§14.3）**：全量样例加载、未知顶层/嵌套键拒绝、坏时区/坏
  时间窗/坏 misfire 拒绝、tab 与重复键拒绝、redacted 视图无家目录路径、
  YAML 子集 roundtrip；CLI `config check/print` 同路径。
- **CLI**：help/version/doctor（缺 state 目录如实 exit 1 并点名）、
  jobs create/list/trigger、runs cancel（queued→cancelled）、backup/
  restore（无 --yes 拒绝 exit 2）、export、delete-data（无 --yes
  拒绝）、config 未知键 exit 2。

P7 未做/边界（如实记录）：

- **真实用户流程人工回归未做**（SPEC P7 行最后一项验收需要真人按
  README 用例操作——机器门禁全部就绪，人工回归待操作者执行）。
- **未发布**：无 PyPI/npm 上传动作（自用项目）；wheel/sdist 构建与
  产物门禁就绪，"包发布"在自用语境下落地为可构建+可扫描+可安装 smoke。
- 控制面远程 TLS HTTP 未实现；`event_retention_days` 的运行账本清扫
  未实现（决策账本保留至 export/delete）；大附件分块/外置 blob 未实现；
  runs.cancel 不触及 actions_queued 之后的 outbox 消息（语义边界在
  SECURITY.md/COMPATIBILITY.md 注明）；skills.explain 不做运行时 PATH
  探测。
- daemon 的 hook 执行在事件循环内联运行（store 连接绑定创建线程；
  runner 本就单飞）——长 hook 会推迟同拍后续任务，属已知取舍。
- P5 遗留移交至此关闭：PAS RPC 服务端 facade（本轮 §14.2 绑定）、
  `pas skills import/explain` CLI 壳（本轮 skills 子命令）、控制面
  bearer/token 认证（本轮 token_file）；client-ts npm 发布仍未做（无
  registry 账号，见上"未发布"）。

## 6. v0.1.1 优化（2026-10-07）

范围：`SPEC.md` §21 的九步实施计划。本节只记录本次实际执行的检查；
"未做/未验证"列在末尾，凭据或宿主环境缺失的项一律标为阻塞，不计入完成。

### 6.1 实际命令与结果

```bash
python3 -m unittest discover -s tests
python3 examples/reference_core.py
python3 examples/agent_loop_demo.py
python3 examples/policy_delivery_demo.py
python3 examples/skills_loop_demo.py
python3 examples/reminder_demo.py
python3 tools/reproduce_audit.py
python3 tools/license_gate.py
npm exec --yes --package=typescript@5.8.3 -- tsc --strict --target ES2022 \
  --module commonjs --lib ES2022,DOM --outDir /tmp/pas-ts-build \
  examples/pi_executor.ts tests/pi_contract_test.ts \
  tests/pi_extension_contract_test.ts examples/pas_pi_extension/index.ts
node /tmp/pas-ts-build/tests/pi_contract_test.js
node /tmp/pas-ts-build/tests/pi_extension_contract_test.js
npm exec --yes --package=typescript@5.8.3 -- tsc -p packages/client-ts/tsconfig.json --noEmit
python3 -m pip wheel . -w /tmp/pas-dist --no-deps
python3 tools/package_gate.py /tmp/pas-dist/proactive_sdk-0.1.1-py3-none-any.whl
python3 tools/gen_sbom.py /tmp/pas-dist/proactive_sdk-0.1.1-py3-none-any.whl -o /tmp/pas-dist/sbom.json
python3 tools/install_smoke.py /tmp/pas-dist
```

| 检查 | 结果 |
|---|---|
| `python3 -m unittest discover -s tests` | **608 项全部通过**（v0.1.0 的 532 + v0.1.1 新增 76：reminders 21、v011_semantics 34、semantics_matrix 11、migration_v011 3、client_ts_drift 7） |
| `examples/reference_core.py` | 输出与 v0.1.0 一致：`{"delivered_notifications":0,"events":1,"pending_notifications":1,"runs":1}` |
| `examples/agent_loop_demo.py` | 三场景输出与 v0.1.0 一致 |
| `examples/policy_delivery_demo.py` | 全部场景通过 |
| `examples/skills_loop_demo.py` | 两条链路输出与 v0.1.0 一致 |
| `examples/reminder_demo.py` | `REMINDER DEMO: PASS (0 model calls, 0 runs, 1 delivered message)` |
| `tools/reproduce_audit.py` | `skills_audit: OK`、`source_manifest: OK`（88/88） |
| `tools/license_gate.py` | `license_gate: PASS (131 tracked files scanned, 99 forbidden hashes, gate status pass)` |
| TypeScript strict 编译（Pi 示例 + 契约测试） | 编译通过；`4 structural Pi contract checks passed`、`6 Pi extension contract checks passed` |
| `tsc -p packages/client-ts/tsconfig.json` | 通过（strict + noUncheckedIndexedAccess + exactOptionalPropertyTypes） |
| wheel 构建 | `proactive_sdk-0.1.1-py3-none-any.whl` |
| `tools/package_gate.py` | `PACKAGE GATE: PASS (1 archive(s))` |
| `tools/gen_sbom.py` | CycloneDX 1.5 SBOM 生成，`pkg:pypi/proactive-sdk@0.1.1`，0 运行时依赖 |
| `tools/install_smoke.py` | `INSTALL SMOKE: PASS`（fresh venv 内 `pas version` 报 `Museion Agent SDK v0.1.1`） |

### 6.2 新增断言覆盖（对应 §21.1 九步）

- **第 1 步（语义矩阵与基线）**：`docs/SEMANTIC_MATRIX.md` 冻结五类语义、
  七条不变量与 v0.1.0 行为清单；`tests/test_semantics_matrix.py` 固定
  heartbeat 无来源/无变化/未授权时的零模型调用、显式任务无来源仍运行、
  来源全失败为可见失败、host owner 不被 PAS 调度、mode 派生默认 misfire、
  同 occurrence 幂等、暂停跳过、业务键去重语义。
- **第 2 步（确定时间直接提醒）**：`mode="reminder"` 的契约校验（正文、
  时区、owner channel、grant、幂等 key、未知键拒绝、禁止 `task.instruction`
  与 reminder 混用）；迁移 007 放宽 `jobs.mode` 与 `actions.run_id`；
  store 单事务「复核—occ+action+outbox—推进 next_due」；到点零模型调用、
  零 run 行；重复接纳/重启恢复/PAS-host 竞争均不双发；暂停/停止/grant
  撤销/删除历史保护。
- **第 3 步（义务与机会型分流）**：`obligation` 只由可信配置赋予，proposal
  无法升级；`due` 提醒不被 cadence 偏好吞掉；静默时段与日配额把提醒
  **延后并保留可见状态**（`reason=quiet_hours` / `daily_quota`），
  超窗不可补发时记 `expired_before_window_open`；跨夜/跨 DST 的本地换日
  用日历重解（DST 日 25 小时）而非固定 86 400 000 ms；`topic_muted` 是
  硬门禁且留可查原因。
- **第 4 步（迟到与错过）**：计划时间、实际时间、`lateness_ms` 与仅已知
  原因（`catch_up_within_grace`）分别落库；超 grace 记
  `missed_beyond_grace episode_slots=n` 且 occurrence 为 `expired`；
  runonce 在目标时刻之后创建时不再被静默停放（v0.1.1 修正）。
- **第 5 步（时效来源刷新）**：`task.refresh_source_ids` 定向读取（命名
  了未注册来源即报错）；`reminder.refresh_sources` 在投递前定向复读：
  来源不可用 → 可恢复失败并延后（不凭旧快照硬发），授权撤销/事实
  tombstone → 抑制并留原因，`fresh_until` 已过 → 延后；纯冻结提醒
  不发起任何来源请求。
- **第 6 步（近 24 小时语义查重）**：`ContextPack.recent_notifications`
  有界（≤20）、脱敏（无正文，destination 折叠为 channel kind，fact 只留
  摘要）；窗口半开（恰好 24 小时前的消息不在内）；只有真正投递成功的
  消息进入摘要；渲染进模型上下文并标注为 DATA。
- **第 7 步（持续任务与活动记录）**：`job_activity` 投影按 phase 分离
  `analysis`（proposed/suppressed/failed + 原因，例如
  `l0_nothing_to_check`）、`action` 与 `delivery`（delivered/accepted/
  unknown/queued/failed）以及 `missed`（含 `topic_muted`）；两种 job
  类型都投影（提醒走 occurrence，Agent 运行走 run/event 链）；投递行按
  (job, state, message) 幂等，同一 message 的重复尝试不重复计数，不同
  message 不会被折叠；`jobs_delete` 返 `action=deleted|stopped`，有审计
  历史只停止追踪，store 拒绝物理删除；`pas jobs stop|activity|activity
  <id>`、`jobs.stop`/`jobs.activity` RPC 与 client-ts 同步。
  边界：`jobs create`（CLI/RPC）不创建 grant，直接提醒需要先用可信
  consent 路径创建 `notify.self` grant，否则以 `invalid_config` 明确拒绝。
- **第 8 步（宿主体验与产物入口）**：`cadence` 名称校验、无数字时不做
  任何额外门禁、有 `cadence_min_gap_seconds` 时只延后机会型触达；
  `artifact:<相对路径>` 引用语法拒绝绝对路径/驱动器/traversal/超长；
  proposal 与 reminder 的每个 `artifact_refs` 必须出现在正文里，否则
  明确拒绝。
- **第 9 步（整体验收）**：见 §6.1 全量命令；迁移 007 从真实 v0.1.0
  数据库（schema 6 + 代表性行）原地升级：行数不丢、`PRAGMA
  foreign_key_check` 为空、`foreign_keys` 恢复为 ON、子表 REFERENCES
  仍指向重建后的表、重开不再重复迁移。同时修正了一个数据删除缺陷：
  `wipe_profile_data` 原先按错误的父子顺序删除并吞掉错误，导致
  `events`/`jobs` 被静默跳过（用户被告知数据已清除，实际仍有残留）；
  现在按 FK 图严格先子后父，并在提交前用 `foreign_key_check` 校验，
  新增的 `job_occurrences`/`job_activity` 也纳入导出与清除。另一个
  被修正的静默缺陷：profile 级 `policy.notification_window` 默认值写入的是
  `quiet_hours_timezone`，而窗口解析只读裸 `timezone` 键，导致未自声明
  时区的 job 实际上没有任何静默时段（默认值被接受后又被忽略）。

另有一项 CLI 冒烟（同一 state 目录）：

```bash
PYTHONPATH=src python3 -m proactive_sdk.service --state-dir /tmp/pas-cli jobs list --json
PYTHONPATH=src python3 -m proactive_sdk.service --state-dir /tmp/pas-cli activity --json
PYTHONPATH=src python3 -m proactive_sdk.service --state-dir /tmp/pas-cli jobs stop nope
```

→ `{"count": 0, "jobs": []}`；`{"activity": [], "count": 0}`；
`error: [invalid_config] unknown job 'nope'`（exit 2）。

### 6.3 v0.1.1 未做 / 未验证（阻塞项，不计为完成）

- **真实日历/通知渠道授权联调未做**：第 5 步的投递前复读用脚本来源
  (`Source`) 驱动真实代码路径，未连接真实日历或 push 服务；不得写作
  已完成端到端兼容。
- **真实用户流程人工回归未做**（P7 遗留）：机器门禁就绪，人工回归
  待操作者执行。
- **性能与模型调用数未标环境**：本次只断言"零模型调用"这一离散事实，
  未测吞吐/延迟；无环境标注的数字一律不写为已达到。
- **cadence 数值**：SDK 不提供任何默认小时数（SPEC §21.1 第 8 步要求）；
  实际节奏数值须由宿主配置提供，本次未对任何宿主的节奏做产线调参。
- **Pi/Hermes 真实服务复测未做**：本轮改动不触及 P5 适配器，未重跑
  §5e 的锁定版本真实服务探针（保持 P5 当时的结论，不重新声明）。
- **客户端分发**：client-ts 与新增 RPC 方法已同步且类型检查通过，
  仍未发布到 npm registry。

## 7. v0.1.2 优化（已完成，2026-10-07）

范围：`SPEC.md` §22 的十步实施计划。**本节记录已完成部分**；第 4–10 步完成后补
充，未完成的项一律标明，不并入"已完成"。

### 7.1 第 1–3 步：内核接口 + 宿主接入层

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | 执行器 seam 收口：`executor.RunExecutor` / `ExecutorContext`；coordinator 不再读 `.broker` / `.config`；broker 改鸭子类型 | 完成 |
| 2 | 决策契约一等化：`decision_contract.py`（版本 `1.0`，10 条规则，逐条绑定到真正执行它的那一层） | 完成 |
| 3 | 通用宿主桥：`host_bridge.py` + `host_drivers.py`（Hermes / Pi 两个驱动）+ `decision_parse.py` + `render_context_message` | 完成 |

实际命令：

```bash
python3 -m unittest discover -s tests
python3 examples/reference_core.py
python3 examples/agent_loop_demo.py
python3 examples/policy_delivery_demo.py
python3 examples/skills_loop_demo.py
python3 examples/reminder_demo.py
```

→ **673 项测试全部通过**（v0.1.1 的 608 + v0.1.2 新增 120）；五个示例输出不变。

### 7.2 第 3 步的真实验证（锁定宿主，8/8 PASS）

在 P5 验证主机（Linux / Ubuntu 24.04 / Azure）上运行，通过**已在运行的 Hermes
gateway**的 profile 隔离端点，**未升级、未重启任何组件**：

```bash
# 在验证主机上，当前 src/ 部署于 ~/pas-v012/
python3 tools/validate_v012_real.py
```

| 探针 | 结果 | 证据 |
|---|---|---|
| `hermes.config` | PASS | Runs API key 由脚本在主机上就地读取（`key_source=profile-config`），未打印、未离开该主机 |
| `hermes.capabilities` | PASS | 30 项 feature，含 `run_submission` / `run_status` / `run_stop` / `runs_idempotency`；fail-closed 检查通过 |
| `hermes.bridge_envelope` | PASS | **纯自然语言 instruction** → 合法 `silent` 信封，`pricing_basis=measured` |
| `hermes.contract_injected` | PASS | 捕获到实际 POST 的 `instructions` 共 1557 字符，含契约首行与原始 instruction |
| `hermes.cancel` | PASS | `level=confirmed` + `settled=cancelled` |
| `pi.initialize` | PASS | `pi_version=1.0.4`（与锁定版本一致） |
| `pi.bridge_envelope` | PASS | 同一套桥 → 合法信封；`input_tokens=1675`、`output_tokens=193`、`pricing_basis=measured` |
| `pi.cancel` | PASS | `level=requested` + `settled=errored(error=conflict)`：宿主未确认停止，PAS 不伪造完成 |

**版本核对**：Hermes `v0.21.5+8493.g9b38eb1 (2026.9.24)`、Pi `1.0.4`，与
`compatibility-lock.json` 逐字一致，**锁定文件无需改动**。

**已消耗**：本次约 6 次模型调用（2 次 Hermes 完成、1 次 Hermes 取消、1 次 Pi
完成、1 次 Pi 取消、1 次重跑）。

### 7.3 真实验证暴露并修掉的缺陷

- **`PiHostDriver.capabilities()` 在连接前返回空字典**。空字典读起来是"这个宿主
  什么都不会"，而不是"还没问过"。首次真机运行即失败（`pi_version: null`）。已
  改为：`capabilities()` 先建立会话再回答，并补一条测试钉住。
- **`PiHostDriver.cancel()` 把"已结束的 run"报成 `unsupported`**。宿主对已终止
  的 run 回 `no active run`，映射成 `unsupported` 会让人以为"这个宿主根本不支持
  取消"。已改为：驱动自己观察过终态的 run，取消时直接回 `confirmed`（它确实不在
  运行），并补测试。

两条都是**只有真机才能发现**的问题：用脚本替身时，替身不会"因为还没连接而返回
空"，也不会"对已完成的 run 报错"。

### 7.4 第 4–9 步：宿主形态、工具主权、用户唤醒授权、建议回路、真实推送、装配体验

| 步 | 内容 | 状态 |
|---|---|---|
| 4 | 宿主形态补齐：`CallableHostDriver`（库内型）+ `SubprocessHostDriver`（黑盒型）+ `extract_envelope` | 完成 |
| 5 | 工具主权契约化：`runs.tool_authority`（迁移 008）+ 每 run 记录 + `status` 汇总 | 完成 |
| 6 | 非 job 唤醒的授权上下文：`events.authz_json`（迁移 009）+ `admit_user_wake` + `note_user_input` + `pas input` + `input.note` RPC | 完成 |
| 7 | `suggest_watch` 回路：`watch_suggestions`（迁移 010）+ 冻结参数 + `claim`/`attach`/`decline` + `pas suggestions` + `suggestions.list`/`suggestions.resolve` RPC | 完成 |
| 8 | 真实通知通道：`tools/push_receiver.py`（真人可读的真实接收端）+ `tools/validate_v012_push.py`（真机 5/5）+ dispatcher sink 注册修复 | 完成 |
| 9 | 装配体验：`@tool` 从类型注解推导 schema、三个内置只读工具、`ProactiveAgent(model=…)` 自动装配、`agent.grant()`、`Job.revision`、`examples/quickstart.py` + `examples/restart_after_three_days.py` | 完成 |

#### 7.4.1 步骤 4–6 暴露并修掉的既有缺陷

**`silent` 决策 + `PolicyEngine` 被记成 `policy_error`。** `Store.record_policy_verdicts`
在 `verdicts` 为空时抛 `invalid_config`，而零提案正是 `silent` 决策的形态——也就是
机会型 heartbeat 最常见的结局。现象是 run 停在 `proposed`（可重试）并报策略失败。

成因是**覆盖盲区**，不是难度：全仓没有任何测试同时覆盖 `silent` 与
`policy_engine`（`test_policy.py` 里两处 `policy_engine=` 用的都是 `propose`；
`test_coordinator.py` 大多不接 policy）。已修并补回归测试。

#### 7.4.2 步骤 7 暴露并修掉的缺陷

**"已拒绝的建议"仍会创建任务。** 原实现先创建 job、再调用状态守卫，于是
`decline` 之后再来一次 `accept`，job 会在冲突抛出**之前**就存在——用户明确的
拒绝被违反。测试 `test_resolving_twice_is_a_conflict_not_an_overwrite` 抓到。

改成**先原子认领（`pending → accepted`）、后创建 job**：冲突在任何东西被创建
之前抛出。代价是"认领成功但 job 创建失败"会留下一条无 job 的 accepted 记录，
因此认领对"已 accepted 且无 job"的重复调用是**可恢复**的,而不是再次冲突。
两种失败模式里,只有一种会违反用户意愿,所以选了它。

#### 7.4.3 第 6 步的真实验证（4/4 PASS）

在锁定宿主上运行。**同一个真实模型、同一个来源、同一套 policy，只有入口不同**：

```bash
# 在验证主机上
python3 tools/validate_v012_userwake.py
```

| 探针 | 结果 | 证据 |
|---|---|---|
| `generic_manual_event.event_unbound` | PASS | 普通 `admit_event` 写入的事件 `authorization is None` |
| `generic_manual_event` | PASS | 模型提出 1 条通知 → verdict `grant_missing` → **outbox 0 / inbox 0** |
| `trusted_note.event_bound` | PASS | `note_user_input` 绑定到 `local-inbox:v012-userwake` |
| `trusted_note` | PASS | 同一模型、同一来源提出 1 条通知 → 入队 → **outbox 1 / inbox 1** |

两次运行的模型摘要（原始输出）分别说明它**理解了自己在做什么**：

- 通用入口：*"PR #1234 刚刚由 alice 合并进 main，符合用户设定的合并即通知条件，阻塞解除。"* → 提案被 `grant_missing` 拒绝
- 可信入口：*"快照显示 PR #1234 刚由 alice 合并进 main，正是任务设定的通知触发条件，应立即通知用户"* → 入队并落进本人 inbox

**第一次探针的失败也是有价值的证据。** 最初版本只给宿主一句话和一条记忆、
没有可观察的变化，真实模型在两个场景都返回 `silent` 并给出理由：

> "本轮无任何 PR #1234 的源数据或快照，未观察到合并事件；上下文中没有可引用的
> 证据，不满足 notify_self 的证据要求，保持静默"

即**证据闭包在真机上是生效的**：一句笔记不是一次事件。探针随后改为提供一个
（脚本化的、明确标注的）来源来报告合并，被测对象仍是授权绑定而非来源连通性。

#### 7.4.4 第 9 步暴露并修掉的缺陷

**一个 task 任务错过的 slot,在面向用户的投影里看不见。** 数据一直都在——
`record_missed_occurrence` 写 `job_occurrences`(state=`expired`),但只有
reminder 路径会写 `job_activity`。于是 `pas activity` / `jobs.activity` RPC
——也就是"我不在的时候有没有漏掉什么"这个问题的两个用户可见入口——
**只覆盖 reminder**,对 task 与 heartbeat 完全沉默。而"关机三天后重启"
恰恰是这类任务的主场。

修法:`record_missed_occurrence` 在首次插入 occurrence 时同写一行
`job_activity`(phase/state 都是 `missed`,带 `episode_slots`)。数据不新增,
只是让两个投影说同一件事。

写这个修复时发现:`record_missed_occurrence` 原来用的是 `INSERT OR IGNORE`,
所以必须靠 `cursor.rowcount == 1` 判断"本次真的新插入了"。否则每个 tick 重复
扫描同一段 episode 都会再写一行 activity。测试
`test_a_task_jobs_missed_episode_is_user_visible` 断言恰好一行。

**`Job` 表达不了"改一个 job"。** 便利类型没有 `revision` 字段,`to_spec()`
把它硬编码为 1。结果是:内容一致时幂等没问题,但**任何编辑都会撞上
`job revision 1 does not follow stored revision 1`**,调用方必须掉到 `JobSpec`
才能改一行 instruction。对"开箱即用"是硬伤。已加 `revision: int = 1` 并透传,
纯增量、向后兼容。

#### 7.4.5 第 9 步的取舍说明

**`@tool` 不猜。** 参数缺类型注解、或类型无法映射到 JSON Schema,都在导入期报错,
而不是生成一个近似 schema。模型看到的就是这份 schema,近似等于对该工具的能力撒谎。

**内置工具带 capability,而不是免检。** `current_time` 不需要授权;
`recall_memory` 要 `memory.read`,`list_recent_activity` 要 `state.read`。
所以自动装配**不能**把 broker 的能力集设成"这些工具声明的并集"——那会让
`required_capability` 看起来在检查、实际CheckIn为空。broker 的能力来源是
**当前活跃 grant 的实时快照**(`LocalToolBroker` 现在接受可调用对象),
新授权立刻生效、撤销立刻失效,两边都有测试钉住。

**两个便利入口是薄的,且不改语义。** `agent.grant()` 只是
`create_grant_from_user_consent` 的短签名,docstring 里写明**只能由用户自己的
代码调用**;`jobs_upsert(idempotency_key=None)` 按内容推导——replay 安全,
而 revision 仍然是调用方的显式动作,内容变了但不 bump 会被拒(有测试)。

#### 7.4.6 第 4–9 步的成本与边界

- 第 4–9 步真机调用共约 13 次（第 6 步 4 次 + 第 8 步探查与验证 9 次）；额度 100，累计约 19 次
- 全部通过 loopback 的 profile 隔离端点，**未触碰 live `default` profile**，
  未升级或重启任何组件；凭据留在验证主机上
- 部署只上传 `src/`（70 个 py 文件），已确认不含 `private-vendor/` /
  `muse-refer/` / `audit/`
- 步骤 4 的黑盒宿主形态用**真实子进程**验证，但那个子进程是测试夹具；
  第 3 步的真实 Hermes/Pi 验证才是真实宿主证据
- 第 8 步的推送接收端是我们自己的服务，证明的是投递链路与 provider 契约，
  **不是**与任何已部署第三方推送服务的互通
- 第 10 步的验收结果与未验证项见 §7.6
- 第 9 步的 quickstart 与重启示例有子进程级测试保证"无凭据也能跑通"

#### 7.4.7 第 8 步的真实验证（5/5 PASS）

机制其实**已经存在**：`WebhookNotificationSink` 逐消息复用 `Idempotency-Key`、dispatcher 对
`delivery_unknown` 先对账再决定、`push_summary_only` 在 `policy._notification_payload`
里对 agent 通知同样生效。缺的是**在一个真实 socket 上、对一个真人可读的接收端**跑一遍——
这就是 `tools/push_receiver.py`。它不是 mock:真实 HTTP 服务、真实网络栈、真实落盘。

```bash
# 在验证主机上（真实 Hermes + 真实模型 + 真实接收端）
python3 tools/validate_v012_push.py
```

| 探针 | 结果 | 证据 |
|---|---|---|
| `push.delivered` | PASS | 真实投递到达并落盘，`external_id` 由接收端签发 |
| `push.lockscreen_minimised` | PASS | 线上真实载荷的键为 `fact_id/kind/revision/run_id/semantic/title`——**没有 `body`** |
| `push.provider_idempotent` | PASS | 同一 `provider_key` 重放 → 接收端吸收，记录数仍为 1，`external_id` 不变 |
| `push.lost_ack_reconciled` | PASS | ACK 真实丢失（`dropped_acks=1`）→ 落账为 `reconciled_delivered` |
| `push.unknown_is_not_blind_retried` | PASS | 该消息不会回到 `pending`，不会被重发 |

**真机收到的东西（人可读，节选）**：

```json
{"external_id": "ext-e757dc792996466d",
 "payload": {"kind": "notify_self", "fact_id": "pr-1234", "revision": "merged",
             "semantic": "notify_self",
             "title": "跟踪项有动静：PR #1234 刚被 alice 合并进 main，符合立即推送条件；
                       PR #5678 本次快照中无评审进展，暂不推送。"},
 "provider_key": "pas-acta905c27a696b734de041679f015b"}
```

**一条意外的好消息。** 探针原来假设会观察到一次 `delivery_unknown`，实际观察到的是
`reconciled_delivered`——因为 `facade.tick()` 在**同一个 tick 内**就跑了
`reconcile_unknowns`（`facade.py:363`）。也就是说系统连一个 tick 都没让消息停在
unknown。是探针测量时机错了，不是系统错了。

**没有验证的**：与任何**已部署**的推送服务（ntfy/Bark/Telegram/…）的兼容性。
接收端是我们自己的，证明的是投递链路与 provider 契约，不是与第三方服务的互通。

#### 7.4.8 第 8 步暴露并修掉的缺陷

**调用方无法提供自己的 webhook 传输。** `OutboxDispatcher.__init__` 预注册了默认
`WebhookNotificationSink`，而 `register_sink` 见键即冲突——于是 facade 的 `sinks=`
参数对**最常见的那种 channel** 完全不可用。已改为：可替换默认、同一实例重复注册幂等、
两个调用方传输仍冲突（静默保留其中之一会从调用方没选的传输发出消息）。3 条测试。

#### 7.4.9 发现但**未**修复的一处（交第 10 步决定）

`outbox.attempts` 是**重试计数器**（仅在 `failed_retryable` 时自增，供
`attempts >= max_attempts` 的重试预算使用），不是投递次数。一条首发即成功的消息
`attempts = 0`，读起来像"从未尝试过"。总次数在 `delivery_attempts` 日志里，那是权威记录。

语义本身没错，属于命名/可观测性问题。改列名需要迁移，风险大于收益，故**记录而不修改**。

### 7.5 第 1–3 步的成本与边界

- 真实验证只使用了 loopback 上的 profile 隔离端点；**未触碰 live `default`
  profile**，未修改任何 Hermes/Pi 组件或配置
- 凭据（Runs API key、provider key）全程留在验证主机上，未出现在任何输出中
- 第 4–10 步尚未完成，本节不作为 v0.1.2 的完成声明

## 8. 明确未做（交接包历史记录，继续有效）

交接包阶段（至 2026-10-06）没有对真实 Hermes gateway、真实 Pi SDK、Muse 后台、邮箱、日历、设备、push 服务或付费模型执行联调；没有发布、安装或提交到用户的仓库；没有验证全部 88 个 Skill 的实际功能；没有完成第三方代码再分发授权核验。

**2026-10-07 P5 更新**：Hermes Runs gateway 与 Pi SDK 已按锁定版本完成真实服务联调（见 §5e，8/8 探针 PASS，含真实付费 provider 的微小调用）；邮箱、日历、设备、push 服务仍未联调；88 个 Skill 仍未验证；未完成第三方代码再分发授权核验。

生产版本的完成标准以 SPEC P0–P7 和第 16 节为准。测试桩通过不能用来抹掉上述缺口。
### 7.6 第 10 步：v0.1.2 整体验收

#### 7.6.1 门禁结果

| 门禁 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `python3 -m unittest discover -s tests` | **793 tests OK** |
| 七个示例 | `python3 examples/<name>.py` | 全部 PASS（含 quickstart 与关机三天的演示） |
| TypeScript 编译 | `npx tsc -p packages/client-ts/tsconfig.json --noEmit` | PASS |
| 生成物漂移 | `python3 tools/gen_client_ts.py` 二次运行 | 哈希不变（生成稳定） |
| 许可门禁 | `python3 tools/license_gate.py` | PASS（131 tracked files / 99 forbidden hashes） |
| 包门禁 | `python3 tools/package_gate.py dist/*.whl dist/*.tar.gz` | PASS（2 archives） |
| 构建 | `pip wheel . --no-deps` + `build_sdist` | `proactive_sdk-0.1.2-py3-none-any.whl` / `.tar.gz` |
| 安装冒烟 | `python3 tools/install_smoke.py` | PASS（新 venv 安装 → CLI → 嵌入 tick） |
| 隐私扫描 | 自建扫描（树 + 两个产物的全部成员） | PASS，320 files/members，无密钥/令牌 |
| 备份恢复 | `pas backup` → `pas restore --yes`（真实往返） | PASS，`schema_version=10`，jobs 与 grants 完整 |
| daemon 重启 | 真实 daemon 上 `kill -9` → 重启 | 正常：残锁被识别、`recovery_complete` 执行、控制面重听、RPC 可用 |
| CLI/RPC | `pas doctor/status/rpc`（真实 daemon） | `system.capabilities` 报 25 个方法 + `decision_contract_version=1.0` |
| CLI 冒烟 | `tests/test_cli_smoke.py`（每个子命令） | 25 个子命令全部给消息、给退出码,无 Python traceback |

版本标识统一为 **0.1.2**（`pyproject.toml`、`proactive_sdk.__version__`、
`packages/client-ts/package.json`）；`pas version` 输出
`Museion Agent SDK v0.1.2 (protocol 1.0; decision contract 1.0; CLI: pas)`。

#### 7.6.2 §22.2 端到端判据逐条核对

| 判据 | 结论 | 证据 |
|---|---|---|
| 第三方不改源码、按文档实现执行器协议并提供"文本进、文本出"的宿主,即可挂上周期任务 | **通过** | `tests/test_host_forms.py`：真实子进程（对 PAS 一无所知）走完 job → 决策 → 策略闭环；真实 Hermes/Pi 见 §7.2（8/8） |
| 无变化时零模型调用 | **通过** | L0 门在 coordinator 内；`examples/quickstart.py` 第二轮 `runs=0`；`examples/restart_after_three_days.py` 三个任务全程只 1 次调用 |
| 有变化时产出经策略与授权校验的通知 | **通过** | §7.4.7 真机 5/5：来源报告变化 → `notify_self` → 策略 → 真实推送 |
| 用户的一句话可触发有授权的动作 | **通过** | §7.4.3 真机 4/4：可信入口入队进 inbox，通用入口同一句话 `grant_missing` |
| agent 的建议必须经用户确认才能变成任务 | **通过** | `tests/test_watch_suggestions.py::SuggestionIsInertTests` |
| 未完成的部分逐条可查 | **通过** | §7.4.6、§7.4.9、§8 |

#### 7.6.3 第 10 步暴露并修掉的缺陷

**`pas config print` 崩溃。** `AttributeError: 'Namespace' object has no attribute
'config'`——而 `config print` 正是新用户会先跑的命令。成因是结构性的:
`_common_options` 用 `default=argparse.SUPPRESS` 注册 `--state-dir/--config/--json`,
好让写在子命令**之前**的值不被子解析器的默认值覆盖;代价是选项从未给出时
该属性根本不存在,于是直接读 `args.config` 的 handler 会抛异常而不是给消息。

**没有 daemon 时 `pas rpc` 抛出未处理的 `FileNotFoundError`。** 同样属于
"用户先看到的是一条 traceback"。

第二处是**修完第一处立刻抓到的**——新写的 `tests/test_cli_smoke.py` 逐个跑每个子命令、
断言"**永远给消息,不给 Python traceback**",第一次运行就报红。这条不变式是通用的:
按 handler 逐个写测试抓不到下一个同类问题。

**`doctor` 的 `sqlite_foreign_keys` 是空检查。** 它打开一个临时 `:memory:` 连接问
`PRAGMA foreign_keys`,而该 pragma 是**按连接**生效且默认为 0——所以它总是报 0,
断言 `fk in (0, 1)` 接受任何值。改为在真实的 profile 数据库上检查(先确认
`integrity_check`,再断言 pragma 打开成功)。

#### 7.6.4 本次验收**没有**验证的

- 与任何**已部署**的第三方推送服务的互通。第 8 步的接收端是自建的
  (`tools/push_receiver.py`),证明的是投递链路与 provider 契约,不是与
  ntfy/Bark/Telegram 等的兼容性。
- 88 个 Skill 的端到端能力联调仍为 **0 个**(`e2e_verified` 全 false),
  这是既有声明,本次未改变。
- 真实宿主上只做了锁定版本的联调;宿主升级后的重新验证未做。
- `outbox.attempts` 的命名问题(§7.4.9)保持"记录不修改"。

