# Museion PAS 主动层 v2 改进计划

- 版本：v2 草案（2026\-10\-18）

- 基线：`KimFischer99/Museion-Agent-SDK` @ `b875f88a06a7`（main HEAD）

- 目标读者：接手改造的工程 agent。本文自包含，读完即可开工，不需要问原作者，也不需要访问任何特定部署环境。

- 一句话目标：把 PAS 从「有骨架的主动运行时」推到「有理由、有在场感、有素材、有账可复盘」的 v2，同时不破坏它已经做对的那部分（证据闭包、投递状态机、可信入口边界）。

## 0\. 本文的边界

本文只写两类内容：**仓库内可确认的事实**，以及**需要落地的设计决定**。

不写任何特定主机上的路径、服务名、profile 名、数据库位置、账号或部署拓扑。如果在本文里看到这类内容，那是错误，请直接删掉，并按仓库自己的 `examples/` 建立本地环境。

基线数字（测试失败数、内存占用等）来自一次评估环境的实测（Linux，Python 3\.12）。不同环境会有差异，**开工第一步是自己跑一遍基线，以你跑出来的结果为准**。

---

## 1\. 基线：已经成立的性质（v2 不得回退）

以下是在真实运行中验过的行为，改造时当作回归底线：

- 真时钟端到端通：建 grant → reminder → 调度 → 策略 → 派发 → `stored_in_inbox`，迟到如实记账，全程零模型调用。

- 证据闭包有效：故意让模型引用不存在的 evidence 去 notify，被 `cites unknown evidence` 拒绝。

- 投递状态机：`delivery_unknown` 不自动重试；网络 IO 在事务外；每次尝试进 `delivery_attempts`，provider 幂等键按 message 记。

- 崩溃恢复、fence 单调递增、迁移只增不改带校验、配置错误默认 fail closed。

- 常驻进程单线程，内存占用很低（评估环境实测约 20 MB RSS）。

测试失败多数来自运行环境而非代码：hook 沙箱的临时目录被 `bwrap --tmpfs /tmp` 一类加固遮蔽；`@tool` 在 Python 3\.12 上不认 PEP 604 的 `X | None` 注解。**不要为了让测试变绿去改测试或吞掉失败**，先把环境因素和真缺陷分开。

已知边界（v2 不试图打破）：

- 兴趣只能从可信入口写（CLI `pas proactive` / trusted\-UI `input.note`），RPC 刻意不提供 `grants.create`。

- `suggest_watch` 提议后必须人来 `resolve`。

- `request_external_action` 必须过审批。

---

## 2\. 参考材料：一份声明「一个 prompt 复现 Muse」的提示词原文

来源：项目所有者 2026\-10\-18 提供的截图。**收录目的是作为决策层的对照样本，不是实现规格。** 全文逐字保留：

```
# proactive_notifier prompt

任务：判断现在是否值得主动给用户发一条消息。有值得说的才发，没有就静默。

检查步骤（按顺序，任一步不满足就输出 [SILENT]）：

1. 空闲检查：看网关日志，用户最后一条 inbound message 必须在 1 小时之前。如果用户 1 小时内发过消息（正在忙或刚刚聊完），→ 输出 [SILENT]。
2. 频率检查：看今天 cron/output 里是否已经发过 proactive 消息。每天最多 1 条，已发过 → 输出 [SILENT]。
3. 内容检查：从以下五类理由里找一条真正值得打扰的事（一次只发一条，挑最值得的）：

- time_sensitive：有 deadline 的事快到了
- changed：用户关心的事有变化
- requested：用户之前交代你盯的事，现在有进展可以汇报
- ready_for_decision：你已经准备好的东西在等他拍板
- new_opportunity：新的机会

五类都没有 → 输出 [SILENT]。

别只盯着内部记录：判断需要外部信息时，根据你对用户的了解（他是谁、记得什么、偏好什么、聊过什么）主动去搜索。

4. 过滤检查（有内容也要过这一关，不满足就输出 [SILENT]）：

- 过时了 (moot)：这事已经过了时间点，说了也没用 → [SILENT]
- 已处理 (already_handled)：用户已经知道或处理过 → [SILENT]
- 未核实 (unconfirmed)：信息来源不可靠、没把握 → [SILENT]
- 现在不值得 (not_useful_now)：是真的，但分量不够打扰一次 → [SILENT]

输出规则：

- 简洁明了，把事说清楚就行，不要废话连篇、长篇大论。不要标题、不要 emoji。
- 口语化，像朋友提醒一句。说明白是什么事，不用解释原理。
- 不要输出你的判断过程，直接输出正文。
- 不值得打扰时只输出 [SILENT]。
```

### 2\.1 它覆盖了什么、没覆盖什么（决定 W1\-W3 的取法）

|维度|这段 prompt|PAS 现状|结论|
|---|---|---|---|
|「该不该说」的判断规则|四步短路 \+ 五类理由 \+ 四条否决，写得干净|`KIND_ENUM` 只有动作类型，没有理由维度|**借理由枚举（W1）**，否决项借成静默理由（W3）|
|用户在场|依赖读网关日志判 last inbound|只有静态静默窗口，不知道用户刚说过话|**补在场门（W2）**，但事实由宿主注入|
|频率控制|grep 输出目录推断今天发过没|有配额账本|用账本，不要学 grep|
|事实来源|让模型「主动去搜」|证据闭包 \+ 单一 Bing News 源|闭包保留；**先补源产能（W4）**，否则模型只能搜泛新闻|
|状态与可解释性|无（静默即无记录）|有账本，但没语义化静默理由|**补 reason code（W3）**|
|成本|每个 tick 一次全量模型调用|heartbeat 有 L0，task 每槽一次|**补两段判定（W5）**|
|反馈|无|无（只记投递状态）|**补反馈回路（W6）**|

核心判断：那个 prompt 复现的是决策层写得好不好，复现不了判断所依赖的事实。v2 的价值在于把这几项从「模型自述」变成「宿主可查」，**不是把 prompt 搬进 PAS**。

---

## 3\. 工作项

编号即建议实施顺序。每项都给：动机 / 改动面 / 做法 / 验收 / 风险。验收一律要求可机械判定，不接受「跑起来看着对」。

### W1 决策里加结构化理由（reason）

**动机**：模型现在只能说「我要通知」，说不出为什么值得打扰。没有理由维度，账本无法统计哪类通知真有用，阈值只能靠拍脑袋。

**改动面**：`decision_contract.py`（proposal schema）、`executor.py`（解析与一次修复）、store 迁移（runs 表加列）、ledger 读取路径。

**做法**：

1. `DecisionProposal` 增加 `reason`，取值 `time_sensitive | changed | requested | ready_for_decision | new_opportunity`。仅当 `kind ∈ {notify_self, draft}` 时必填。

2. 缺失或非法 → 视为非法决策（与证据闭包同级处理），走现有的「最多修一次」再 fail closed。**不要**默认填一个值。

3. 契约版本 \+1；配置加 `policy.require_reason`（默认 true）。设为 false 时接受缺失并记为 `unspecified`，用于灰度与回滚。

4. 迁移只增不改；老 state 的既有行回填 `unspecified`，不做推断。

5. 提示词侧由执行器注入 enum 定义与「每条理由必须能指出本轮上下文里的具体事实」，与证据闭包配合。

**验收**：

- 五个枚举值各一条单测，全部能落库并在 ledger 的 sent 视图读出。

- 缺失 reason 的 proposal 被拒，且错误信息里带字段名。

- 老 state 上跑迁移两次，结果幂等，行数与内容不变。

- 用 local\_inbox 目标跑一次真实 notify，账本能读到 reason 字符串。

**风险**：模型为了满足必填而编理由。缓解：理由必须绑定本轮 evidence（沿用闭包校验），并在报告里统计 `reason=unspecified` 的比例作为质量指标。

---

### W2 用户在场门（activity gate）

**动机**：现有静默手段只有静态 `notification_window`（语义是静默窗口，不是允许窗口）。运行时知道自己发过什么，不知道用户刚说完话。这一条对打扰率的改善最直接。

**改动面**：policy 层新增 predicate；facade 注入一个 probe；宿主适配层实现 probe。内核不直接读宿主存储。

**做法**：

1. 定义 `ActivityProbe` 协议：`last_user_activity_at() -> datetime | None`。失败返回 `None` 并记 warn，**不因此阻断**（fail open，避免宿主故障变成永久静默）。

2. `PolicyConfig` 增加 `idle_gate: true` 与 `idle_min_minutes: 60`。判据 = `now - last_user_activity_at >= idle_min_minutes`。

3. 门放在 coordinator 入选阶段而不是派发阶段：用户在场就不必起 run，省一次模型调用。短路理由写 `user_active`（见 W3）。

4. **reminder 不受此门约束。** 闹钟是承诺，必须守时；只有 heartbeat/task 产出的 `notify_self` / `draft` 过门。这一点必须有回归测试，否则会吞掉提醒。

5. 宿主适配层实现 probe：用该环境已有的会话/消息查询接口拿最近一条用户入站消息时间；只读、带超时、异常一律返回 `None`。不在内核里硬编码任何存储格式。

**验收**：

- 单测：假 probe 返回 5 分钟前 → 短路且 `silence_reason=user_active`；返回 2 小时前 → 正常走判定。

- 边界：恰好 60 分钟，按「不小于」放行，测试固定时钟。

- 回归：`idle_gate=true` 时 reminder 仍按时投递（真时钟 e2e，脱生产 state）。

- probe 抛异常时系统继续工作，日志有一条 warn，不产生重复通知。

**风险**：与宿主存储的耦合。缓解：只读 \+ 超时 \+ fail open，且实现留在适配层。

---

### W3 沉默也要有语义理由（silence\_reason）

**动机**：现在只有 L0 的机器理由（源没变化）。没有「过时了」「分量不够」，就无法回答「为什么三天没吱声」，也无法判断阈值是不是太紧。

**改动面**：`RunReport` / run 记录；coordinator 各短路点；ledger activity 视图。

**做法**：

1. 枚举（宿主侧）：`no_source_change` / `user_active` / `quota_exhausted` / `quiet_window` / `gate_error`。

2. 枚举（模型侧，可选字段）：`moot` / `already_handled` / `unconfirmed` / `not_useful_now` / `reason_none`。

3. 模型输出 `silent` 时可带 `silence_reason`；未带记 `unspecified`。未知值不得崩溃：降级为 `unspecified` 并记 warn。

4. ledger 的 activity 视图输出理由字符串，不再只给布尔。

**验收**：

- 每个枚举至少一个用例覆盖。

- 注入未知 reason 值 → 服务不崩，落 `unspecified`，warn 一条。

- 构造三种不同的短路原因，在账本里各自可读。

**风险**：枚举膨胀。规则：只在「会改变运维判断」时才加新值。

---

### W4 来源抽象与产能（这是天花板）

**动机**：公开源只有 Bing News RSS，等于只有「新闻」。值得盯的东西（机构公告、期刊目录、专题页更新）大多没有 RSS。决策层再好，没素材也只能沉默。

**改动面**：`sources/` 抽成 `SourceConnector` 协议；新增通用 connector；配置里加源定义。

**做法**：

1. 协议：`probe(cursor, now) -> ProbeResult{items, next_cursor, changed}` \+ `describe()`。现有 Bing 实现重构成第一个实现，行为不变。

2. 新增三类通用 connector：

    - `rss`：任意 RSS/Atom URL，域名 allowlist 必填。

    - `sitemap_diff`：取 `sitemap.xml`，比对 URL 集合与 `lastmod`，只输出新增/变更条目。

    - `page_hash`：取页面，stdlib `html.parser` 抽正文（去 script/style/nav），归一化后做 shingle 哈希；变化超阈值才算 `changed`，避免广告位抖动。

3. 安全（新增网络面，默认关闭）：仅 http/https、禁内网与回环、域名 allowlist 必填、单响应大小上限（建议 2 MB）、连接与总超时、固定 UA、不跟随跨域重定向。

4. 成本：`sitemap_diff` 与 `page_hash` 零模型调用，只在 `changed` 后起 run。

**验收**：

- 三类 connector 各有本地 fixture 单测，**不联网**。

- 离线 e2e：本地起一个静态站点服务，验证「首轮无变化 → 次轮改一个文件 → 恰好一条候选」。

- 安全测试：指内网地址、超 allowlist、超大响应、重定向跨域，四条都必须被拒并记明确错误。

- 真实网络只做手动验证，不纳入 CI。

**风险**：这是 v2 里最大的一块，且会引入 SSRF 面。若时间不够，先交协议 \+ `rss` \+ `sitemap_diff`，`page_hash` 单独排期。

---

### W5 两段判定：便宜的在前

**动机**：`task` 模式每槽一次完整 run，没有变化也照烧；`heartbeat` 的 L0 只覆盖「源没变化」。要在有候选之后也保持便宜。

**改动面**：coordinator 统一路径；ContextPack 组装；配置项。

**做法**：

1. 统一为 `probe → candidates → run`：probe 空则短路（`no_source_change`），非空才起 agent。

2. 候选去重（按条目 id \+ 内容哈希），上限 `policy.max_candidates`（建议 10）；超出的按源的新鲜度排序截断，并记 `truncated` 计数。

3. `task` 模式默认走这条路径；需要旧的「每槽必跑」语义时显式 `always_run: true`（兼容开关，默认 false）。

4. 账本增加每次 tick 的 `model_calls` 与 `candidates` 计数，便于事后核成本。

**验收**：

- 用假模型计数器断言：零候选 tick 的模型调用 = 0；一候选 = 1；`always_run=true` 时与旧行为一致（回归测试）。

- 账本能读出 `model_calls`、`candidates`、`truncated`。

- 连续 24 个 tick 的模拟数据里，空转调用数不超过配置预期。

**风险**：probe 本身的网络成本会被忽略。缓解：probe 超时预算单独配置，并在账本记 probe 耗时。

---

### W6 打扰后悔回路（feedback）

**动机**：账本记的是投递状态，不是用户的反应。发了没人理、或回一句「这我知道」，都是信号。不回灌，阈值永远靠时间调。

**改动面**：RPC 新增 `feedback.record`；store 新增反馈表；统计视图。

**做法**：

1. `feedback.record{message_id, signal, at}`，`signal ∈ {replied, ignored_24h, dismissed, complained}`。**只对 trusted UI 开放，不加进模型工具面**，模型不能写自己的成绩单。

2. `ignored_24h` 由宿主侧定时任务回填（读受控通道的回复情况），不由模型判断。

3. 派生只读报表：每 reason 的 acceptance 率、每 source 的产出率与无效比。CLI `pas stats --since`。

4. **不自动调阈值。** 只给建议值，由人改配置。理由：自动收紧会被单一事件带跑。

**验收**：

- 聚合逻辑单测：构造 10 条样本，acceptance 率正确到小数。

- `pas stats --since 7d` 能出表；无数据时不报错。

- 模型工具列表里**没有** feedback 写入方法（断言注册数量与名单）。

**风险**：把「没回」当「不喜欢」会误判（人可能就是在忙）。缓解：`ignored_24h` 只做弱信号，权重低于 `dismissed`/`complained`。

---

### W7 宿主执行器（等前六项跑出数据再开）

**动机**：PAS 自带执行器是几个内置工具的小循环，不是宿主 agent 本体。要让语气、记忆、工具面正确，得把每轮分析交给宿主 agent。

**前置门**：W1\-W5 完成；有至少 7 天账本数据可对比；以下三项测出数字后才允许开。

**做法**：

1. `PAS_EXECUTOR=hermes`（或对应的宿主适配器）：走宿主 Runs API，使用该 profile 的会话与工具面。

2. 计量：宿主侧的进程/容器资源上限、每轮 token、内存峰值、单轮时延。

3. **headless run 不得进长期记忆**：请求里带标记，或在记忆侧按 source 过滤。否则主动分析轮次会稀释对话记忆。

4. 回滚开关保持一键：`PAS_EXECUTOR=builtin` 立即回退。

**验收**：

- 一次真实主动分析由宿主 agent 本人产出，账本里有 `run_id` 与 `host_session_id`。

- 有实测数字：内存峰值、token/轮、时延。

- 记忆条目数不因 headless 轮次增加（前后各取一次计数）。

- 切回 builtin 后系统行为与 v2 一致。

**风险**：资源与配额。若峰值逼近宿主上限，默认不开，只保留开关。

---

## 4\. 顺序与依赖

```
W3 (契约小改) ─┐
W1 (reason)   ─┴→ 同一次契约变更提交
W2 (activity gate) ── 依赖 W3 的 reason code
W5 (两段判定) ── 依赖 W4 的 probe 协议（可先定义接口再实现）
W4 (源)       ── 独立，工作量最大
W6 (feedback) ── 依赖 W1 的 reason 才能聚合
W7 (executor) ── 最后，前置门全部通过
```

建议：W1\+W3 一个 PR；W2 一个；W5\+接口一个；W4 内部再拆 2\-3 个；W6 一个；W7 单独立项。

---

## 5\. 全局护栏（违反即打回）

1. **不加第三方依赖**，保持纯 stdlib；确有需要先报告再申请。

2. **Python 3\.11/3\.12 兼容**：`@tool` 注解用 `Optional[X]`，不用 `X | None`。

3. **迁移只增不改**，幂等，带版本校验。

4. **可信入口不放宽**：`grants.create`、`suggestions.resolve`、`approvals.resolve` 不得进 RPC 或模型工具面。

5. **证据闭包不放松**：任何通知都必须引用本轮上下文里真实存在的事实。

6. **`delivery_unknown`**** 不自动重试**。

7. **外发默认关**：测试一律用 `local_inbox`；首条真实外发属于对外动作，要单独批准。

8. **配置仍是 YAML 子集**：不支持块序列，`notification_window` 必须嵌套 `start/end`，`shutdown_grace_seconds` 是整数。

9. **`policy.notification_window`**** 是静默窗口**，不是允许发送时段。W2 不要在它上面叠语义。

10. **`ExecStart`**** 顺序**是 `pas --config X serve --app Y`，写反会进 restart 循环。

11. **不要交一个只在特定主机成立的实现**：该暴露的配置项暴露，路径与凭据由部署方填。

---

## 6\. 验证环境配方（脱生产）

1. throwaway `state_dir` \+ `owner_destination="local-inbox:<x>"`，不给任何能外发的 sink，物理上不可能误发。

2. 建 `mode=task` \+ `runonce` 的 job，`at` 设 1 秒后，**sleep 过点再 ****`agent.tick(max_runs=1)`**，否则 `admitted` 为空。

3. 失败原因不在 `runs` 表（那里只有 `error_class`），在 `RunReport.reason` —— 把 tick 的 report 整个打印出来。

4. 验 sink 用传输层一定拒的目标（不存在的收件人），拿到 `failed_*` 映射即证明真执行了发送。

5. 验权限边界：让模型拿假证据去 notify，必须被闭包挡回。

6. 若运行环境有 `bwrap --tmpfs /tmp` 或 `PrivateTmp` 一类加固，hook 测试的临时目录不要放 `/tmp`，换一个不被遮蔽的目录。

---

## 7\. 非目标

- 不做「机器自己开关注」。运行时提议、人来批，保持现状。

- 不复刻第 2 节那段 prompt 去做一个 cron 版替代品；改的是 PAS 本身。

- 不做 dashboard、不发真实消息、不动 sink 的截断与去链接逻辑。

- 不为了「看起来完整」而在 CI 里跑真实网络测试。

- 不把某个具体部署的配置写进仓库。

---

## 8\. 交付物

1. 独立分支，按第 4 节拆 PR；每个 PR 自包含、可单独回滚。

2. 新增测试并保持既有测试不全红（环境类失败需在报告里标注为环境产物，不得改动测试来「修绿」）。

3. 一份 `IMPLEMENTATION-v2.md`：每项的改动面、验证命令与真实输出、未验项、回滚方法。

4. 按实情更新 `COMPATIBILITY` 矩阵；没验过的照旧标 ❌，不要顺手改成 ✅。

完成后回报三件事：做了什么、证据是什么（命令 \+ 输出）、哪些没做以及原因。不要只报「已完成」。

