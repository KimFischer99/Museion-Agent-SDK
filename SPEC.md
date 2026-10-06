# Proactive Personal Agent SDK — 架构与施工 SPEC

**文档版本：0.1 / 2026-10-06**  
**项目工作名：`proactive-agent-sdk`；下文简称 PAS。** 名称、包名与命令名仅用于设计，不代表已注册或已发布。  
**交付性质：可交接施工的规范、附件审计、参考实现和测试，不是已经完工的 SDK。**  
**主要输入：用户提供的 `muse-sdk.zip`；外部依据为文末列出的官方接口文档。**

## 0. 先读结论与交付边界

项目应实现一个**可以嵌入、也可以独立运行的主动 Agent 运行内核**，而不是把 Muse 目录改个名字发布，也不是让模型在循环中每隔半小时说一次话。

核心链路是：

```text
持久化调度 / 低成本探针
  → 事件去重与原子入队
  → 资格、预算、数据新鲜度检查
  → 只读上下文与 Agent 推理
  → 结构化动作提案
  → 权限 / 审批 / 时段 / 去重判断
  → 持久化 outbox
  → 工具动作或本人通知
  → 回执、结果对账、反馈
```

**四个不能混同的概念：唤醒不等于通知；已提交不等于已完成；已发送不等于用户已阅读；Skill 可被加载不等于其依赖已经可用。**

本规范的默认架构为 **Python 3.11+ 内核 + SQLite + 可选独立守护进程 + 薄 TypeScript 客户端 / Pi 适配器**。Python、Node 的最终支持版本须以 CI 实际验证为准；不要从此文推断所有版本均已兼容。框架适配器不进入核心依赖。第一版不依赖 Redis、消息队列集群、向量库或特定模型厂商。

### 0.1 本交接包已有与尚未完成的内容

| 内容 | 当前状态 | 接手者的责任 |
|---|---|---|
| Muse 附件结构、部分源码和许可审计 | 已完成；88 个 Skill 有逐项机器可读记录 | 复核来源授权，不能将本报告视作授权证明 |
| 核心可靠性参考切片 | 已编写，测试结果见 `VALIDATION.md` | 拆分为生产模块，补齐规范中的其余职责 |
| Hermes HTTP transport 示例 | 文档对齐 + 模拟测试 | 对锁定版本进行真实服务集成测试 |
| Pi structural adapter 示例 | 独立 TypeScript 编译 + 接口桩测试 | 用真实 Pi 包与受控资源加载器绑定并测试 |
| 完整 scheduler、daemon、审批、连接器、sandbox | 未在本次实现 | 按施工阶段完成；不能用 mock 冒充 |
| 全部 88 个 Muse Skill 的端到端能力 | **未实现，也不能由目录复制自动获得** | 分能力补齐适配器并更新兼容矩阵 |
| 公开再分发 Muse 原文件的权利 | 附件不能证明 | 取得明确许可，或不分发这些文件 |

施工顺序：先读本 SPEC，再读 `AUDIT_AND_REUSE.md`；运行参考测试；从 P0 开始按验收条件推进。不得先搭漂亮 UI，再补持久化和权限。

## 1. 对 Muse 素材的判断

附件 README 自述来源为 Muse 云沙盒中的运行文件；本次可以核验附件内容，但**不能独立认证它完整代表 Muse 的生产后台实现**。附件自己也说明决定层位于未采集的编译产物中。以下区分三类依据：

- **A：附件可见事实**，例如 shell helper 的具体行为、Skill frontmatter、心跳配置。
- **W：外部官方接口事实**，例如 Hermes Runs、Pi SDK、Agent Skills 格式。
- **D：本项目设计决定**，例如 outbox、fencing、SQLite 事务边界；不是声称 Muse 内部一定这样实现。

### 1.1 可以确认的机制与缺口

| 附件机制 | PAS 对应实现 | 不能推导出的结论 |
|---|---|---|
| 默认 30 分钟 heartbeat 示例、`HEARTBEAT.md` 清单 | 可配置调度 + 下一轮读取清单版本 | 不是必须每轮调用 LLM，也不是 30 分钟准点保证 |
| hook 的 `silent / wake / disable_after_run` | 无模型探针、事件接纳、一次性监控终止 | helper 没有实现完整调度器或可靠队列 |
| 上下文 brief、sent、pending 等视图 | typed ContextPack + 派生 Markdown 视图 | 文本文件不是事务账本的充分替代 |
| 主动偏好、静默优先、本人通知 | PolicyEngine + UserPreferences + NotificationSink | 编辑偏好文件不能直接授予外部写权限 |
| Skill overlay / 可见性门控 | 能力解析与授权后的 Skill 选择 | release channel 不等于用户授权或安全边界 |
| authd、sentinel、推理代理等接口痕迹 | 独立 CredentialsPort / ApprovalService / ModelPort | 接口描述不等于后台服务已包含在附件 |

附件排除核心 daemon、执行器二进制、凭据与审批服务；不包含可直接调用的 Muse 决策器。它更适合做**行为与兼容接口参考**。[A1–A6]

README 对配置“可编辑”的概括与更具体的平台调度文档存在差异：后者把 cron 文件视为只读投影。本项目采用明确规则：**DB/API 是任务状态的唯一真源；cron Markdown 是导入/导出格式，不是双向自动同步数据库。** `HEARTBEAT.md` 和风格偏好仍可文件化，但其修改不自动改变权限。[A1,A4]

### 1.2 什么叫“通用 Agent SDK”

本规范不宣称存在一个覆盖模型循环、心跳、工具、记忆与投递的统一“通用 Agent SDK 认证”。PAS 定义自己的 **Interoperability Profile v1**，在已有标准覆盖的边界使用标准：

| 边界 | 采用的约定 | 不负责的部分 |
|---|---|---|
| Skill 打包与渐进加载 | Agent Skills 格式；可显式启用 Muse legacy 导入模式 | 不承诺 Skill 的外部服务天然可用 |
| 工具服务 | 可选 MCP adapter，协商其协议版本 | MCP 不是持久化调度器或守护进程 |
| 跨语言控制面 | PAS JSON-RPC 2.0，协议版本协商 | 不把 Pi 自定义 JSONL RPC 当成 JSON-RPC 2.0 |
| 数据结构 | JSON Schema 2020-12；外部时间用带时区 RFC 3339 | 不接受不可解释的自由字符串计划 |
| 包与生命周期 | SemVer、类型声明、迁移、测试、支持矩阵 | 未实测的宿主版本不列为已支持 |

Agent Skills 的格式和 MCP 的工具接口是互补层，而非整个项目的替代品。[W1,W7,W8,W9]

## 2. 产品范围、运行方式与成功条件

### 2.1 三种接入方式

**方式 A：Builder 直接构建个人 Agent。** Builder 提供模型适配器、受控工具与来源，PAS 内置有预算的 ToolLoopExecutor；无需先安装 Hermes 或 Pi。SDK 自己管理会话引用、运行、流式事件、工具调用、暂停与取消。

**方式 B：已有宿主使用主动能力。** Hermes plugin / Pi extension 向 PAS 注册计划、读取状态、接收待处理通知；PAS 持久化执行这些请求。宿主退出后是否仍能运行，取决于 PAS daemon 或另一受监督执行进程是否仍在运行。

**方式 C：PAS 驱动已有宿主。** PAS 持续运行，通过 Hermes Runs 或 Pi worker 执行特定只读分析任务。宿主提供推理和已审核工具，PAS 保留调度、策略和结果账本。

对于同一任务，必须明确 `scheduler_owner = pas | host`。不得同时由 PAS timer、Hermes heartbeat 和原生 cron 发出同一心跳。委托给 host 时仍须把逻辑 occurrence id 带回 PAS 做最终接纳与去重。

### 2.2 v0.1 必须具备

单用户、单 profile、单主机上的完整最小闭环：五类计划；无模型 hooks；上下文增量读取；独立 AgentExecutor；权限与批准；本人 inbox；至少一个真实通知通道；持久化去重；重启恢复；Hermes/Pi 接口；Skill 导入审计；CLI/SDK API；可重复的测试与安装说明。

初版不做跨主机 HA、任意多租户、全量 Muse UI、全部移动端传感器同步，也不做没有用户授权的“自主扩大目标”。这些不是让最小闭环成立的前提。

### 2.3 成功的业务情景

1. “每天上午九点总结我的日程”：按用户 IANA 时区运行；读取授权日历；生成带来源和时效的摘要；只通知本人；重启不重复发昨日内容。
2. “每半小时检查我关心的资料有没有变化”：无变化时零 LLM 调用、零通知；有变化但仍在静默时段则保存待通知项。
3. “有新结果时提醒一次”：探针状态、检测事件和停止监控原子提交；发送失败不让一次性提醒消失；最终通知完成与监控停止是两个状态。
4. “采用 Muse gmail Skill”：可以加载其流程，但只有注册了兼容工具与具体账户授权后才能读邮件；缺依赖明确阻塞，不假装已经连接。
5. 用户中途说“别再追踪了”：禁用任务，撤销后续工具调用和待发送项；已经由外部服务接受的动作须进入对账，不能宣称已被撤回。

## 3. 总体架构与模块职责

```text
                        Host / Builder
          Python API | TS client | Hermes/Pi extension | CLI
                                |
                  Authenticated Control Plane
                   Jobs / Grants / Approvals
                                |
     Clock ----> Scheduler ---> EventStore <--- HookRunner
                                 |                |
                        Admission / Coalescer   Staged state
                                 |
                       RunCoordinator / Leases
                                 |
              ContextBuilder <-> Source & Memory ports
                                 |
                   AgentExecutor / ToolLoopExecutor
                     | Hermes | Pi | custom |
                                 |
                   ActionProposal + evidence refs
                                 |
                  PolicyEngine / ApprovalService
                                 |
                     Action & Delivery Outbox
                      | tools | inbox | channels |
                                 |
                      receipts / reconciliation
```

本图是架构说明，不是附带的可部署系统图。

| 模块 | 必须拥有 | 不应拥有 |
|---|---|---|
| Scheduler | occurrence、时区、漏跑、优先级、下次时间 | LLM 判断、实际发送 |
| HookRunner | 隔离执行、结果解析、staging state | 连接器原始凭据、任意 shell 权限 |
| Store | 事务、唯一键、迁移、租约与账本 | 提示词业务推理 |
| ContextBuilder | 来源、新鲜度、裁剪、快照引用 | 擅自扩大数据范围 |
| Executor | 有界推理、受控工具、结构化最终结果 | 自行跳过授权或直接发消息 |
| PolicyEngine | 账户/动作授权、频率、时段、内容隐私 | 接受模型自报的权限 |
| OutboxDispatcher | 发送尝试、回执、未知结果对账 | 用“生成成功”代替“投递成功” |
| SkillRegistry | 发现、校验、依赖、渐进加载、来源 hash | 将目录存在解释为技能可用 |
| Adapters | 映射宿主稳定接口、能力发现、错误归一 | 复制一套独立调度与授权系统 |

### 3.1 依赖与进程

核心库不能 import Hermes 或 Pi；通过 Python Protocol 与跨进程协议注入。单守护进程含 scheduler、store writer、coordinator 与 dispatcher。运行模型或本地脚本的 worker 独立进程按需启动。最小版本同一 profile 最多一个 Agent 推理任务，少量并发只读 I/O；取消与超时由 supervisor 管理。

SQLite 仅由本地服务管理；宿主通过 API 修改，不直接打开 DB。首版每个 profile 一套 DB、工作目录、凭据引用与运行身份。不能把加一个 `tenant_id` 字段当成安全的多租户产品。

### 3.2 仓库结构

```text
proactive-agent-sdk/
  pyproject.toml
  src/proactive_sdk/
    api.py                 # 唯一公共 Python facade
    contracts.py           # Protocols、模型、错误类型
    scheduler.py           # occurrence / misfire / TZ
    hooks.py               # 进程、staging、legacy bridge
    coordinator.py
    context.py
    executor.py            # 独立有界 ToolLoopExecutor
    policy.py
    skills.py
    store.py
    delivery.py
    service.py             # daemon 与控制面
    adapters/              # 可选安装；不倒置依赖
      hermes.py
      mcp_tools.py
    migrations/
  packages/client-ts/      # 从 schema 生成类型，不另建运行内核
  packages/adapter-pi/
  schemas/
  tests/                   # unit / conformance / integration / fault
  examples/
  docs/
```

这些是施工目标路径；本交接包中的 `examples/` 是先行参考代码，不能当作上述文件已经存在。

## 4. 统一契约与生命周期

### 4.1 公共对象

所有跨进程消息必须包含 `protocol_version`、`request_id` 或 `event_id`。所有变更类请求使用 `idempotency_key`；同 key 不同规范化内容返回冲突。外部 UTC 时间带 `Z` 或明确 offset；内部毫秒字段统一使用 `_ms` 后缀。参考切片为便于测试使用整数秒，生产迁移时不能混用。

| 对象 | 最少字段 |
|---|---|
| JobSpec | job_id、revision、owner、mode、schedule、task、grant_refs、delivery_policy、misfire_policy、deadline、enabled |
| WakeEvent | event_id、origin、occurrence_id、observed_at、expires_at、source_refs、cause、dedupe_key |
| RunRequest | run_id、attempt、fence、context_ref、skill_refs、tool_allowlist、budget、deadline、policy_version |
| RunHandle | executor_id、host_run_id、state、resume_token、capabilities |
| ContextPack | task、preferences_ref、source_snapshots、pending_items、sent_facts、memory_refs、locale、timezone |
| Decision | decision、summary、proposals |
| ActionProposal | kind、fact_id、revision、evidence_refs、arguments/body、expires_at；不允许模型指定任意外部接收者 |
| ActionRecord | action_id、canonical_arguments_hash、grant_id、approval_id、policy_version、state |
| DeliveryAttempt | message_id、attempt_id、fence、provider_key、state、receipt、last_error_class |
| Usage | model/provider、输入输出 token、缓存 token、工具次数、elapsed、计价依据；不可测项目标为 unknown |

### 4.2 核心端口

以下是**计划实现的接口设计**，不是已经可 `pip install` 的 API。

```python
from typing import AsyncIterator, Protocol

class AgentExecutor(Protocol):
    async def capabilities(self) -> "ExecutorCapabilities": ...
    async def start(self, request: "RunRequest") -> "RunHandle": ...
    async def events(self, handle: "RunHandle",
                     after_seq: int = 0) -> AsyncIterator["RunEvent"]: ...
    async def status(self, handle: "RunHandle") -> "RunStatus": ...
    async def cancel(self, handle: "RunHandle") -> "CancelAcknowledgement": ...
    async def close(self) -> None: ...

class Source(Protocol):
    async def fetch_delta(self, request: "SourceRequest") -> "SourceBatch": ...

class ToolBroker(Protocol):
    async def call(self, request: "AuthorizedToolCall") -> "ToolResult": ...

class DeliverySink(Protocol):
    async def send(self, request: "DeliveryRequest") -> "DeliveryReceipt": ...
    async def reconcile(self, request: "ReconcileRequest") -> "ReconcileResult": ...
```

`SourceRequest` 必须绑定具体账户、最小读取范围和 deadline。`AuthorizedToolCall` 由 broker 根据当前 grants 签发，不能直接反序列化模型输出得到。`DeliveryReceipt` 区分 `durably_stored / provider_accepted / user_read`，不能把一个成功 HTTP 请求写成“用户已阅读”。

`ExecutorCapabilities` 至少声明 streaming、cancellation、resumption、external_tool_broker、read_only_enforcement、usage_reporting。**声明不是证明**：合约测试与部署侧隔离检查仍必需。不支持硬取消、工具约束或对账时，不能承接要求这些能力的任务。

### 4.3 运行状态

```text
queued → admitted → running → proposed → policy_evaluated
                                   ├→ suppressed
                                   ├→ waiting_for_approval
                                   └→ actions_queued → completed
任一适用状态 → failed / cancelled / expired / interrupted / recovery_required
```

`completed` 表示本 run 的决策与动作安排完成；下游 delivery 仍可能 pending。面向用户显示三个维度：**分析状态、动作状态、通知状态**，不要压成一个含糊的“完成”。

流式输出属于观察事件，不能根据半截文本创建动作。仅当 executor 最终状态成功、严格 schema 验证通过且 policy 复验通过，才提交提案。取消是请求；只有执行器或 broker 确认实际停止后，才能释放相关副作用锁。

### 4.4 统一错误

`invalid_config`、`unsupported_capability`、`dependency_missing`、`permission_denied`、`approval_required`、`auth_required`、`rate_limited`、`budget_exceeded`、`deadline_exceeded`、`stale_context`、`provider_unavailable`、`conflict`、`effect_unknown`、`internal_error`。

每个错误带 safe message、retryability、retry_after、scope 和 correlation id，不带 token、邮件正文或未脱敏的 HTTP body。授权失败、明确不可重试配额失败、参数冲突不得自动转其他账户或其他凭据重试。

## 5. 调度、心跳与低成本唤醒

### 5.1 计划类型

| kind | 必须字段 | 语义 |
|---|---|---|
| interval | anchor、every_seconds | 按锚点推进，适合心跳与轮询 |
| daily | local_time、timezone | 每个本地日期一次 |
| weekly | weekdays、local_time、timezone | ISO 星期编号 1–7 |
| monthly | day_of_month、local_time、timezone | 不存在的日期默认跳过，不自动改月底 |
| runonce | at | 一次性时间，完成/过期后不再创建新 occurrence |

`mode` 与 kind 分离：`heartbeat` 是机会检查；`task` 是明确任务。周期任务不一定是心跳；once watch 也不等于只允许一次投递尝试。

时区使用 IANA 名称。夏令时跳过时刻默认 skip；重复本地时刻默认 earliest，只生成一次。用户可显式选择不同策略，但必须持久化在任务版本中。runonce 的歧义本地时刻必须要求 offset 或 fold 选择，不能静默猜测。

生成 daily / weekly / monthly occurrence 时，用候选本地时间的 fold=0/1 转 UTC 再 round-trip 回本地验证；删除不往返一致的候选，按 UTC 去重并应用 fold 策略。不要仅 `replace(tzinfo=...)` 就认定不存在的本地时间是有效的。

### 5.2 漏跑与恢复

heartbeat 默认 `coalesce_latest`：机器停机两天后最多补一次检查，不补发 96 次。显式 scheduled task 默认仅在 grace window 内补跑一次；超出 deadline 标 expired，并保留可查询的未执行原因。runonce 默认遵守自己的 expires_at，而不是无期限迟到执行。

interval 的逻辑时间为：

```text
slot = anchor + floor((now - anchor) / every) * every
next = slot + every
```

`occurrence_id` 根据 job_id、revision、逻辑 slot 计算。引入 jitter 时只改变真实接纳时刻，不改变 occurrence id。重试沿用原 occurrence/run，不能创造一个“新任务”绕过去重。

DB 事务内同时：验证任务 revision 与 enabled → 插入唯一 occurrence/event → 推进 next_due。事务后才启动模型。调度扫描使用到期索引；不要每十秒扫描全部历史任务。

调度等待使用 monotonic clock；计划与审计使用 UTC wall clock。时钟倒拨不重放已接纳 slot；系统恢复后重新计算。任务编辑采用 optimistic revision，设定生效边界；旧版本已接纳任务需要明确保留或取消，不能靠两个不同 revision 的 key 自动解决重复业务通知。

### 5.3 心跳的两级运行

**L0 无模型阶段**检查：任务是否启用、清单是否为空、来源是否变化、缓存是否过期、用户是否已撤销授权、是否已有同目标任务在跑、预算是否耗尽。空清单、无变化且无到期义务时直接结束，记录机器原因但不通知。

**L1 推理阶段**仅处理新信号、已到执行时间的显式任务、经授权的周期性回顾。并非每个 L0 tick 都创建一轮完整对话。某个任务要求“即使没有变化也发日程摘要”，应由 `task` 的明确语义进入 L1，而不是破坏所有 heartbeat 的默认静默。

优先级建议为：用户交互 > 明确时限任务 > 事件 watch > 一般 heartbeat > 可选维护。对同一 profile 的同类 heartbeat 做 coalescing；不能以“模型还在推理”为由无限累积队列。

### 5.4 清单文件与权限

`HEARTBEAT.md` 中允许自然语言检查项。文件更新产生新内容 hash；受限解析器或一次有预算的计划编译将其映射为结构化目标。尚未绑定授权的项目为 `needs_configuration`，不能仅凭一行“检查所有邮件”访问全部账户。

Agent 可以提出新清单或任务建议，但无权自行修改硬授权、静默时段和权限上限。经用户明确批准的任务变更通过 API 提交。默认 30 分钟只是起始配置；各 Source 再设最小轮询间隔、配额、合并与退避规则。

## 6. Hook 运行与 Muse 兼容协议

### 6.1 协议兼容

附件中最值得保留兼容的是：

```text
stdout:
HATCH_HOOK_RESULT:{"decision":"silent|wake","reason":"...","payload":...,
                   "disable_after_run":true}

stderr:
HATCH_HOOK_LOG:{"message":"...","...":"structured fields"}
```

仅接受退出码 0、恰好一个终结结果、合法 UTF-8 与 JSON。结果必须是 stdout 最后一个非空行；允许前面的普通诊断文本，但不把它们交给模型执行。`payload` 在 legacy 模式保留任意 JSON 值；新事件封装另外提供 schema_version。

生产上 stdout 64 KiB、stderr 64 KiB、单次 hook 默认超时 5 秒、payload 最大 16 KiB 是**可配置的建议起值**，不是附件的事实。必须在读取管道时限流，不能全部缓冲后再判断超限。超时终止进程组，必要时用容器/cgroup 管理后代进程。

失败、无结果、多个结果、非法字段都进入 `hook_error`，不直接转换为 wake。错误次数累计与管理员诊断有单独冷却，防止“监控坏了所以每十秒唤醒模型”的费用风暴。

### 6.2 不要原样采用状态提交时序

原 helper 会在 shell 中更新 JSON 文件；这与 event 入队并不是一个事务。可靠桥接必须：

1. coordinator 领取 hook 执行租约，读取 canonical `hook_state` 与 version。
2. 在隔离的 invocation 临时目录创建旧状态副本，设置 `HATCH_HOOK_STATE_DIR` 指向 staging，而不是生产状态目录。
3. 使用已验证 ID 和只读 helper 运行 child process；收集结果和 staging state。
4. 事务内校验 invocation 去重和 version；更新 state；接纳 wake event；应用 disable_after_run。
5. 提交后才运行 Agent；删除 staging。失败则丢弃 staging，不推进检测水位。

若进程在第 3 与第 4 步之间崩溃，下次重试仍看到旧 canonical state；若在第 4 步之后崩溃，事件已持久化。**停止探针并不删除它已创建的待处理通知。**

`invocation_id` 的重试必须沿用原逻辑 ID；同 ID 不同内容返回冲突。每个 hook 禁止并行写状态。第三方副作用不得藏在 hook 中，否则上述状态事务无法保证外部行为可回滚。

### 6.3 helper 可用，但不等于安全运行环境

原 `hatch_hook_runtime.sh` 可作为**待授权的兼容帮助库**，技术测试见验证报告。它的 `silent` / `wake` 会 `exit 0`；只能 source 到独立 child shell。脚本中的路径来源需要外层校验；原子 rename 不能替代 fsync、并发控制和数据库事务。

`HATCH_HOOK_DRY_RUN=1` 仅影响该 helper 的状态写入。它不能阻止脚本执行 curl、删除文件或写其他路径。真正 dry-run 必须使用临时只读输入、受控网络、没有连接器 secret 的沙盒。安全边界不写成一个环境变量。

默认 hooks 仅消费服务预取的授权快照和普通系统健康信号。必须访问公开网站的探针，通过网络 broker 配置域名范围、DNS/redirect 检查与超时。不得访问云 metadata endpoint、宿主内部管理网或通过 URL 获取更多秘密。

## 7. 上下文、来源、记忆与文件视图

### 7.1 ContextPack

一个 run 的 ContextPack 是不可变快照，包含：

```json
{
  "schema_version": "1.0",
  "task": {"goal_id": "daily-agenda", "scope": "selected-calendar"},
  "locale": "zh-CN",
  "timezone": "Europe/Berlin",
  "preferences_ref": "preferences:7",
  "sources": [{
    "source_id": "calendar",
    "account_ref": "account:primary",
    "snapshot_ref": "snapshot:example",
    "observed_at": "2026-10-06T06:55:00Z",
    "fresh_until": "2026-10-06T07:10:00Z",
    "sensitivity": "private"
  }],
  "pending_refs": [],
  "sent_fact_refs": [],
  "memory_refs": [],
  "untrusted_content_policy": "data_only"
}
```

例中的时间、账户与 ID 都是假数据，不是用户的实际行程。

Sources 的增量结果至少包含 cursor、版本、deleted/tombstone、观察时间、必要证据和是否还有分页。分页游标与 provider grant 绑定，不跨账户复用。

**区分三个进度：已读取来源的 cursor、已接纳事件的 watermark、已确认投递的 ledger。** 不能因为邮件已经读取，就假定用户已经收到摘要；也不能为了投递失败而重复抓取整个邮箱。

### 7.2 文件视图

可导出与 Muse 接近的 `context.md / sources.md / sent.md / pending-updates.md / memory.md`，便利宿主渐进读取和调试。但它们是受权限控制的派生视图，不是唯一数据库。带来源版本和 snapshot id；不用模型随手编辑 `sent.md` 作为去重真相。

首版只生成实际有数据的视图。没有邮箱授权，就返回 source unavailable，而非创建空 `emails.md` 并声称“没有新邮件”。缓存断网时可给出带“截至何时”的结果；要求实时确认的任务必须阻塞。

### 7.3 记忆与偏好

核心提供 `MemoryPort`，默认实现为结构化偏好与带证据的简短记忆条目，不强制 embedding 或图数据库。长期推断的用户偏好记录 `source / confidence / last_confirmed / expires_at`；推断不得升级为工具授权。

读取来源文档、网页和 Skill 中的命令，都属于不可信或低优先级材料。审批、账户选择与权限由 control plane 决定；“忽略规则”一类来源文字不得进入 policy 配置。

删除用户数据时覆盖原始快照、派生摘要、索引、待发内容、持久日志与备份保留策略。同步来源的删除不必删除依法或按用户选择保留的审计事件，但须最小化内容、可解释并可配置。默认日志不记录思维链；仅记录简短决定原因和证据引用。

## 8. 独立 AgentExecutor 与主动决策

### 8.1 不复刻缺失的黑盒

附件没有决定层源码。PAS 自己实现两段式决策：

**确定性筛选**决定有没有资格运行和哪些能力可用；**有预算的 Agent 推理**判断当前信息是否值得提出动作。最终所有提案再经过确定性策略。只有优先级排序等可调的软判断可以使用模型，不把权限、账本或去重交给模型自由处理。

默认 Decision：

```json
{
  "decision": "propose",
  "summary": "授权来源中出现了尚未通知的新修订。",
  "proposals": [{
    "kind": "notify_self",
    "fact_id": "source:item-123",
    "revision": "rev-2",
    "body": "发现一条与你的跟踪目标相关的新内容。",
    "evidence_refs": ["snapshot:item-123:rev-2"],
    "expires_at": "2026-10-07T00:00:00Z"
  }]
}
```

`silent` 要求 proposals 为空。拒绝额外未知字段、过大内容和不支持的动作类型。kind 支持 `notify_self / draft / internal_record / suggest_watch / request_external_action`；后两者默认不会直接创建新授权或执行外部写入。

摘要是可显示的简短原因，不要求披露模型内部思维链。模型解析失败最多允许一次有成本预算的修复，仍失败则结束，不从混杂文本里“猜一个动作”。

### 8.2 内置 ToolLoopExecutor

用于 Builder 不依赖 Hermes/Pi 的场景。`ModelPort` 接受消息与已授权工具 schema，返回内容、结构化工具调用和 usage；每一轮按以下顺序执行：

```text
校验 deadline / cancel / token reservation
  → ModelPort.generate
  → 验证 tool call schema 与 call_id
  → ToolBroker 校验实际身份、scope、fence、网络与预算
  → 执行只读工具，限制大小、回传 evidence ref
  → 附加工具结果
  → 直到 FinalDecision 或达到上限
```

建议起值：单 run 最多 8 次模型回合、12 次工具调用、120 秒 wall time、8 个最终提案；默认值必须可配置并随测试调优，不能宣称本次已做成本优化实验。

模型适配器至少有一个真实 provider 实现和一个 FakeModel 测试实现。Builder 可直接注入任何符合 ModelPort 的实现；供应商专有参数放 adapter namespace，不污染公共协议。工具 schema、错误、流式事件、structured output 的兼容性必须做实际测试。

暂停审批时释放推理计算资源，但保留 run 与 exact proposal。恢复后重新检查权限、时效和参数 hash。工具输出可能含恶意指令；工具元数据里的“read-only”标注必须与 adapter 的实际行为匹配。

### 8.3 费用与资源

每个 run、source、profile 有独立预算。调用前预留 token/工具额度，结束后按真实 usage 结算。平台不提供 usage 时标 unknown，使用保守上限而不是写成零费用。到期通知不应因为低优先级维护任务耗尽预算而永久饥饿，可预留显式任务预算。

成本可估为：

```text
每日总成本 ≈ Σ(实际 L1 次数 × 每次模型与工具成本) + 来源轮询成本
```

不能用“每天 48 次心跳”直接推导必有 48 次 LLM 调用。缓存命中、无变化筛选和聚合摘要的收益要通过 trace 与评测验证。

## 9. 权限、审批、工具执行与安全边界

### 9.1 权限来源

`Grant` 绑定 profile、具体账户、capability、资源过滤条件、动作范围、期限和撤销版本。接收者本人通道在可信配置中绑定。模型只提供消息正文与事实身份，不自由选择手机号、邮箱、webhook URL 或聊天群。

硬策略优先于 preferences、Skill 和模型建议。`allowed-tools`、`includeInPrompt`、Hermes plugin capability 标识都不是外部写入授权。[W1,W4]

典型分级：

| 动作 | 默认处理 |
|---|---|
| 授权范围内读已选来源 | 自动，但受速率、预算和敏感字段规则约束 |
| 给已绑定本人通道发通知 | 可在用户授予长期通知权限后自动；仍遵守时段与话题限制 |
| 创建本地草稿、记录证据 | 自动或按敏感度设置 |
| 向他人发送、改日历、发布内容 | 明确授权的窄自动化规则，或单次人工审批 |
| 支付、删除、账号设置与新凭据授权 | 默认拒绝自动执行；专门审批与专用 adapter |
| 自动生成新监控 / 扩大目标 | 只提出建议；不能增加作用域、频率或费用上限 |

### 9.2 审批的冻结

审批内容包括工具/动作、账户、接收者、规范化参数、附件 hash、证据、有效期和 canonical request hash。批准绑定该 hash，不是“今后随便执行同类事情”。提交前参数或附件变化则重新审批。撤销授权立即使未执行 approval 失效。

审批 UI 不信任模型生成的按钮与链接。仅 control plane 能接受已认证用户的批准。模型、hook、Skill 不获得 approval endpoint 的 bearer 权限。

### 9.3 宿主的工具约束

**“请只做分析不要发送”不是安全边界。** Hermes 或 Pi 若仍能直接调用带凭据的 shell、网络工具或发邮件功能，就可能在输出提案前已经产生副作用。

生产支持以下两种可证明配置：

1. 受控 session / profile 只装必要的只读工具，并由运行环境隔离网络与凭据；
2. 所有工具动作都经 PAS ToolBroker，执行前检查当前 grant、审批和 fence。

无法达到上述之一时，该宿主只能用于明确授权的低风险、本地非敏感实验；`doctor` 标记 `unsafe_executor`，不宣传为完整安全主动运行模式。允许 shell 的 Skill 需要真实容器/VM边界与网络代理，不靠字符串匹配禁止几个命令。

### 9.4 数据与供应链

不自动运行导入 Skill 的 install 脚本；不从 Markdown 下载并执行未知二进制。固定版本/commit/hash，检查 zip slip、symlink escape、压缩炸弹、递归依赖和路径越界。默认禁用未知插件与自动项目 extensions。

密钥存 OS keychain / 受控 secret service；DB 只存 secret_ref。工具 worker 仅获得当次能力，不继承整个宿主环境。网络 broker 处理 allowlist、DNS rebinding、redirect、SSRF、TLS 与响应大小。审计日志对邮件正文、账户、URL query 等做默认脱敏。

## 10. 动作、通知、去重与恢复

### 10.1 两层去重

触发层 key：`profile + job + revision + occurrence / event source revision`，阻止同一事件创建重复 run。

业务通知 key：`profile + goal + fact_id + revision + destination + action_kind`，阻止不同心跳、重试和多个渠道入口对同一事实重复发出相同意图。是否跨渠道去重由显式策略决定。不要只 hash 文案：模型换一种说法并不是新事实。

同事实有实质更新才增加 revision；来源没有 revision 时用规范化关键字段 hash。突发相似事项可聚合；已静音话题、已发事实、过期信息都应抑制。提高优先级不能绕过用户的明确禁区。

### 10.2 事务 outbox

一个数据库事务中保存最终决策、动作记录、批准引用与待投递项，再提交 run 的阶段状态。实际网络发送在事务外进行，不能持有 SQLite 写锁等待 LLM 或网络。

```text
pending → sending → provider_accepted / stored_in_inbox
                   ↘ failed_retryable / failed_terminal / delivery_unknown
pending → deferred / suppressed / expired
delivery_unknown → reconciled_delivered 或经权威确认后可重试
```

本地 inbox 与同 DB 的 outbox 可用同一事务得到可靠一次入箱效果。外部服务无 idempotency key 或查询回执能力时，不能保证端到端 exactly-once。系统提供持久化尝试、业务去重和未知状态对账；在无法判定时宁可暂停，而不是谎称既保证不漏又保证不重复。

发送请求可能已被 provider 接受但 ACK 丢失。此时保留 `delivery_unknown`；不能仅因 lease 到期就重发。重试需 provider 同一幂等 key，或确认旧 worker 已终止且旧请求未被接受。普通搜索“没找到消息”不足以作为权威未发送证明。

### 10.3 租约与 fencing

worker 的 token 每次 claim 单调递增。完成、提交动作与工具执行都检查有效 token。lease 只表明数据库中的所有权，不意味着旧进程已经停止。外部工具不能被 fencing 阻止时，需要串行副作用锁和对账，否则 lease 机制仍可能双发。

只读推理 run 可在超时后重试；未知外部写入不能当作普通失败重试。cancel 后仍可能收到晚到的真实回执，应对账保留，而非因 run 已 cancelled 就丢弃事实。

### 10.4 投递前复验

发送前重新检查当前 grant、topic mute、quiet hours、目标账户/本人映射、内容敏感性、新鲜度、用户是否已处理事项、已发 ledger，以及额度。锁屏 push 默认只放一般摘要，不暴露私人邮件正文。

夜间产生的机会可以到次日聚合；超过有效期则 suppressed/expired，不机械发过时消息。日历“还有十分钟开会”这类消息到次日不能补发。用户本人的主动提醒与向外部对象发送信息使用不同授权模型。

## 11. Muse Skills 的可用兼容层

### 11.1 审计结果影响设计

附件共发现 **88 个 `SKILL.md`**：82 个在 `skills/<name>/`，另 6 个在 `skills/artifacts/` 子层。43 个名称含下划线，不满足严格格式；41 个 name 与直接父目录不一致；`meta-ads` 描述为 1247 字符；全部 88 个都含 boolean 类型的 `metadata.includeInPrompt`，不符合字符串映射要求。详细记录在 `audit/skills.json`。这说明应实现 legacy importer，而不是直接承诺严格标准兼容。[A7,W1]

### 11.2 导入流程

```text
本地显式选择 Muse 目录
 → 安全遍历与 hash
 → legacy frontmatter 解析
 → 生成无碰撞的 canonical id / aliases
 → 校验依赖、引用和资产
 → 生成 compatibility sidecar
 → 给出导入报告
 → 按明确授权决定是否安装 / 启用
```

原目录保持只读。生成的 overlay 使用 `google-calendar`、`muse-db`、`artifact-document` 等合法名称，记录原始 name/路径/hash。嵌套 artifacts 的共享 references/scripts 必须进入依赖闭包，不能只复制单个 `SKILL.md`。

`metadata` 的布尔值可规范化为字符串，但其原始语义放兼容 sidecar；`title/icon/category` 等保留在命名空间扩展，不强塞进标准必选字段。过长 description 用经审阅的短摘要进入检索索引；完整原文继续留在原文件，不无提示截断并覆盖它。

`includeInPrompt=true` 不解释为每轮全文注入。只加载当前授权且可用能力的短索引；被选中后读正文；资源按需读取，避免一次塞入 88 份文档。高风险 Skill 不因一个 metadata 字段就进入模型上下文。

### 11.3 Sidecar 结构

下面是 PAS 的扩展元数据，不冒充 Agent Skills 标准字段：

```yaml
schema_version: "1.0"
source:
  format: muse-legacy
  original_name: google_calendar
  original_path: skills/google-calendar/SKILL.md
  sha256: "<actual-file-hash>"
canonical_name: google-calendar
aliases: [google_calendar]
requirements:
  capabilities: [calendar.read]
  tools: [hatch_gws_cli]
  grants: [selected_calendar_account]
compatibility:
  status: adapter_required
  path_strategy: isolated_virtual_mount
distribution:
  status: permission_unverified
```

技术状态和再分发状态必须独立。建议状态为：
`discovered → parsed → dependencies_resolved → contract_tested → e2e_verified`；任何阶段可显示 blocked 原因。`installed` 不替代 `e2e_verified`。

### 11.4 能力适配不是字符串替换

| 原依赖 | PAS 应做 | 首版缺失时 |
|---|---|---|
| `hatch_gws_cli gmail ...` / calendar | 有限命令 grammar → 类型化 Google 能力 → 真实连接器；保留账户和错误语义 | 明确 adapter_required |
| Muse 自身 DB / tools | 按允许的只读查询映射到 PAS 视图；不能透传任意 SQL | blocked，不能生成假数据 |
| notification / chat / push | 本人通道映射、outbox、回执 | inbox fallback 仅在用户允许时 |
| authd / 动态凭据 | SecretPort 与宿主授权提供者 | 不伪造 `hsurr:*` 或授权链接 |
| 浏览器、设备、HealthKit 等 | 对应宿主 provider、权限和同步机制 | 声明 unsupported |
| 宽域研究 / 多 Agent delegation | 明确受限 delegation adapter、预算与合并 | 首版不支持或只用等价只读能力 |
| artifacts 与媒体脚本 | 安装所需工具、资产与路径映射；独立许可核验 | 按依赖阻塞，不影响心跳核心 |

`hatch_gws_cli` 兼容命令必须用 argv 列表解析，不接受任意 shell 拼接。只实现并测试确定需要的子命令；未识别命令返回 unsupported，不“尽力猜测”后调用更高权限 API。

路径优先通过受控文件解析器或**隔离容器内部**的只读虚拟挂载适配，不在用户机器真实创建 `/opt/hatch` 或覆写 home。宿主不能挂载时生成审核过的 overlay，逐项映射绝对路径；不能全局正则替换任何出现 `muse` 的字符串。

附件 gmail Skill 含固定英文输出要求等宿主风格假设。PAS 的用户 locale 与已确认偏好优先，兼容层显式覆盖这些宿主约束；否则中文用户会得到错误语言的主动摘要。[A7]

### 11.5 首版兼容范围

首版做到 **88 个入口可发现、可审计、可说明缺口**；从中优先打通日历读取、邮件读取/摘要、公开资料跟踪和本人通知的真实闭环。每个通过的 Skill 必须列出具体支持任务与子命令，而不是只列名称。

可以提供 `pas skills import --format muse --source /local/path --audit-only` 以及 `pas skills explain google-calendar`。不内置 Muse 全量目录，不从不明来源自动下载。采用用户自行提供目录的 BYO 模式只解决分发形式，**不构成对使用、访问来源或第三方权利的法律豁免**。

## 12. Hermes、Pi 与外部框架

### 12.1 Hermes 适配

核对日的官方接口提供 `/v1/capabilities` 和 Runs 生命周期；Hermes 也已有会话 heartbeat。因此本项目增加的是跨宿主的可靠内核、政策与兼容层，而不是声称给 Hermes 发明了定时功能。[W2,W3]

推荐默认连接：

```text
PAS coordinator
 → GET /v1/capabilities
 → POST /v1/runs + persisted Idempotency-Key
 → GET /v1/runs/{id} 或 SSE events
 → 提取真正完成的 output
 → PAS schema / policy / outbox
```

采用专用低权限 profile 与分离的 proactive session；不要向用户正在操作的 live transcript 注入半成品推理。宿主可以在 PAS 最终通知后，由 session owner 安全追加一条摘要。

`examples/hermes_runs_adapter.py` 演示真实已文档化的 request shape，但尚未对实际服务运行。Idempotency-Key 的服务保留期有限；SDK 自己必须保存请求 hash 与 host_run_id。遇到响应丢失用相同 key 对账，超过服务保留期后不得无脑重建未知写入任务。等待审批和 stopping 都不是成功完成。SSE 只用于观察，断开后用状态查询恢复。[W2]

Hermes plugin 通过官方注册入口向用户暴露 `proactive.schedule / status / pause / skills.inspect` 等工具。所有操作落到 PAS；无需 import 私有 cron 内部模块或直接改宿主任务文件。若采用宿主调度，必须显式切换 ownership 并停止 PAS 的同任务定时器。`hermes --mode rpc` 不是本方案接口，不能从 Pi 复制这个命令。[W4,W5]

### 12.2 Pi 适配

核对日 Pi 官方文档采用 `@earendil-works/pi-coding-agent` 的 `createAgentSession`。新项目应从当次验证的包与文档生成锁文件，不盲用旧资料里的包作用域。[W6]

优先 Node worker 中通过 SDK 创建独立 session。Python 通过 PAS 自有 JSON-RPC 桥与 worker 通信；worker 只负责 executor，不持有自己的任务调度真源。受控 factory 明确 cwd、agentDir、SessionManager、资源加载器与 tools，禁止默认发现未知项目 extension。`examples/pi_executor.ts` 接受这个 vetted factory，未代替它实现隔离。

`await session.prompt()`、最终内容和 idle 状态决定该调用是否结束；不能拿 prompt ACK 当结果。CLI 路径可用 Pi 官方 RPC，但它是 Pi 自己的 JSONL 消息协议，须用官方客户端/适配器映射到 PAS；不能直接当 JSON-RPC 2.0 server。对于可重试/恢复事件，须按其真实生命周期终结事件处理。[W6,W10]

Pi bridge 的能力检查包含实际工具名单，但工具名并不能证明只读。资源加载器、扩展加载、工具执行和网络边界必须由 composition root 与 supervisor 控制。取消后等待 idle；超时未停则上报 `cancellation_unconfirmed` 并由进程监督器处理，不开启另一个可能重复写入的 worker。

### 12.3 其他框架、MCP 与接入原则

其他 Agent 只需实现 AgentExecutor 或提供被 PAS 调用的受控 worker。MCP bridge 可以暴露 schedule/status 等工具和 Skill 检索资源，也可消费外部工具服务；但“安装一个 MCP server”不会让退出的宿主持续运行。持久化守护进程和生命周期责任必须写入部署说明。[W7,W8]

适配器入库要求：能力发现、版本锁定、会话隔离、真实完成状态、取消行为、schema 验证、写权限防护、断线恢复、usage 口径和端到端 contract tests。工具名称或提示词能对上，只能叫基本协议联通。

## 13. 持久化、事务与数据模型

### 13.1 生产表与索引

附带 `examples/schema.sql` 是**生产表结构的起点**，不是完整业务实现。每个 profile 独立 DB；逻辑字段至少包括：

| 表 | 主要内容 / 唯一约束 |
|---|---|
| meta / schema_migrations | profile 身份、schema 版本、migration checksum |
| jobs | job_id、revision、schedule/task JSON、enabled、next_due_ms |
| hooks | hook_id、definition hash、state version、state JSON、enabled |
| events | event_id、唯一 idempotency key、payload hash/ref、expires_at |
| hook_invocations | invocation_id、请求 hash、状态版本、对应 event |
| runs | run_id、event_id、state、attempt、fence、lease_until、deadline、host handle |
| run_events | run_id + seq、事件类型、摘要或引用、时间 |
| source_state / snapshots | 账户与来源 cursor、版本、证据与新鲜度 |
| grants / approvals | 最小能力、范围、撤销版本、批准 hash、有效期 |
| actions | action_id、规范化参数 hash、授权、审批、状态 |
| outbox / delivery_attempts | 业务 key、接收者引用、有效期、尝试 fence、回执与未知状态 |
| skill_installs / feedback | 导入来源、兼容状态、用户反馈与 suppression 依据 |

所有关键关系使用外键；唯一索引保证 job occurrence 和业务 action 去重。`next_due_ms`、`runs(state,lease_until_ms)`、`outbox(state,not_before_ms)` 建索引。大正文存本地受控 blob store，DB 存 hash/ref；无需将每一轮上下文复制到多个大 JSON 列。

### 13.2 事务边界

只允许短事务，事务中不 await 网络：

- 调度：检查 revision + 创建 event/run + 推进 next_due。
- Hook：CAS state + invocation 幂等 + event/run + disable。
- 决策完成：校验 run fence + 保存审计 + 建 actions/outbox + 更新 run state。
- claim：选候选 + increment fence + 设 lease；单 writer 保证一致。
- 回执：校验 attempt 身份 + 写 provider receipt + 更新 delivery ledger。

业务事务结束才 ACK 控制面的写请求。事务提交失败不返回“已创建”。对于重复 key，同 payload 返回既有对象，不同 payload 返回 conflict。删除一个源对象不能级联抹掉仍需对账的外部动作。

### 13.3 SQLite 模式与版本

首版不需要引入消息队列集群。SQLite 使用 `foreign_keys=ON`、busy timeout 和 `synchronous=FULL`；DB 目录与 journal 文件都必须在本地受控文件系统。不要放在 NFS、跨机器共享盘或自动同步文件夹。

若开启 WAL，应在 `doctor` 检查实际链接的 SQLite，而不是只检查 Python 包版本。官方已记载 WAL-reset 竞态修复；选择已含修复的版本/可信回移补丁，并把证明放进依赖锁与测试。当前参考环境 SQLite 为 3.46.1，所以示例明确使用 DELETE journal，不借用未修复 WAL 路径。[W12]

性能升级先采用单 writer actor 与适当批处理，不先做多个随机写连接。备份用数据库 backup API 或经过验证的停写快照；WAL 模式不能只复制主 `.db` 文件。恢复时校验 schema 与 integrity；迁移前备份、失败回滚，拒绝旧二进制写入新 schema。

## 14. 公共 SDK、控制面与配置

### 14.1 设计目标 API

以下 API 是接手者要实现并通过 conformance 的目标。示例用于消除施工歧义；不能在成品未发布时把它作为已存在包的安装教程。

```python
from proactive_sdk import ProactiveAgent, Interval, Daily, Job, Scope

agent = ProactiveAgent(
    state_dir="./private-state",
    executor=my_executor,        # standalone / Hermes / Pi adapter
    sources=[calendar_source, research_source],
    sinks=[personal_inbox, personal_notification_channel],
    timezone="Europe/Berlin",
    locale="zh-CN",
)

# grant 由可信用户界面或应用配置完成；模型不能自行调用授权入口。
grant = await agent.grants.create_from_user_consent(
    capability="calendar.read",
    account_ref="calendar:primary",
    scope=Scope(resource_ids=["work-calendar"]),
)

await agent.jobs.upsert(
    Job(
        id="daily-agenda",
        mode="task",
        schedule=Daily(local_time="09:00", timezone="Europe/Berlin"),
        instruction="总结今日已授权日历中的安排，并只通知本人。",
        grant_refs=[grant.id],
        notification_profile="owner-default",
    ),
    idempotency_key="setup-daily-agenda-v1",
)

await agent.serve()  # 前台阻塞；持续运行由部署者的 service supervisor 负责。
```

facade 还必须实现 `start/stop/close`、async context manager、单次 `tick`、jobs CRUD、pause/resume、manual run、status/events、skills audit/import/explain、feedback、approval、export/delete。停止 API 能区分 graceful drain 与强制停止风险。

嵌入模式由 Builder 拥有事件循环。daemon 模式由 `pas serve` 拥有；两者不能争抢同一 state directory。`pas doctor` 检查已启动实例、DB 锁、目录权限、依赖、宿主 capability 与安全配置。

### 14.2 跨语言控制面

建议本地 Unix socket 为默认；远程才用 TLS HTTP。所有调用按 authenticated principal 绑定 profile，不相信请求体自报的 owner。控制面 bearer token 不传给模型或普通 Skill。

JSON-RPC 2.0 方法集：

```text
system.hello / system.capabilities / system.health
jobs.create / jobs.update / jobs.list / jobs.pause / jobs.resume / jobs.delete
runs.get / runs.list / runs.cancel / runs.events
skills.audit / skills.import / skills.explain
approvals.get / approvals.resolve
notifications.list / notifications.feedback
```

跨语言 schema 从同一份 JSON Schema 生成，包含版本协商、错误码和 max size。流式事件用 seq/cursor；支持断线从已有 seq 重连，至少用 DB 状态补齐最终结果。JSON-RPC 通知没有返回值；要确保任务创建成功就用有 id 的 request，不能用无响应通知代替可靠写入。

MCP 暴露给模型的是受限工具子集，例如建议计划、查看状态和读取批准请求。`grants.create` 与 `approvals.resolve` 仅面向可信用户 UI，不把这两项默认做成模型工具。

### 14.3 配置样例

```yaml
config_version: "1"
profile: personal
timezone: Europe/Berlin
locale: zh-CN

runtime:
  max_concurrent_agent_runs: 1
  shutdown_grace_seconds: 20
  event_retention_days: 30

heartbeat:
  enabled: true
  every_seconds: 1800
  misfire: coalesce_latest
  checklist: ./HEARTBEAT.md

policy:
  notification_window: {start: "09:00", end: "21:30"}
  max_unsolicited_notifications_per_day: 5
  external_writes: approval_required
  lockscreen_content: minimal
  allow_untrusted_shell_skills: false

skills:
  import_mode: explicit_local
  source: ./private-vendor/muse-sdk/skills
  distribution: excluded
```

数量、保留期和时间段是示例默认值，不是对用户个人偏好的假设，也不是 Muse 唯一配置。配置加载拒绝未知关键字段；打印配置时隐藏路径中的个人信息与 secret references。profile 时区从配置/用户明确设置获得，不根据开发机器时区自动推断。

## 15. 分阶段施工计划

### 15.1 阶段与交付门槛

| 阶段 | 施工任务 / 目标文件 | 必须通过的验收 | 依赖 |
|---|---|---|---|
| P0 边界与来源 | 建仓；contracts/schema；许可证清单；把 88 条审计与 hash 做成可复现命令；单 profile 假设 | 无 Muse 原文进入公开源码包；禁 zip slip；标准 schema 可校验；未知许可阻断 release | 无 |
| P1 持久化与时钟 | store、migrations、Clock、五类 schedule、misfire、claim/fencing、jobs API | 时区/DST/跨月/停机/重启/并发 claim 测试；同 occurrence 单入队；旧 fence 不能提交 | P0 |
| P2 Hooks 与事件 | 沙盒 runner、legacy parser、staging、CAS、dry-run、hook 状态机 | 原 helper 兼容测试；失败不唤醒；一次性 watch 不丢通知；无网络/文件越权 | P1 |
| P3 独立 Agent 闭环 | Source/Memory ports、ContextPack、ToolLoopExecutor、至少一真实 ModelPort、bounded tools、Decision schema | 没变化零 LLM；显式任务会运行；恶意来源不改权限；deadline/预算有效；真实模型 fixture 不冒充 | P1,P2 |
| P4 策略与投递 | grants、审批、outbox、本人 inbox、一个真实通知 sink、unknown 对账、feedback | 夜间不发；撤销立刻生效；冻结参数审批；ACK 丢失不盲发；本人目标不可替换 | P3 |
| P5 宿主适配 | Hermes Runs/plugin；Pi worker/extension；跨语言协议与 generated client | 固定版本真实服务测试；提交≠完成；取消≠已停；隔离 session；受控工具；版本不支持时 fail closed | P3,P4 |
| P6 Skills 能力 | legacy importer、aliases、依赖闭包、路径映射、Gmail/Calendar 最小兼容工具 | 88 入口审计一致；至少邮件/日历/资料跟踪/本人通知闭环；其余缺口透明；不得模拟已授权 | P3–P5 |
| P7 产品化与发布 | daemon、systemd/container 示例、CLI帮助、备份恢复、日志、文档、包发布、SBOM、兼容矩阵 | 全量 conformance + 安装 smoke + 重启故障 + 安全 + license gate；真实用户流程人工回归 | P0–P6 |

P0–P4 构成可独立运行的核心 beta；P5–P7 后才可以按本规范发布框架兼容版。某 Skill 未完成不阻止核心 SDK 发布，但 README 必须明确兼容层状态，不能标“88 个能力全部实现”。

### 15.2 可给多个施工 Agent 的明确工单

**A：Core/store/scheduler。** 负责 schema、state transitions、fault injection 与 deterministic clock；不得自行定义第二套 ActionProposal。输出 migration、状态机测试和恢复说明。

**B：Executor/security/context。** 负责 ModelPort、ToolBroker、审批冻结与 ContextPack；依赖 A 的事务接口。不得用 prompt 代替权限检查。输出独立 Agent demo 和恶意来源测试。

**C：Adapters/skills。** 在共同 contracts 冻结后实现 Hermes、Pi、legacy importer；不得修改核心数据语义来迎合某宿主。输出 `compatibility-lock.json` 和真实端到端证据。

**集成负责人：** 合并 schema 变更，复核 release gate。每个 PR 列出改变的契约、兼容性、测试、已知缺口；不能把长期 TODO 默认为已完成。未取得来源许可时，C 只能消费本地私有目录，不提交原文件。

### 15.3 关键实现细节工单

- **SCHED-01**：存储 job revision；UTC anchored interval；本地 calendar occurrence；明确 DST/fold；fake clock 覆盖。
- **STORE-01**：所有 admission 和 completion 的事务；并发 claim 及 stale fence；DB owner 检查；迁移回滚。
- **HOOK-01**：staging state；复制前验证文件不是 symlink；运行后只接纳限定 JSON 对象；原子提交。
- **EXEC-01**：限定工具 schema 的 provider loop；structured output validator；output/reasoning 分离；真实成本统计。
- **POLICY-01**：grant 与 action hash；提交前复验；topic mute；静默时段；锁屏去敏。
- **SEND-01**：outbox dispatcher；attempt journal；幂等 provider；unknown reconciling；不丢晚到回执。
- **HERMES-01 / PI-01**：版本能力发现；request/handle 持久化；不接入 live 用户 session；失联恢复。
- **SKILL-01**：源不可变；规范化 overlay；相对引用闭包；缺失二进制与授权可解释；import audit-only。
- **OPS-01**：shutdown drain；supervised restart；磁盘满报警；backup/restore；敏感日志检查。

## 16. 验收与评测设计

### 16.1 硬正确性测试

| 测试组 | 至少覆盖 |
|---|---|
| Scheduling | interval 漂移、向前/向后校时、runonce 过期、DST gap/fold、每月 31 日、停机一周、编辑与暂停竞态 |
| Hooks | malformed JSON、多终结行、非零退出、超大输出、timeout、僵尸子进程、路径逃逸、dry-run 真无外部写 |
| Transactions | state 已写/event 未入队的故障点；run 已接纳/worker 未启动；提交回复丢失；多进程抢同一任务 |
| Execution | partial 不是 completed；tool 超时；未知写入；模型输出非法动作；cancel 未停；过期 fence |
| Policy | 账户切换攻击、任意接收者、scope 扩大、审批后改附件、撤销授权、隐私/夜间/话题禁止 |
| Delivery | provider 已收 ACK 丢失、重复 callback、晚到回执、无幂等 provider、未知状态不自动重发 |
| Skills | 88 条发现、aliases 冲突、metadata 规范化、嵌套 assets、缺依赖、恶意 references、无许可 bundle |
| Adapters | locked Hermes/Pi 真实版本；版本缺能力；会话重启/恢复；资源发现污染；工具行为而非只检查名字 |
| Operations | clean install、升级/回滚、磁盘满、数据库损坏恢复、日志脱敏、数据删除与备份策略 |

原子性测试须有真实进程 kill / restart 场景，不能只 mock 一个异常。两个独立进程并发访问 SQLite 的测试单列，不能把单进程重复函数调用称为并发压力测试。

### 16.2 主动行为质量

构建可回放的合成时间线，按来源事件、真实用户目标、明确偏好和期望行为标注。至少包含：无变化、多条重复、实质更新、已由用户处理、过时提醒、晚间非紧急、明确禁止话题、权限丢失、恶意网页指令、来源矛盾。

分别报告：应通知召回率、误通知率、每日报警量、有效来源覆盖率、重复率、陈旧通知率、单位有效通知成本、平均与 P95 延迟。统计分母和 unknown 状态不可省略。不能只用 LLM Judge 给一个“像 Muse”分数。

确定性 hard gates：无授权外部写入为 0；未获批接收者为 0；已失效任务继续启动为 0；静默禁止时间段违规为 0；同业务 key 的本地 inbox 重复为 0。外部 exactly-once 不可证明的通道独立报告 unknown/reconcile，而不是隐藏异常。

软质量由任务主人评估“相关、有新信息、时机合适、可执行、不过度打扰”。可以用模型辅助初筛，但不替代 hard gates。

### 16.3 性能与成本目标

下面是**待测验收目标**，不是本次跑出的结果：无变化的一天 LLM 调用数为 0；配置 1,000 个轻量任务时不逐秒全表扫描；单 profile 长时间运行无持续内存增长；多日停机后 coalescing 不造成通知风暴。

在固定硬件、固定依赖、关闭网络模型的基准下记录 idle RSS、CPU、DB size 增长、scheduler lateness；另测真实 provider 延迟。机器休眠期间不保证执行；系统重启后的漏跑语义必须可预测。任何性能数字都附环境与命令。

### 16.4 本交接包的测试地位

`reference_core.py` 只覆盖核心可靠性切片；它不实现真实 provider、sandbox、完整日历调度或生产授权服务。`hermes_runs_adapter.py` 与 `pi_executor.ts` 的模拟测试证明本地代码逻辑，不证明远程产品版本兼容。详见 `VALIDATION.md`；完成 P7 前不得删去这些边界声明。

## 17. 运维、部署、开源发布与维护

### 17.1 最小部署

一个长期在线的 daemon、一个 profile 目录、受控 worker、SQLite 和可选通知 adapter。systemd 或容器 supervisor 负责启动与重启；macOS 可用单独 launchd 配置，但本地电脑睡眠不等于云端持续运行。守护进程默认前台，便于 supervisor 监督，不在 SDK import 时偷偷起后台线程。

启动：检查 schema、依赖与锁 → 恢复 pending/unknown → 重算到期任务 → 开始服务。停止：停止接纳新任务 → 请求 worker cancel/drain → 持久化剩余状态 → 关闭连接。不能在退出时把所有 running 简单改 pending，因为其中可能有外部未知副作用。

### 17.2 可观察性

结构化日志包含 run_id、event_id、action_id、阶段、safe reason、时长、预算与来源引用；默认没有原始 private content。指标至少有 wake、suppressed、model_calls、tool_denied、outbox_pending、delivery_unknown、grant_revoked、scheduler_lateness。

明确 liveness 与 readiness：进程活着但凭据失效、数据库满或宿主不可用，应显示 degraded；用户能看到任务最后成功检查时间，不把“计划仍存在”当“监控正常工作”。

### 17.3 发布门禁

1. 原创代码选择并落实许可证，例如 Apache-2.0 或 MIT；第三方部分分别列来源和许可，不能用项目 LICENSE 覆盖未知授权代码。
2. 构建包、源码包和容器都排除 `private-vendor/`、用户配置、原始 Muse 档案、token、内部端点和个人任务。
3. 生成 SBOM、依赖锁和外部版本兼容记录；CI 对分发包本体扫描，而不只扫描 git tracked files。
4. README 说明这是独立兼容项目，不暗示官方背书，不保证能连接未公开的 Muse 后台。
5. 保留 public conformance、可复现 demo、安装/卸载、备份恢复、SECURITY 与贡献说明。
6. 迁移与 protocol breaking change 走版本规则；外部文档变化后先测再更新 supported matrix。

附件未有总许可证的事实不自动判定所有使用都违法，但也不能据此认定可公开复制。发布者需确认来源与适用许可；BYO 目录与重写实现都不能代替必要的权利审查。[W13]

## 18. 可复用代码与参考运行

### 18.1 来自附件的代码

真正可直接接入的首要候选是 `architecture/hooks/runtime/hatch_hook_runtime.sh`：114 行，Bash + jq，实现协议输出与文件状态 helper。**技术复用需外层补强；公开分发需授权。** 私有参考副本另包提供，没有混入本交接包的公开源码候选。

其 hash、入口函数和风险见 `AUDIT_AND_REUSE.md`。其他 runtime-cell、authd client 与健康检查脚本主要是可研究的模式，不是可直接搬走的通用 SDK。

### 18.2 本次原创参考代码

```text
examples/reference_core.py       原子 admission、状态 CAS、只读 run fencing、outbox
examples/hermes_runs_adapter.py  Hermes HTTP 调用与完成态提取
examples/pi_executor.ts          注入式 Pi session adapter 与结果 envelope 校验
examples/schema.sql              生产数据表起点
tests/                          单元与结构接口测试
reuse/extract_original.py        从指定原附件提取 helper 到私有目录；不会自动 publish
reuse/test_original_helper.py    对已知 hash helper 做受控测试
```

运行参考切片：

```bash
python -m unittest discover -s tests -v
python examples/reference_core.py

tsc --strict --target ES2022 --module commonjs --lib ES2022,DOM \
  --outDir /tmp/pas-ts-build \
  examples/pi_executor.ts tests/pi_contract_test.ts
node /tmp/pas-ts-build/tests/pi_contract_test.js
```

参考 demo 只创建内存中的待通知记录，不调用模型、不发真实消息；输出应显示一个事件、一个已规划 run、一条 pending 通知和零已投递通知。它刻意不伪造成功回执。

参考测试使用标准库；TypeScript 编译需要本机 TypeScript compiler。后续工程可以更换实现，但必须保留状态语义与故障测试。

## 19. 仍须接手者解决的明确问题

**来源授权：** 尚无覆盖附件全体内容的可验证许可，不得直接公开整个附件或本次私有 helper 副本。

**Muse 内部实现：** 决策器、手机 push、设备数据同步、认证/审批服务与部分 CLI 二进制未包含；必须自研或替换。

**外部适配联调：** 本次核对的是官方文档，不是运行中的用户 Hermes/Pi 配置。绑定前探测并锁定版本、真实 capability 与工具权限。

**全量 Skills：** 完整逐项审计已提供，但所有 connector、assets 与工具 ABI 的端到端闭包仍要施工；文法兼容不等于所有能力复刻。

**硬安全与高可用：** 本次参考代码不是 sandbox，不承诺多租户隔离、跨主机 HA 或外部网络 exactly-once。

上述问题不妨碍按阶段搭建可用、可开源的独立核心，但必须成为任务和发布门禁，而不是被“兼容 Muse”一句话掩盖。

## 20. 证据与来源索引

### 附件依据

- **A1** `README.md`：素材来源自述、四件套映射、缺失的二进制/后台。
- **A2** `architecture/cron.d/minutely/heartbeat__interval@30m.md`：心跳计划示例。
- **A3** `architecture/hooks/runtime/hatch_hook_runtime.sh`：114 行协议与状态 helper。
- **A4** `architecture/platform-docs/scheduling-and-watching.md` 与 `chat/scheduling-and-watching.md`：轮询、投影配置、运行验证、hook/cron边界。
- **A5** `architecture/HEARTBEAT.md` 与 `PROACTIVE_PREFERENCES.md`：清单与偏好。
- **A6** `architecture/runtime-cell/launch-daemon.sh` 与 `skill-scopes.conf`：部署环境耦合及可见性门控。
- **A7** 全部 88 个 `skills/**/SKILL.md`；机器可读证据 `audit/skills.json`。
- 精选源文件的 hash/行数见 `audit/selected-source-manifest.json`。为避免暴露个性化归档任务，未分发完整原始路径清单。

### 外部官方资料

核对日期均为 **2026-10-06**。这些链接是查阅入口，不代表其未来内容永远不变；接手者应在真实安装版本上重新验证。

| 编号 | 资料 | 用途 |
|---|---|---|
| W1 | https://agentskills.io/specification | Skill 格式与渐进加载 |
| W2 | https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server | Runs、capabilities、状态与幂等提交 |
| W3 | https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat | Hermes 原生 heartbeat |
| W4 | https://hermes-agent.nousresearch.com/docs/developer-guide/plugins | plugin 注册与扩展边界 |
| W5 | https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration | 官方程序化接入方式 |
| W6 | https://pi.dev/docs/latest/sdk | Pi SDK 与 session 生命周期 |
| W7 | https://modelcontextprotocol.io/specification/2025-11-25/architecture | MCP 架构边界 |
| W8 | https://modelcontextprotocol.io/specification/2025-11-25/server/tools | MCP 工具协议 |
| W9 | https://www.jsonrpc.org/specification | JSON-RPC 2.0 |
| W10 | https://pi.dev/docs/latest/cli-integration | Pi RPC、CLI 与完成态 |
| W11 | https://pi.dev/docs/latest/skills | Pi Skill 加载 |
| W12 | https://sqlite.org/wal.html | WAL 约束、备份与已记录竞态修复 |
| W13 | https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository | 缺少许可不代表可自由复制再分发 |
| W14 | https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices | 工具连接的安全注意事项 |

本规范不以新闻稿、同名第三方 Muse 项目或其他 Muse Code SDK 的许可证替代附件的来源证据。
