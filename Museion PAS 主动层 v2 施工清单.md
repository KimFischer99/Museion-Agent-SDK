# Museion PAS 主动层 v2 施工清单

- 依据：`Museion PAS 主动层 v2 改进计划.md`（v2 草案，2026-10-18；已按计划 §0 的「只写仓库事实与设计决定」边界修订）
- 施工对象：本仓库根目录（product 仓库；`01/` 是 git-ignored 的本地交接包，只读参考，**不要改**）
- 目标基线：`origin/main` @ `b875f88a06a7`（本地 HEAD `67d52be` 落后 1 个 commit，开工先 fetch 到基线）
- 施工方式：单个 agent（我本人）顺序施工，按 PR 拆分；每 PR 自包含、可单独回滚、可随时停下
- 验收原则：可机械判定；环境类失败照旧记为环境产物，**不得改测试修绿**
- **设计目标（一切取舍的锚点）**：本 SDK 是 **plugin-first 的 proactive runtime**——其他 agent 装载后即获得 Muse 式主动能力。凡不损害该目标的范围放大可以接受；凡触及模型工具面/证据闭包/可信入口边界的放宽一律不做。
- **环境边界（照抄计划 §0 的约束）**：本清单不写任何特定主机的路径、服务名、profile 名、数据库位置、账号或部署拓扑；宿主侧工作只按「适配层契约」描述，实现留给部署方。基线数字（测试失败数、内存等）来自评估环境，**不同环境会有差异，以本地实跑为准**（见 P0-2/P0-3）。

> 读法：§1 是开工前必做；§2 是本清单相对计划原文的**事实校正**（不读会改错文件）；§3 是要先定夺的设计决策；§4 是逐项施工卡；§5 是施工批次与检查点；§6–§8 是验收、护栏与交付物。

---

## 0. 一分钟结论

**顺序（7 个施工批次）**：B0 开工准备 → B1（W1+W3 同一次契约变更）→ B2（W2 在场门）→ B3（W5 两段判定，先落 W4 协议接口）→ B4/B5/B6（W4 源：协议+SDK 注册+rss / sitemap_diff / page_hash+安全）→ B7（W6 投递反馈回路）→ W7 单独立项（前置门通过后再排）。

**三个必须先决、否则一定返工的点**：

1. **W4 的接入方式已定（D6）**：不做配置文件语法扩展，改为 **SDK 程序化注册**（宿主 app 模块里 `registry.register(...)`）。`config.py` 是严格 YAML 子集、不支持块序列，而**每个部署本来就有 app 模块**，程序化注册对「其他 agent 装载」更通用；配置化留作可选后续。
2. **W1 的「runs 表加列」位置错了**：reason 是 proposal 级，落在 `run_proposals`，并要在读面（runs/inbox/activity）透出；`runs` 表只该加 `silence_reason`（W3）。
3. **W5 的 task 模式新门会误伤两类现有语义**：无源纯推理 task（现在必须跑）和手动触发（`pas jobs trigger` 用户显式要求跑）。门必须对这两类豁免，否则是把已知正确行为改坏（见施工卡 W5「坑」）。

**施工单元（不按人日计量）**：本清单由 agent 执行，人日/人力并行是排期单位，对 agent 施工无意义。计量单位改为「施工批次」：6 个 PR + 1 个立项 = 7 个可独立验证、可随时停下的批次，批次间只有依赖顺序。每批的进入条件、检查点与产出见 §5。

---

## 1. 开工前 P0（半天，全部通过才动代码）

### P0-1 基线与分支

```bash
git fetch origin
git rev-parse origin/main          # 期望 b875f88a06a7292a9c01ef3f450ca5c32bf5c4b5
git switch -c feat/pas-v2 origin/main
git log -1 --format='%h %s'        # 期望 b875f88
```

- [ ] 不要从本地 HEAD `67d52be` 开工（计划基线是 `b875f88`，与本地差 1 个 commit，先 fetch 到基线）。
- [ ] 不要动 `main`；如需并行，用 `git worktree add ../pas-v2-work origin/main`。
- [ ] 计划文档 `Museion PAS 主动层 v2 改进计划.md` 目前是 untracked 且未被 ignore：**先决定它是否随分支提交**（见 D10），别开工后夹在代码 commit 里。

### P0-2 Python 3.11/3.12 验证环境（当前本机是缺口）

计划 §0 说明：基线数字来自一次**评估环境**（Linux，Python 3.12），「不同环境会有差异，开工第一步是自己跑一遍基线，以你跑出来的结果为准」。本机实测：Python 只有 **3.14.4**，无 pytest、无 venv，而护栏 #2 要求 3.11/3.12 兼容，且测试入口是 **unittest**，不是 pytest。

```bash
# 主基准：3.12（与计划评估环境一致）
pyenv install 3.12.7            # 或 uv python install 3.12
python3.12 -m venv .venv-312 && .venv-312/bin/python -m pip install -e .
.venv-312/bin/python -m unittest discover -s tests
```

- [ ] 3.11 与 3.12 各跑通一次（3.12 是主基准，3.11 是兼容下限）。
- [ ] 计划 §1 提到的 hook 沙箱临时目录被 `bwrap --tmpfs /tmp` 一类加固遮蔽，属**特定 Linux 加固环境**现象；本机 macOS 不复现。需要这类覆盖时在 Linux 容器里另跑，**不要改测试去绕**。
- [ ] 预检 `@tool` 注解坑：`toolkit.schema_for_type` 只认 `typing.Union`（`Optional[X]`），`X | None` 在 3.12 会走到 "cannot map ... onto a JSON Schema type"（计划 §1 也点名）。新增工具函数一律 `Optional[X]`；可加一条 3.12 的回归用例钉住。
- [ ] 注意：`.venv/` 已在 `.gitignore` 中，可放仓库内。

### P0-3 基线测试与失败清单（不改绿）

计划 §0 已删掉固定的失败数，明确「不同环境会有差异，以你跑出来的结果为准」。本机实测（macOS / py3.14.4）：

```bash
python3.12 -m unittest discover -s tests 2>&1 | tee 01/v2-baseline-312.txt
# 本机 py3.14 预跑结果仅作参考：Ran 851 tests, FAILED (errors=29)
# 正式基线以你在 py3.12 上跑出来的结果为准（计划 §0）
```

- [ ] 记录「测试总数 + 失败/错误集合」作为**本机基线**；本机 29 个 error 属环境类（如 loopback TLS 连接拒绝等），与计划评估环境（Linux / py3.12）的数字不必一致——**差异只记录、不追平、不改测试**。
- [ ] 同时确认计划 §1 的回归底线仍成立：真时钟端到端（reminder 零模型投递到 `stored_in_inbox`）、证据闭包拒绝未知 evidence、`delivery_unknown` 不自动重试、崩溃恢复 / fence 单调、迁移只增不改带校验、配置错误 fail closed——这些对应测试必须在基线绿集合内。
- [ ] 把逐条失败名单存为对照基线；每个 PR 只允许「基线失败集合之外」的新增失败。
- [ ] 交付物/最终报告里标注为环境产物；不把本机数字写成计划的硬指标（计划已撤回该类数字）。

### P0-4 目录与文档纪律

- [ ] 代码只写 product 仓库根；`01/` 是本地交接包（`SPEC.md` / `VALIDATION.md` / `AGENTS.md` 可只读参考），**不进 git、不改、不提交**。
- [ ] 本地验证环境按仓库自己的 `examples/` 与 `release/runtime/app.py` 自建（计划 §0 明确「不需要访问任何特定部署环境」）；不引用任何私有部署的配置、路径或凭据。
- [ ] `release/` 也被 ignore，改发布产物走 `tools/build_release.py` 重建，不手改。
- [ ] 计划 §0 的边界同样约束本清单与代码：不写特定主机的路径/服务名/profile/DB/账号/部署拓扑；该暴露的配置项暴露，路径与凭据由部署方填（对应新增护栏 #11）。代码里不要硬编码部署路径或凭据；`01/` 这类本地目录引用只出现在工作文档里。
- [ ] 注意：上游 `67d52be` 刚**删掉**了 construction docs 与 `tools/validate_*.py` 验证脚本。计划 §8 第 3 条要求的 `IMPLEMENTATION-v2.md` 与 §6「验证环境配方」不能直接引用已删除文件，要新写（见 D10），且内容同样遵守 §0 边界。

### P0-5 施工台账

- [ ] 每个 PR 记录：改动面（文件+符号）、执行的命令与真实输出、未验项、回滚方法。这是最终报告三件事的原料。

---

## 2. 计划 ↔ 代码现实对照（必读，避免按计划字面改错文件）

| # | 计划原文 | 代码现实（已核实） | 施工动作 |
|---|---|---|---|
| 1 | W1「store 迁移（runs 表加列）」 | reason 是 **proposal 级**：`run_proposals` 表（`m004_p3_executor.sql`）经 `store.record_run_decision` 写入；`runs` 只有 `decision_summary/proposal_count/usage_json` | 迁移加 `run_proposals.reason`；`runs` 只由 W3 加 `silence_reason`；读面 join 透出 |
| 2 | W1「`proactive_ledger view=sent` 读出」 | 仓库**没有** proactive_ledger/drill/stats 命令；读面是 `pas runs list/show`、`pas activity`、`pas notifications list`、RPC `runs.*`/`jobs.activity`；sent 账本是 `store.recent_sent_notifications` 内部查询 | 验收映射到 `pas runs show --json` / `pas notifications list --json`；新增字段走 `ProactiveAgent.runs_get` 与 `store.list_inbox` |
| 3 | W1「改动面：decision_contract.py、executor.py」 | 契约是**五处联动**：`schemas/v1/decision.json`、`contracts.py`（`ActionProposal`/`validate_decision`）、`decision_parse.py`（`PROPOSAL_KEYS`）、`decision_contract.py`（`CONTRACT_RULES`/prompt）、`packages/client-ts` + `tools/gen_client_ts.py`；`tests/test_decision_contract.py` 会逐条规则验证「prompt 里说的必须真被对应层拒绝」 | 一次改齐；漏一处 `test_schemas` / `test_client_ts_drift` 立刻红 |
| 4 | W2「PolicyConfig 增加 idle_gate」 | 配置有**两层**：运行期 `policy.PolicyConfig` + 用户 YAML 的 `config.py`（`_POLICY_KEYS` / `PolicySection` / `PasConfig.policy` / parse / emit）；且现有 `facade._policy_config_from_agent_config` 只映射到 `PolicyConfig`，不映射 `CoordinatorConfig`；`release/runtime/app.py` 又要再映射一次 | 两层 + 三处映射都写；否则 YAML 配不上、或配了不生效 |
| 5 | W2「运行时不知道用户刚说过话」 | 部分不成立：PAS 自己记 `input.note`（`facade.note_user_input` → `store.admit_user_wake` → `events` 行 `origin='manual'`、payload `{"reason": text}`）。宿主会话不经过 PAS，才需要注入 probe | 内置 probe 查自己的 `events`（origin='manual' 的最大 observed_at_ms）+ 可注入 host probe，取 max；见 D3 |
| 6 | W3「silent 也要有语义理由」 | 宿主短路已有机器字符串：`l0_nothing_to_check` / `l0_source_unauthorized` / `l0_source_error:<...>` / `l0_no_source_change` / `l0_context_stale`（`coordinator._suppress` → `store.record_run_suppressed` → `runs.state='suppressed'` + `run_events.kind='suppressed'`）；`RunReport.reason` 只在进程内、**不落库**；`job_activity.reason` 列存在但 **L0 抑制不写 activity 行** | W3 = 枚举映射 + 持久化（`runs.silence_reason`）+ 给 L0 抑制补 `job_activity` 投影；不要改原有 `l0_*` 字符串（日志/测试有依赖） |
| 7 | W3「模型 silent 可带 reason；未知值降级」 | 模型输出是**严格闭集**：`decision_parse.DECISION_KEYS = {decision,summary,proposals}`，未知字段直接拒 | 加 `silence_reason` 必须同步 schema/parse/contract/prompt/TS；「未知枚举值降级 unspecified」是对严格哲学的**刻意例外**，只许对这一个字段生效并记 run_event，需单独测试 |
| 8 | W4「sources/ 抽成 SourceConnector 协议」 | 没有 `sources/` 包；已有 `contracts.Source` 协议（`fetch_delta`）+ `context.SourceRegistry` + `research.NewsSearchSource`（Bing）+ `connectors.GmailMailSource` | 复用现有协议（`fetch_delta` 即 probe 原语），补 `describe()`；新 connector 放新包 `src/proactive_sdk/sources/`；不要重写研究源（`tests/test_research_source.py` 是回归） |
| 9 | W4「配置里加源定义」 | `config.py` 是严格 YAML 子集：**块序列直接报错**（"block sequences are not part of the config grammar"）、未知 key 直接拒 | 按 D6 改为 **SDK 程序化注册**，配置语法不动；W4 的成本回到 connector 实现与安全边界，不再是配置格式设计 |
| 10 | W4 安全要求 | `net.EgressBroker` 已有：仅 https、host allowlist 必填、禁内网/回环（SSRF 解析后再校验 IP）、`max_bytes` 默认 1 MiB、总超时、固定 UA、**拒绝一切重定向**、content-type 白名单（`text/*` 已含 html） | 大部分已具备；补：2 MB 上限（构造参数）、per-source allowlist 配置校验。计划说「不跟随跨域重定向」，现状更严（全拒）——建议保持全拒并写进文档，不要让 page_hash 为了重定向放宽 |
| 11 | W5「task 模式默认 probe→candidates→run」 | 现状**所有模式都已 fetch**（L0 变更探测已执行），task 只是不用 `changed` 做门；且 task **无源 = 纯推理仍必须跑**（`coordinator._process` 注释明说）；无 job 的 hook/手动 wake 在 `_task_of` 里返回 `mode='task'`，而带 job 的手动 trigger 返回的是 `job.mode` | 新门只对「有源且非 always_run」的 task 生效；无源 task 与 `origin='manual'`（手动触发/用户 wake）必须豁免（见 W5 卡） |
| 12 | W5「配置 switch always_run」 | 更自然位置是 job 级 `task.always_run`；`schemas/v1/job_spec.json` 的 `task` 是 `additionalProperties:false`，`contracts.JobSpec` 有逐字段校验 | 加 schema + JobSpec 校验 + CLI flag + TS 重生成；放 config 全局开关会失去逐 job 语义 |
| 13 | W6「RPC 新增 feedback.record / store 新增反馈表」 | 仓库已有的是一套**偏好反馈**：`feedback` 表（m001）、`store.record_feedback`、`FeedbackManager`、RPC `notifications.feedback`、CLI `pas notifications feedback`（kind = handled/not_useful/mute_topic/...）。它记录的是「用户偏好」，不是「投递结果」 | 按 D5 **分开建**：新表 `delivery_feedback`（replied/ignored_24h/dismissed/complained）+ 新 RPC `feedback.record`；旧偏好反馈路径**不动**，避免两种语义混在一条表/一个方法里 |
| 14 | W6「插件工具列表里没有 feedback 写入」 | 插件在 `examples/pas_hermes_plugin/`，当前恰好 5 个工具且 `tests/test_pas_plugin.py` 精确断言名单，本来就没有 feedback | 该验收已天然满足；只需保留断言防回退 |
| 15 | W7「PAS_EXECUTOR=hermes 走宿主 Runs API」 | `release/runtime/app.py` 已支持 `PAS_EXECUTOR=model\|pi\|hermes`；`HermesRunsExecutor`/`HostBridge` 已存在 | 仓库内缺口：profile 作用域会话如何表达（按宿主 API 契约在适配层确认，**不在内核硬编码路径**）、`host_session_id` 记账、headless 标记、计量（宿主侧采集）；长期记忆侧过滤在宿主侧 |
| 16 | 护栏 #4「grants.create、suggestions.resolve、approvals.resolve 不得进 RPC 或模型工具面」 | 现状：`grants.create` **不在** RPC；`suggestions.resolve` / `approvals.resolve` **就在** RPC（trusted control plane），但**不在**插件/模型工具面（插件只有 5 个 `proactive.*`） | 正确不变量是「不放宽**模型工具面**」；不要按字面去删已有 RPC（会破坏冻结方法集/TS/测试） |
| 17 | 计划 §8 第 3 条要 `IMPLEMENTATION-v2.md` | `67d52be` 刚专门删除了 construction docs 与验证脚本，理由是「不是产品」 | 决定存放位置（D10），别把刚删掉的东西加回 product main |
| 18 | 计划 §1「测试失败多数来自运行环境」（v2 计划已删除固定失败数，§0 要求自跑基线） | 本机 `unittest discover`：851 tests / 29 errors（环境类） | 记录为本机基线；不同环境数字不必一致，P0-3 建对照，后面每 PR 只比集合变化 |

---

## 3. 开工前必须定夺的决策点（建议在 B1 开工前全部拍板）

| ID | 决策 | 建议 | 影响 |
|---|---|---|---|
| D1 | W1 reason 落库位置与读面 | `run_proposals.reason` 落库；读面：`runs_get` 带 proposals、`list_inbox` join 出 reason。**不**把 reason 复制进 outbox（避免双写不一致） | W1、W6 |
| D2 | `require_reason` 配置层 | 用户 YAML 放 `policy.require_reason`（符合计划），运行期放 `ExecutorConfig.require_reason`，由 facade 映射；`parse_decision_text` 加参数，executor 与 `host_bridge` 都走同一入口 | W1 |
| D3 | W2 门的位置与 probe 优先级 | 先做 coordinator L0（0 模型调用、改动小）；probe 值 = max(内置 events probe, 注入 host probe)；`None`/异常 = 放行（fail open）；`origin='manual'` 的 wake 豁免（否则刚说完话会吞掉自己的请求）。若日后嫌 suppressed run 行噪，再前移到 admission（记录 skipped occurrence） | W2 |
| D3b | W2 配置归属 | 运行期放 `CoordinatorConfig.idle_gate/idle_min_minutes`（门在 coordinator），用户 YAML 放 `policy` 块并映射；不要塞进 `PolicyConfig` 造成「配置在 policy、生效在 coordinator」的错位 | W2 |
| D4 | W3 宿主侧枚举与现有 `l0_*` 字符串的关系 | 枚举 5 值 + `None`；`l0_nothing_to_check`/`l0_source_unauthorized`/`l0_source_error` 映射不到的，保留 raw detail、`silence_reason=None`（不硬塞新枚举；枚举膨胀按计划风险条控制） | W3 |
| D5 | W6 是否新增 `feedback.record` | **新增** `feedback.record` + 新表 `delivery_feedback`（按计划），旧 `notifications.feedback` 与 `feedback` 表不动。理由：plugin-first 意味着第三方宿主会直接对接 RPC，投递结果信号值得一个显式、类型化的方法；加法扩展协议可接受（B7 同步方法集 + TS + drift 测试） | W6 |
| D5b | `ignored_24h` 由谁判定 | PAS 只能判「24h 无任何反馈」；「是否回复」由宿主侧回填（RPC/CLI 注入）。回填开关默认关，避免单事件误判 | W6 |
| D6 | W4 源接入方式 | **程序化注册**：新 connector 作为公开 SDK 类（`proactive_sdk.sources.*`），宿主在 app 模块里 `registry.register(...)`；不扩展 `config.py` 语法（护栏 #8 不动），也不加第二份配置文件。配置化（`sources_file` JSON）留作可选后续。理由：plugin-first，每个部署本来就有 app 模块；注册即代码对宿主最通用 | W4 全部 |
| D7 | W4 首轮语义与 cursor 上限 | sitemap/page_hash 首轮只建基线、输出 0 条（否则上线即误报）；cursor 存 `source_state.cursor_ref TEXT`，设大小上限（如 64 KiB），超限退化为「hash + 全量 changed」 | W4 |
| D8 | W5 计数口径 | 用「run 级」：`runs.model_turns` 即模型调用数（suppressed=0），新增 `runs.candidates_count/truncated_count`；daemon tick 级另由 metrics 聚合。不要为「tick」新造一套账本 | W5 |
| D9 | W7 回滚开关命名 | 现状 `PAS_EXECUTOR=model` 就是内置执行器，**没有** `builtin` 这个值；保持 `model` 并在文档写明「计划里的 builtin ≡ model」；若要改名，`release/runtime/app.py` + `deploy/README` + 测试一起改 | W7 |
| D10 | v2 工作文档存放 | **已降级为非决策**：计划与实现记录都是 reference 文档，施工完成并验证通过即可。计划稿留 `01/`（本地输入）；`IMPLEMENTATION-v2.md` 作为交付物生成，位置不阻塞施工（默认 `01/`，需要留痕再进 `docs/`） | 全部 |

---

## 4. 逐项施工卡

每张卡结构：**目标 / 现实锚点 / 改动面 / 步骤 / 验收（可机械判定）/ 坑**。文件链接均相对仓库根。

### W1 决策里加结构化理由（reason）— B1 前半

**目标**：`notify_self` / `draft` 提案必须带 `reason ∈ {time_sensitive, changed, requested, ready_for_decision, new_opportunity}`；缺失/非法 = 非法决策（与证据闭包同级，最多修一次后 fail closed）；灰度可关。

**现实锚点**
- 模型输出解析唯一入口：`decision_parse.parse_decision_text`（严格 JSON、未知字段拒绝、`DECISION_KEYS`/`PROPOSAL_KEYS` 白名单、`validate_decision`、证据闭包）。
- 决策契约与 prompt：`decision_contract.decision_contract()` / `agent_system_prompt()`；`CONTRACT_RULES` 每条规则都写明由哪一层执行，`tests/test_decision_contract.py` 会实际驱动该层并断言被拒。
- 双执行器都要覆盖：`executor.ToolLoopExecutor._parse_decision` 与 `host_bridge` 的 `parse_decision_text` 调用。
- 落库：`store.record_run_decision` 写 `run_proposals`（kind 白名单也在这里，需同步 reason 校验）。

**改动面（一次改齐，禁止只改 Python）**
1. `src/proactive_sdk/contracts.py`
   - 新增 `REASON_ENUM`（单一来源；`decision_contract` 从这里导入，仿 `KIND_ENUM` 与 `_PROPOSAL_KINDS` 的 drift 测试模式）；
   - `ActionProposal.reason: str | None`、`__post_init__` 校验枚举、`to_dict()` 输出；
   - `validate_decision` 加跨字段规则：`kind ∈ {notify_self, draft}` 时 reason 必填。
2. `src/proactive_sdk/decision_parse.py`
   - `PROPOSAL_KEYS` 加 `reason`；解析时透传。
3. `schemas/v1/decision.json`
   - `$defs.action_proposal.properties.reason`（enum + 描述）；
   - 用 `allOf/if/then` 表达「notify_self/draft 必填」（结构约束），`validate_decision` 仍保留兜底。
4. `src/proactive_sdk/decision_contract.py`
   - `CONTRACT_RULES` 加两条：`reason_enum`（layer=`schema`）、`reason_required`（layer=`decision`）；prompt 文本加入规则；
   - `DECISION_CONTRACT_VERSION` 从 `1.0` → `1.1`。
5. `tools/gen_client_ts.py` 重生成 `packages/client-ts/src/schema_types.ts`（`test_client_ts_drift` 强制）。
6. 测试夹具：`tests/test_schemas.py` 的 `VALID_FIXTURES.decision`、`tests/test_decision_contract.py` 的 `_proposal()` 默认值（draft 必须带 reason，否则整套契约测试批量红）。
7. 配置与灰度：
   - `src/proactive_sdk/config.py`：`policy.require_reason`（`_POLICY_KEYS`、`PolicySection`、`PasConfig.policy`、parse、emit）；
   - `src/proactive_sdk/executor.py`：`ExecutorConfig.require_reason: bool = True`；
   - `parse_decision_text(..., require_reason=True)`：false 时缺失应记为 `unspecified`（不是默认填假值）；
   - `src/proactive_sdk/facade.py`：config → ExecutorConfig 映射；`release/runtime/app.py` 同步；
   - 非法值在任何配置下都拒绝（灰度只放宽「缺失」）。
8. 迁移：新建 `src/proactive_sdk/migrations/m012_v2_reason_silence.sql`（W1+W3 共用）
   - `ALTER TABLE run_proposals ADD COLUMN reason TEXT`；
   - 老行回填 `unspecified`（迁移内 `UPDATE ... WHERE reason IS NULL`）；
   - 不得修改 m001–m011 任何文件（护栏 #3）。
9. 写入与读面：`store.record_run_decision` 插入 reason；`store.run_proposals` 返回 reason；`ProactiveAgent.runs_get` 附带 proposals（D1）；`store.list_inbox` join 出 reason。

**步骤**
1. 先落契约（1–4）+ 重生成 TS + 修夹具 → `test_decision_contract` / `test_schemas` / `test_client_ts_drift` 先绿。
2. 再落解析/执行器/灰度开关（7）→ 新增 `tests/test_v2_reason.py`。
3. 再落迁移与写入/读面（8–9）→ 新迁移测试。
4. 最后 e2e：ScriptedModel 走 local_inbox notify，读回 reason。

**验收**
- [ ] 五个枚举各一条单测：落库后 `store.run_proposals(run_id)` 读回原值。
- [ ] 缺失 reason 的 `notify_self`/`draft` 提案被拒，`PASError.safe_message` 含字段名 `reason`；`action_repair` 走且只走一次。
- [ ] `require_reason=false` 时缺失记 `unspecified`，报告/账本能区分。
- [ ] 老 state 迁移两次幂等：行数、内容不变（仿 `tests/test_migration_v011.py` 的「旧库 + 新二进制」做法）。
- [ ] `python3.12 tools/gen_client_ts.py && git diff --exit-code packages/client-ts/src/schema_types.ts` 干净。
- [ ] 真执行器 e2e：`tests` 内用 `ProactiveAgent` + FakeClock 跑一次 notify（local_inbox），读面能读到 reason 字符串。

**坑**
- `notify_self` 已有 `_NOTIFY_SELF_REQUIRED`（evidence_refs/expires_at）机制，reason 要加进同一套校验与契约漂移测试，不要另起一套。
- `internal_record` / `suggest_watch` / `request_external_action` 不要求 reason——写进 schema 条件，别扩大必填面。
- 任何层都不要「默认填一个值」；`unspecified` 只允许出现在灰度关闭或老数据回填。
- reason 一定要能绑定本轮 evidence（计划风险条）：提示词里写「每条理由必须指出本轮上下文里的具体事实」，并保留证据闭包不变。

---

### W2 用户在场门（activity gate）— B2

**目标**：用户在最近 `idle_min_minutes`（默认 60）内说过话时，heartbeat/task 产出的 `notify_self`/`draft` 不必起模型分析；reminder 不受影响；probe 故障 fail open。**用户刚发起的 wake（`input.note`，`origin='manual'`）不过门**——那是用户自己要求的响应，不能因为「用户刚说过话」被吞。

**现实锚点**
- 静默窗口 `windows.quiet_end_ms` 语义是**静默窗口**（护栏 #9），W2 不要在其上叠语义。
- 用户输入有两条来源：PAS 自己的 `events`（`origin='manual'`，来自 `input.note`/`admit_user_wake`）与宿主会话（不经过 PAS）。
- reminder 走 `scheduler._admit_reminder`，**完全不进 coordinator、零模型**——所以门天然不吞 reminder；但仍要有回归测试防止将来把门前移时误伤。

**改动面**
1. 协议：`src/proactive_sdk/coordinator.py`（或 `contracts.py`）定义 `ActivityProbe` Protocol：`last_user_activity_at_ms() -> int | None`（用毫秒，与 store/clock 一致；计划写 datetime，D3 已改为 ms）。
2. 内置实现：`StoreUserActivityProbe`（读 `events` 中 `origin='manual'` 的 `MAX(observed_at_ms)`；只读、短查询，不新增长连接）。
3. `ProactiveAgent.__init__` 增加 `activity_probe: ActivityProbe | None = None`（facade）；默认用内置 probe，注入时取 max（D3）。
4. `CoordinatorConfig` 增加 `idle_gate: bool = True`、`idle_min_minutes: int = 60`（D3b）。
5. `coordinator._process`：在 `_task_of` 之后、source fetch 之前判断：
   - `mode != "reminder"` 且 `idle_gate` 且 job 存在 且 **`origin != "manual"`**；
   - probe 返回非空且 `now - last < idle_min_minutes*60_000` → `_suppress(..., silence_reason="user_active")`（依赖 W3 通道，所以 B1 先行）；
   - probe 抛异常/超时 → logger.warn + `run_events` 记一条 `activity_probe_failed`，**继续执行**（fail open）。
6. 配置：`config.py` 的 `policy` 块加 `idle_gate`/`idle_min_minutes`（`_POLICY_KEYS`、`PolicySection`、`PasConfig.policy`、parse、emit）；`facade` 里**新增** config→`CoordinatorConfig` 的映射（现有 `_policy_config_from_agent_config` 只映射 `PolicyConfig`），`release/runtime/app.py` 同步；`deploy/README.md` 写明部署侧 probe 契约（只读、超时、异常=None、不要长连接、不要写宿主库）。
7. 宿主适配层（**仓库外，部署方实现**）：用该环境已有的会话/消息查询接口取最近一条用户入站消息时间；只读、带超时、异常→`None`；**不在内核硬编码任何存储格式、路径或服务名**（计划 §0 边界 + 护栏 #11）。仓库内只交货 `ActivityProbe` 协议 + fake probe 测试 + `deploy/README.md` 里的适配层契约说明。

**验收**
- [ ] 假 probe 返回 5 分钟前 → `outcome=suppressed`、`silence_reason=user_active`、ScriptedModel 调用数 = 0。
- [ ] FakeClock 恰 60 分钟 → 按「不小于」放行，正常走判定（固定时钟，不 sleep）。
- [ ] 2 小时前 → 正常走判定。
- [ ] probe 抛异常 → 系统继续工作、日志/事件有一条 warn、不产生重复通知。
- [ ] 回归：`idle_gate=true` 时 reminder 仍按时投递，且模型调用为 0（真时钟 e2e 脱生产 state；`local_inbox` 断言 `stored_in_inbox`）。
- [ ] 回归：`idle_gate=true` 且刚有 `input.note` 时，该 wake 自己的 run 仍然执行（`origin='manual'` 豁免）。
- [ ] `config check` + `config print` 对新 key 往返一致；未知 key 仍拒绝。

**坑**
- 门放在 **coordinator L0**（省模型调用），不要放到 delivery/policy 阶段（那样模型已经烧过了）。
- 不要把 probe 做成 import 时连接宿主 DB；不要在 coordinator 里直接打开外部 DB（内核不读宿主库，实现留在部署层或内置 events probe）。
- `notification_window` 一个字都不要动。
- probe 返回 `None` = 未知 = 放行（fail open），不要当成「用户在场」。
- 内置 probe 会读到刚写入的 `input.note`（`origin='manual'`）时间戳：如果不过滤当前事件来源，用户刚说完话会把自己要求的 run 吞掉。豁免只针对 `origin='manual'` 的事件，hook/定时 wake 仍照常过门。
- 顺手加一条 warning：若 W2 门把一次 task 抑制了，W3 的 activity 投影要能看见原因；否则又回到「三天没吱声不知道为什么」。

---

### W3 沉默也要有语义理由（silence_reason）— B1 后半（与 W1 同一个迁移/PR）

**目标**：宿主短路与模型静默都有可读的语义理由；`run`/`activity` 账本能回答「为什么没发」。

**现实锚点**
- 宿主短路：`coordinator._suppress(lease, reason, now_ms)` → `store.record_run_suppressed` → `runs.state='suppressed'` + `run_events.kind='suppressed'`；现有 reason 是字符串 `l0_*`。
- 模型静默：`decision="silent"` 时 `runs.decision_summary` 只有 `summary` 文本，无 reason 维度。
- `job_activity.reason` 列存在，但 **L0 抑制没有写 activity 行**（activity 行目前由 scheduler/reminder 路径写）——这是 W3 的关键缺口。

**改动面**
1. 枚举单一来源：`contracts.py` 新增 `HOST_SILENCE_REASONS = (no_source_change, user_active, quota_exhausted, quiet_window, gate_error)`、`MODEL_SILENCE_REASONS = (moot, already_handled, unconfirmed, not_useful_now, reason_none)`、`UNSPECIFIED = "unspecified"`。
2. 模型侧：`Decision.silence_reason`（可选，仅 `decision="silent"`）；`decision_parse.DECISION_KEYS` 加 `silence_reason`；解析时未知值**降级**为 `unspecified` 并记 `run_events` 一条（刻意例外，注释说明）；`schemas/v1/decision.json` 加 optional enum（`moot|already_handled|unconfirmed|not_useful_now|reason_none|unspecified`）；`CONTRACT_RULES` 加一条 `silence_reason_requires_silent`（layer=`decision`）。
3. 宿主侧：`RunReport` 增加 `silence_reason`；`_suppress` 保留 raw detail（进 `run_events`），`silence_reason` 按 D4 映射：`l0_no_source_change→no_source_change`、`l0_context_stale→no_source_change`?（按 D4 表执行；映射不到→`None` + raw detail）。
4. 持久化：m012 同迁移加 `runs.silence_reason TEXT`；`record_run_suppressed(reason_detail, silence_reason)` 写两处；`record_run_decision` 写模型的 `silence_reason`（proposed/silent 时）。
5. 投影：coordinator 抑制时补 `store.record_job_activity(...)`：phase=`analysis`、state=`suppressed`、reason=`silence_reason`（拿不到 job 时跳过，不伪造）；dedupe 用 run_id 保证幂等。
6. 读面：`pas activity --json`、`pas runs show --json`、RPC `runs.get/list`、`jobs.activity` 都能读到 `silence_reason`。

**验收**
- [ ] 每个枚举至少一个用例（宿主 5 + 模型 5 + `unspecified`）。
- [ ] 注入未知模型值 → 服务不崩、落 `unspecified`、有一条 run_event/警告。
- [ ] 构造三种不同的短路原因（计划原话），在账本里各自可读（FakeClock 集成即可，不要求真实隔天）。
- [ ] `pas runs show <id> --json` 对 suppressed run 有 `silence_reason` 与 raw detail。

**坑**
- 不要改既有的 `l0_*` 字符串（grep `tests/` 已有多处断言）；映射层新写。
- 「未知枚举值降级」只允许 `silence_reason` 一个字段，别的字段保持严格拒绝；要有测试钉住这个边界。
- 计划里 `gate_error` 与 W2 的 fail open 有语义冲突：fail open = 继续跑，没有 silence；把 `gate_error` 只用于「门判不了但选择了抑制」的场景，或干脆不产生（D4）。
- 枚举膨胀：只在「会改变运维判断」时才加新值。

---

### W4 来源抽象与产能（天花板）— B4/B5/B6

**目标**：公开源从「只有 Bing News」扩到「任意 RSS/Atom、sitemap 变更、页面变更」，全部走统一协议、统一安全边界、零模型探测；接入方式按 D6 走 **SDK 程序化注册**（不扩配置语法）。

**现实锚点**
- 协议已存在：`contracts.Source.fetch_delta(request) -> SourceBatch`；`context.SourceRegistry` 负责注册/授权/错误包装；`research.NewsSearchSource`（Bing）与 `connectors.GmailMailSource` 是两个现成实现。
- 网络边界已存在：`net.EgressBroker`（https-only、allowlist 必填、SSRF、大小、超时、UA、拒重定向、content-type 白名单）。
- `source_state.cursor_ref` 是现成的探测状态位。
- 接入方式（D6）：部署必然有 app 模块（`pas serve --app module:factory`），现有 Bing/Gmail 就是在 `release/runtime/app.py` 里程序化注册的；新 connector 沿用同一路径作为公开 SDK 能力，默认关闭（不注册就没有网络面）。

**W4.0 协议决策（先写，再动 connector）**
- [ ] 结论写进 PR 描述：**不新增平行协议**。`fetch_delta` 就是 probe 原语；`SourceBatch.items/cursor_ref` 覆盖 `ProbeResult{items, next_cursor, changed}`；新增 `describe()`（返回 `{source_id, kind, account_ref, config_digest, capabilities}` 供账本/文档用）。
- [ ] `SourceRegistry.register` 接受并保存 describe 元数据；`requires_explicit_selection` 沿用现有机制。

**W4.1 接入与注册（D6：程序化注册，不改配置语法）**
- [ ] 新 connector 作为公开 SDK 类导出（`proactive_sdk.sources.RssSource` / `SitemapDiffSource` / `PageHashSource`），构造时强制校验：仅 https、allowlist 必填、URL 合法、超时/大小上限有默认值——**不注册就不存在网络面**。
- [ ] `release/runtime/app.py` 增加装配示例（从 env/JSON 读源清单，或直接代码注册）；`examples/` 放一个最小注册示例，供其他宿主 agent 照抄。
- [ ] `deploy/README.md`：安全默认 + 「部署方在 app 模块里注册源」的契约说明（不写任何具体主机路径/服务名）。
- [ ] 暂不为 `config.py` 加 `sources:` 语法（YAML 子集边界不动，护栏 #8 不变）；若后续需要「不改代码换源」，再单独排期 `sources_file` JSON 加载器。

**W4.2 connector 施工（新包 `src/proactive_sdk/sources/`）**
- [ ] `__init__.py` / `base.py`：`describe()` 约定、公共校验、URL/allowlist 规则。
- [ ] `feed.py`：从 `research.py` 抽取 RSS/Atom 公共解析（`_local_name`/`_child_text`/`_plain_text`/`_canonical_url`）；`research.NewsSearchSource` 改为委托，行为不变（`tests/test_research_source.py` 必须原样绿）。Atom 支持：`entry.id/published/updated/link[@href]`。
- [ ] `rss.py`：任意 RSS/Atom URL；域名 allowlist 必填；`fact_id/revision` 稳定（复用现有 canonical URL + 内容哈希思路）。
- [ ] `sitemap_diff.py`：取 `sitemap.xml`（含 sitemap index 可选），解析 `loc/lastmod`；首轮只建基线输出 0 条（D7）；cursor 存 canonical JSON（设上限，超限退化）；只输出新增/变更条目。
- [ ] `page_hash.py`（可单独排期）：`EgressBroker` 取页面 → stdlib `html.parser` 去 `script/style/nav` → 正文归一化（空白/链接/时间）→ shingle 哈希（建议 64-bit simhash，Hamming 阈值默认 3）→ 超阈值才 `changed`，避免广告位抖动；首轮建基线。
- [ ] 三个 connector 的 `fact_id`/`revision`/`content`/`sensitivity="public"` 规则统一并写测试。

**W4.3 安全与成本**
- [ ] 每 connector 构造时强制 `EgressBroker(allowed_hosts=..., max_bytes=2*1024*1024)`；连接/总超时预算单独配置（探测超时预算写进 ledger，供 W5 成本核对）。
- [ ] 保持「拒绝一切重定向」（比计划更严），写进文档；不要让 page_hash/sitemap 为了跳转放宽。
- [ ] `sitemap_diff`/`page_hash` 零模型调用，只有 `changed` 之后才起 run（与 W5 联动）。

**验收**
- [ ] 注册路径演练：宿主 app 风格注册后 coordinator 能读到（fake connector + registry 断言）；未注册时没有任何网络面/连接尝试。
- [ ] 三类 connector 各有本地 fixture 单测，**不联网**（`tests/fixtures/sources/`）。
- [ ] 离线 e2e：本地起一个静态站点服务（按仓库现有 `EgressBroker` 仅 https 的边界，用 **loopback TLS** fixture；复用 `tests/test_net.py` 的自签 CA + IP pinning 做法），验证「首轮无变化 → 次轮改一个文件 → 恰好一条候选」。
- [ ] 安全四拒：内网地址、超 allowlist、超大响应、重定向跨域，四条都必须被拒且错误明确。
- [ ] 真实网络只做手工验证，不进 CI（命令写进 `IMPLEMENTATION-v2.md`）。

**坑**
- 别顺手重构 `research.NewsSearchSource` 的行为（它的 cursor/去重语义有完整回归测试）。
- sitemap/page_hash **不能**每次全量当 changed，否则上线即误报；首轮必须建基线。
- cursor 是 `TEXT`，没有长度限制，别把整站 URL 表无限塞进去；设上限并测试超限行为。
- `net.EgressBroker` 的 `allow_loopback` 只给测试用，绝不能在 release 装配里打开。
- 计划说「sources/ 抽成协议」「配置里加源定义」；现实已有协议、且配置语法不支持列表。按 D6：复用协议 + 程序化注册，配置化留作可选后续——不在 B4 里临时扩语法。

---

### W5 两段判定：便宜的在前 — B3

**目标**：先探测、后分析；零候选 tick 不烧模型；task 模式不再每槽必跑（可关），账本能核成本。

**现实锚点**
- 所有模式都已执行 fetch/变更探测（`coordinator._process` L0 段），heartbeat 用 `changed` 做门，task 不用。
- task 无源 = 纯推理，必须继续跑（现有注释与行为）。
- 无 job 的 hook/手动 wake 在 `_task_of` 里被当作 `mode='task'`；带 job 的 `pas jobs trigger` 返回 `job.mode`，但事件 `origin='manual'`——**用户显式要求跑，不能被变更门吞**。
- `SourceBatch` 已带 cursor/observed_at；`SnapshotMaterializer` 目前对整批 materialize。

**改动面**
1. `CoordinatorConfig`：`max_candidates: int = 10`、`always_run` 不走 config（见 3）。
2. `coordinator._process`：
   - fetch 后构造候选集：仅 `changed` 的 `(entry, batch)`，条目去重 key = `(source_id, fact_id, revision)`；按新鲜度（`observed_at`/顺序）排序，截断到 `max_candidates`，记 `truncated`；
   - 门：`mode == "task"` 且 `entries` 非空且 `not always_run` 且候选为空 → `_suppress("no_source_change")`（silence_reason 走 W3）；
   - **豁免**：`entries` 为空（纯推理 task）必须跑；`origin == "manual"`（手动触发/用户 wake）必须跑；`mode == "heartbeat"` 保持现状；
   - materialize 只对入选候选（构造过滤 batch 或新增过滤接口；`source_state` 的 cursor/watermark 仍按全量 batch 更新）。
3. `always_run`（D8：放 job 级）：`schemas/v1/job_spec.json` 的 `task.properties.always_run`（boolean）；`contracts.JobSpec` 校验；`service.py` 的 `jobs create` 加 `--always-run`；`tools/gen_client_ts.py` 重生成；job 创建/更新测试。
4. 计数：m013 `ALTER TABLE runs ADD COLUMN candidates_count INTEGER/truncated_count INTEGER`；`record_run_decision`/suppressed 路径写入；model_calls 用现有 `usage.model_turns`（suppressed=0）。
5. 读面：`pas runs list/show --json`、`pas activity`、`runs.get` 带计数；daemon metrics 汇总。

**验收**
- [ ] 假模型计数器：零候选 tick 的模型调用 = 0；一候选 = 1；`always_run=true` 与旧行为一致（回归）。
- [ ] 纯推理 task（无源）仍然跑（回归，必须有）。
- [ ] 手动触发（origin=manual）在有源无变化时仍然跑（回归，必须有）。
- [ ] 账本能读出 `model_calls`、`candidates`、`truncated`。
- [ ] 连续 24 个 tick 的 FakeClock 模拟里，空转调用数不超过配置预期。
- [ ] 候选去重单测：同 fact 不同 revision 只留新鲜的一条；超上限截断计数正确。

**坑**
- 这是**行为语义变更**（task 默认不再每槽必跑）：`always_run` 默认 false 会让存量 job 变安静。发布说明 + `IMPLEMENTATION-v2.md` 必须写清；`docs/COMPATIBILITY.md`/README 该改的改。
- 手动触发与无源 task 的豁免是计划未提的两处已知正确行为，漏掉就是回退。
- 门在 fetch 之后才有候选，探测本身的网络成本照付；把 probe 耗时记进账本（计划风险条），并给探测独立超时预算。
- 截断只影响进模型的上下文，不影响 cursor 更新（否则会重复探测已确认的旧内容）。

---

### W6 打扰后悔回路（feedback）— B7

**目标**：账本不只记投递状态，还能记用户反应；能按 reason/source 出接受率报表；阈值仍由人改，不自动调。

**现实锚点（重要：不是从零开始，但要语义分开）**
- 已有的是**偏好反馈**：`feedback` 表（m001）、`store.record_feedback`、`FeedbackManager`、RPC `notifications.feedback`（冻结方法集内）、CLI `pas notifications feedback`，kind = `handled/not_useful/mute_topic/unmute_topic/wrong_target`。**这套不动**。
- 插件 `examples/pas_hermes_plugin/` 只有 5 个工具、无 feedback 写入；`tests/test_pas_plugin.py` 精确断言名单——天然满足「模型不能写自己成绩单」。
- 「回复情况」只有宿主知道；PAS 只能判「24h 内无任何反馈」。

**改动面（D5：新增 `feedback.record` + 新表 `delivery_feedback`，旧路径不动）**
1. m014 新建 `delivery_feedback` 表：`feedback_id`、`message_id REFERENCES outbox(message_id)`、`signal CHECK IN ('replied','ignored_24h','dismissed','complained')`、`source CHECK IN ('user','host')`、`authenticated_actor`、`created_at_ms`，并对 `(message_id, signal)` 做幂等唯一约束。**另需同步 `store.export_profile_data` 与 `wipe_profile_data` 的显式表清单**（否则导出/删除会静默漏新表）。
2. RPC 新增 `feedback.record{message_id, signal, at}`：`rpc.py PROACTIVE_RPC_METHODS`、`rpc_server` handler、`packages/client-ts/src/rpc.ts` 方法表 + typed helper、`tests/test_client_ts_drift.py`、README/docs。加法扩展：`system.hello` 的方法清单自动包含，协议版本保持 1.0 并在文档注明「只增不删」。**绝不进插件/模型工具面**。
3. `ignored_24h` 由宿主侧定时任务回填（读受控通道回复情况），仓库内提供 API：`store.backfill_ignored_24h(now_ms)` = inbox 已投递 >24h 且无 `replied/dismissed/complained` → 写 `signal=ignored_24h, source='host'`；`daemon._cycle` 低频调用；开关默认关（D5b）。宿主「是否回复」通过 `feedback.record` 记 `replied`，不由 PAS 猜。
4. `pas stats --since 7d`（新 CLI 子命令，`service.py`）：
   - 报表 1：每 reason 的 acceptance 率（分母=已投递 inbox；分子=replied；弱信号 ignored_24h 单列，不并入分母）；数据来源：`delivery_feedback.message_id → inbox → run_id + fact_id + revision → run_proposals.reason`；
   - 报表 2：每 source 的产出率/无效比（需 W5 的 candidates/truncated 计数）；
   - 无数据 → 空表、退出码 0；`--json` 可用。
5. 边界断言：插件工具名单不变；内置模型工具（`builtin_tools` 3 个）不含 feedback 写入；RPC 方法集 diff 只允许新增 `feedback.record` 一处。

**验收**
- [ ] 聚合逻辑单测：构造 10 条样本，acceptance 率精确到小数（不四舍五入拍脑袋）。
- [ ] `pas stats --since 7d` 能出表；无数据不报错。
- [ ] 插件工具列表断言仍为 5 个且无 feedback 写入（保留既有 `test_pas_plugin` 断言，可加注释指向本条）。
- [ ] `ignored_24h` 回填：24h 内不写、无反馈 24h 后写、已有 `replied/dismissed/complained` 不写；重复调用幂等。
- [ ] `feedback.record` 只在 trusted control plane 可达：插件工具面与模型工具面都调不到；`source='host'` 只能由内部回填写。
- [ ] `delivery_feedback` 表已进 export/wipe 清单：`export_profile_data` 有该表、`wipe_profile_data` 删该表后 foreign_key_check 通过。

**坑**
- 别把「没回」当「不喜欢」：报表里 `ignored_24h` 必须是弱信号、单独列，并加权说明；不要拿它单独调阈值。
- 「不自动调阈值」是硬要求：报表只给建议值，代码里不要出现自动改配置。
- 不要复用旧的 `feedback` 表/`notifications.feedback`：那是偏好反馈（mute/handled），和投递结果混在一起会让第三方宿主无法区分语义。
- RPC 方法集是冻结的：加法扩展可以，但必须在 PR 描述里写明「新增 `feedback.record`，未删改任何已有方法」，并同步 TS 方法表与 drift 测试。
- 与 W1 的耦合：没有 reason 就没有「每 reason 接受率」，所以 B7 必须在 B1 之后。

---

### W7 宿主执行器（等前六项跑出数据再开）— 单独立项

**前置门（缺一不开）**
- [ ] W1–W5 完成并合并；
- [ ] 至少有 7 天账本数据可对比；
- [ ] 以下三项有**本机实测数字**（不引用他人环境）：宿主侧资源上限对照（进程/容器内存峰值）、每轮 token、单轮时延。计划提到评估环境常驻约 20 MB RSS，仅作量级参考，不作达标线。

**仓库内改动面**
1. `src/proactive_sdk/hermes.py` / 对应宿主适配器：让宿主 Runs API 使用该 profile 的会话与工具面。**具体路径与字段以宿主 API 契约为准，由适配层实现，内核不硬编码**（计划 §0 边界 + 护栏 #11）；实现前先对齐宿主 API，并把适配层行为写成可离线跑的 adapter 测试。
2. `release/runtime/app.py`：`PAS_EXECUTOR=hermes`（或对应适配器）接上 profile 作用域；回滚值写清（D9：`model` 即计划里的 `builtin`，仓库现状没有 `builtin` 字面值——要么改名，要么在文档里显式映射）。
3. 账本：`host_session_id` 目前没有独立列；先确认宿主响应里是否真有会话标识——有则加 `runs.host_session_id`（m015）或写进 `host_handle_json`（已有 `executor_id/host_run_id`）；没有就不虚构字段，报告如实说明替代字段。
4. headless 标记：请求里带标记（字段名以宿主 API 为准），测试断言标记确实发送；**长期记忆侧按 source 过滤**由宿主/记忆侧实现，单独立项，不在内核硬编码。
5. 计量：usage 已有 token/turns/elapsed；内存/资源峰值由部署方采集并标环境，不写成假数。

**验收**
- [ ] 一次真实主动分析由宿主 agent 本体产出，账本有 `run_id` 与 `host_session_id`（或如实说明替代字段）。
- [ ] 有本机实测数字：内存峰值、token/轮、时延（标环境）。
- [ ] 长期记忆条目数不因 headless 轮次增加（记忆侧前后各取一次计数）。
- [ ] `PAS_EXECUTOR=model`（计划里的 `builtin`）切回后行为与 v2 一致（回滚演练一次）。
- [ ] 若资源峰值逼近宿主上限：默认不开，只留开关——写进 `COMPATIBILITY` 与部署文档。

---

## 5. 施工批次、依赖与检查点

> 计量单位是「批次」，不是人日。每批 = 一个 PR；批内连续完成，批间只有依赖顺序，不存在「派人并行」。每批结束必须能回答：测试门是什么、动了哪些文件、怎么回滚。

| 批次 | 内容 | 进入条件 | 关键文件 | 检查点（本批结束必须绿） | 改动面 |
|---|---|---|---|---|---|
| **B0** | 分支/环境/基线/决策点 | 结论拍板 | 无代码 | py3.12 基线存档；D1–D10 有结论 | 无 |
| **B1** | W1 reason + W3 silence_reason（同一次契约变更 + 同一个 m012 迁移） | B0 | `schemas/v1/decision.json`、`contracts.py`、`decision_parse.py`、`decision_contract.py`、`executor.py`、`host_bridge.py`、`coordinator.py`、`store.py`、m012、`config.py`、`facade.py`、`gen_client_ts`、tests | 契约五处一致 + 迁移幂等 + `runs`/`inbox`/`activity` 可读 reason/silence_reason | 大（契约 + 迁移 + 双执行器） |
| **B2** | W2 activity gate | B1（用 W3 的 reason 通道） | `coordinator.py`、`facade.py`、`config.py`、`release/runtime/app.py`、deploy README、tests | probe 三态测试 + reminder/manual 两条回归 | 中 |
| **B3** | W5 两段判定（含 W4.0 协议接口先落） | B1 | `coordinator.py`、`contracts.py`、`schemas/v1/job_spec.json`、`service.py`、m013、tests | 零候选 = 0 模型调用；无源 task / manual 两条回归；计数可读 | 中 |
| **B4** | W4a 协议 `describe()` + SDK 注册 + `rss` connector | B3、D6 已定（程序化注册） | `contracts.py`、`context.py`、`src/proactive_sdk/sources/`、`release/runtime/app.py`、`examples/`、deploy README、tests | 注册演练 + rss fixture 单测 + 安全四拒 | 中（无配置语法改动） |
| **B5** | W4b `sitemap_diff` | B4 | `sources/sitemap_diff.py`、fixtures、tests | 本地静态站点 e2e：首轮 0 条、次轮恰好 1 条 | 小 |
| **B6** | W4c `page_hash` + 安全加固/离线 e2e | B4 | `sources/page_hash.py`、`sources/feed.py`、fixtures、loopback TLS e2e、tests | 阈值抖动测试 + 离线 e2e + broker 安全回归 | 中 |
| **B7** | W6 投递反馈（`feedback.record` + `delivery_feedback`）+ `pas stats` | B1（reason 聚合）、B3（candidates 计数） | `store.py`、m014、`rpc.py`、`rpc_server.py`、`packages/client-ts/`、`gen_client_ts`、`service.py`、`daemon.py`、tests | 10 样本聚合精确 + stats 空数据不报错 + 方法集/TS 漂移绿 + 工具面防回退 | 中 |
| **W7** | 宿主执行器（立项） | W1–W5 完成 + 7 天数据 + 本机实测数字 + 宿主侧配合 | `hermes.py`、`release/runtime/app.py`、m015、docs | 前置门全过才开工；仓库内只做适配器与记账 | 立项（跨仓库） |

依赖图（顺序执行，不是人力并行）：

```
B0 ──> B1 ──┬──> B2
            ├──> B3 ──> B4 ──┬──> B5
            │                └──> B6
            └──> B7
W7：W1–W5 完成 + 7 天数据 + 三项数字 → 立项
```

**执行与停点规则**
- 同一时间只做一批；B2/B3、B5/B6/B7 之间没有「并行人力」，只是可以先做 A 再做 B。
- 任一批结束都是安全停点：仓库保持绿、旧迁移零改动、本批改动可单独回滚；未完成批次不提交半成品。
- 每批交付三样：测试门命令与输出、改动文件清单、回滚方法（直接进 `IMPLEMENTATION-v2.md` 与最终报告）。
- 需要人拍板的阻塞点只出现在 B0（D1–D10）与 W7 前置门；其余批次不应停下来问。

---

## 6. 验收命令速查（每批 PR 的收尾都要跑）

```bash
# 0) 基线对照（P0-3 保存的文件）
python3.12 -m unittest discover -s tests 2>&1 | tee /tmp/after.txt
#  对比：新增失败只能是本 PR 预期的用例；既有环境失败集合不变

# 1) 单模块（tests/ 没有 __init__.py，不能用 tests.test_x 模块路径）
python3.12 -m unittest discover -s tests -p "test_decision_contract.py" -v
python3.12 -m unittest discover -s tests -p "test_schemas.py" -v
python3.12 -m unittest discover -s tests -p "test_store.py" -v

# 2) 契约/客户端漂移
python3.12 tools/gen_client_ts.py && git diff --exit-code packages/client-ts/src/schema_types.ts

# 3) 迁移（仿 test_migration_v011 的旧库升级思路）
python3.12 -m unittest discover -s tests -p "test_migration_v012.py" -v

# 4) 插件/工具面
python3.12 -m unittest discover -s tests -p "test_pas_plugin.py" -v
python3.12 -m unittest discover -s tests -p "test_tool_authority.py" -v

# 5) 发布门禁（动 release/发布前）
python3 tools/license_gate.py && python3 tools/package_gate.py && python3 tools/build_release.py

# 6) 脱生产 e2e（local_inbox，绝不给 webhook sink）
#    throwaway state_dir + owner_destination="local-inbox:<x>"；runonce job 的 at 设 1 秒后，
#    sleep 过点再 tick，否则 admitted 为空；失败原因看 RunReport.reason（runs 表只有 error_class）
```

**计划验收条目的落地口径**（计划原文 ↔ 实际做法）：

| 计划写法 | 实际做法 |
|---|---|
| `proactive_ledger view=sent` | `pas runs show <id> --json` / `pas notifications list --json`（字段扩展后） |
| 「构造三种不同的短路原因，在账本里各自可读」 | FakeClock 集成测试构造三种原因（不要求真实隔天）；需要脚本时 `tools/` 下新写（已删除的 `validate_*.py` 不恢复） |
| 「真实主动分析 / 真 daemon」 | 用 `ProactiveAgent` + `tick(max_runs=1)` 的集成测试；需要真进程时手工跑 `pas serve --app ...` 写进 IMPLEMENTATION |
| 「本地起一个静态站点服务」 | **loopback TLS** fixture 站点（仓库 `EgressBroker` 只允许 https；复用 `tests/test_net.py` 自签 CA 做法） |
| 「真实网络只手动验证」 | 命令写进 `IMPLEMENTATION-v2.md`；CI 不跑 |
| 「插件工具列表断言」 | 复用 `tests/test_pas_plugin.py` 现有精确名单断言 |

---

## 7. 全局护栏落地检查表（每条都要能在 PR 里机械检查）

| # | 护栏 | 检查方法 |
|---|---|---|
| 1 | 不加第三方依赖 | `pyproject.toml` deps 保持空；新文件 import 审查；`python3 tools/package_gate.py` |
| 2 | Python 3.11/3.12 兼容；`@tool` 用 `Optional[X]` | 两个版本各跑全量（`@tool` 注册在 3.12 遇到 `X | None` 会直接抛错）；新增 `@tool` 参数只用 `Optional[X]`，review 时人工过一遍 |
| 3 | 迁移只增不改、幂等、版本校验 | `git diff --stat src/proactive_sdk/migrations/` 旧文件 0 改动；新迁移跑两次幂等测试；`MIGRATION_COUNT` 同步 |
| 4 | 可信入口不放宽 | 模型工具面：`builtin_tools` 仍 3 个；插件工具面：`test_pas_plugin` 名单不变；RPC 方法集 diff 只能是本 PR 声明的变化（且 `grants.create` 仍不在） |
| 5 | 证据闭包不放松 | `tests/test_decision_contract.py` 的 `evidence_closure` 规则 + 新增 reason 回归；W4 的源内容仍只是 DATA |
| 6 | `delivery_unknown` 不自动重试 | 不动 `delivery.py`；PR 里 diff 检查 |
| 7 | 外发默认关 | 所有测试用 `local_inbox`；e2e 脚本断言无 webhook sink；首条真实外发单独批准；`release/runtime/app.py` 默认不配 sink |
| 8 | 配置仍是 YAML 子集 | `pas config check/print` 往返；新 key 加进 `_POLICY_KEYS` 校验；块序列仍报错；W4 按 D6 不新增 `sources` 段 |
| 9 | `notification_window` 是静默窗口 | B2 diff 不该出现 `windows.py` / `quiet_hours` 语义变化 |
| 10 | `ExecStart` 顺序 `pas --config X serve --app Y` | 不动 `deploy/systemd/pas.service`；部署文档只加 env |
| 11 | 不交只在特定主机成立的实现；不把具体部署配置写进仓库 | 代码里不出现硬编码的宿主路径/服务名/profile/DB 位置/凭据（这类字符串只允许出现在测试 fixture 或文档示例）；新增配置项必须在 `config.py` 校验并在 `deploy/README.md` 说明「部署方要填什么」；PR 描述单列一节「环境假设」 |

---

## 8. 完成定义与交付物（每个 PR / 全部完成）

**每个 PR 的 DoD**
- [ ] 本 PR 的验收命令全绿，且全量测试只包含基线环境失败；
- [ ] `git diff` 只含本 PR 声明的文件；旧迁移 0 改动；
- [ ] 契约变更五处同步：Python 类型/校验、`schemas/v1/`、TS 生成物、prompt/契约文本、测试夹具；
- [ ] 使用 `always_run` / `idle_gate` / `require_reason` / `sources` 等新开关的默认值写进 README 或 IMPLEMENTATION；
- [ ] 回滚方法：单行 env/配置、迁移不删列不重写旧行、代码上开关默认兼容；
- [ ] 更新 `IMPLEMENTATION-v2.md` 对应节（改动面 / 验证命令与真实输出 / 未验项 / 回滚）。

**整体交付物（计划 §8）**
1. 独立分支，按 §5 的 7 个批次推进；每批自包含、可单独回滚、可在批次边界安全停下。
2. 新增测试并保持既有测试不全红；环境类失败在报告里标注为环境产物，**不得改动测试来「修绿」**；不把某个环境的测试数字写成硬指标。
3. `IMPLEMENTATION-v2.md`（位置见 D10）：每项改动面、验证命令与真实输出、未验项、回滚方法。
4. `docs/COMPATIBILITY.md` 按实情更新：没验过的保持 ❌，不要顺手改 ✅。

**最终汇报三件事**：做了什么；证据是什么（命令 + 输出）；哪些没做以及原因。不要只报「已完成」。

---

## 9. 今天就能开始的前 8 步

1. `git fetch && git switch -c feat/pas-v2 origin/main`，确认 HEAD `b875f88`。
2. 建 py3.11 / py3.12 venv，跑基线 `unittest discover`，存 `01/v2-baseline-3xx.txt`（本机数字只作对照，不追平计划评估环境）。
3. 通读计划 §2 表格 + 本清单 §2（计划↔代码对照）。
4. 记录 D1–D10 结论（D5 = 新增 `feedback.record`；D6 = 程序化注册；D10 = 非决策；其余按清单默认值），写进 B1 描述。
5. B1 第一步：契约五处一次改齐 + `gen_client_ts` + 夹具，先让 `test_decision_contract` / `test_schemas` / `test_client_ts_drift` 绿。
6. B1 第二步：解析/执行器/host bridge + 灰度开关 + `tests/test_v2_reason.py`。
7. B1 第三步：m012（reason + silence_reason）+ `store.record_run_decision`/`record_run_suppressed` + 读面 + 迁移幂等测试。
8. B1 收尾：W3 的枚举映射与 `job_activity` 投影 + 三种短路原因的 drill 测试 → 提 PR；然后开 B2/B3。

---

## 附：本版修订记录（跟随计划 v2 修订，2026-10-18 第二版）

1. **跟随计划新增的 §0 边界**：删除本清单中所有宿主特定信息（宿主会话/数据库路径/查询语句/资源上限数字/长期记忆产品名/profile 路径等），W2 的 probe 与 W7 的宿主执行器改为「适配层契约」描述，实现留给部署方；代码不得硬编码部署路径与凭据。
2. **基线口径**：计划已撤回固定失败数，改为「不同环境会有差异，开工第一步自己跑基线，以你跑出来的结果为准」。P0-3 改为记录**本机基线**（本机预跑：851 tests / 29 errors，环境类）并与计划评估环境（Linux / py3.12）数字解耦，不追平、不改测试。
3. **W3 验收**：从「三天 drill」改为计划原话「构造三种不同的短路原因，在账本里各自可读」（FakeClock 集成即可）。
4. **W4 离线 e2e**：从计划旧写的「http.server」改为「本地起一个静态站点服务」；按仓库现有 `EgressBroker` 仅 https 的边界，用 loopback TLS fixture。
5. **W7**：计量与前置门改为通用口径（宿主侧进程/容器资源上限、每轮 token、内存峰值、单轮时延）；删除具体资源数值与长期记忆产品专名，「headless 不进长期记忆」的过滤明确为宿主/记忆侧任务。
6. **新增护栏 #11**：不交只在特定主机成立的实现、不把具体部署配置写进仓库；配套 P0-4 的环境边界检查与自查项。
7. **顺带修正一个正确性问题**：W2 的在场门必须豁免 `origin='manual'` 的 wake（否则 `input.note` 刚写入的活动时间会让用户自己要求的 run 被门吞掉），已加入目标、步骤、验收与坑。
8. **计量单位修正**：原稿沿用计划 PR 拆分给出的「人日」估时，但本清单由 agent 执行，人日属于人类排期单位、无意义；已改为「施工批次 B0–B7 + 检查点 + 停点」口径，并删除所有「不同人并行」的建议（agent 只有顺序执行）。
9. **按 plugin-first 定位重新定夺 D5/D6/D10**：D6 改为 **SDK 程序化注册**（不扩配置语法、不加第二份配置文件）；D5 改为**新增 `feedback.record` + 新表 `delivery_feedback`**（旧偏好反馈路径不动）；D10 降级为非决策（reference 文档，施工+验证通过即可）。B4 改动面因此从「大（含配置格式设计）」降为「中」，B7 增加 RPC 方法集/TS 漂移同步与 export/wipe 表清单同步。
