# 验证记录

日期：2026-10-06。只记录本次实际完成的检查；以下数字不代表成品 SDK 的完整测试覆盖率。

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

## 6. 明确未做（交接包历史记录，继续有效）

没有对真实 Hermes gateway、真实 Pi SDK、Muse 后台、邮箱、日历、设备、push 服务或付费模型执行联调；没有发布、安装或提交到用户的仓库；没有验证全部 88 个 Skill 的实际功能；没有完成第三方代码再分发授权核验。

生产版本的完成标准以 SPEC P0–P7 和第 16 节为准。测试桩通过不能用来抹掉上述缺口。
