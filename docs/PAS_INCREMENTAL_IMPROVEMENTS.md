# PAS 七项增量改进：实施交接方案

- 状态：**方案待实施**。本 PR 只增加本文，不修改运行时代码、协议、数据库或版本号。
- 日期：2026-11-02（America/Los_Angeles）。
- 核实基线：v0.1.6，`9a564e6254e804686106fa1bd25043c3f7cc907d`；核实时工作区干净。
- 目标：将七项讨论转成可独立验收的小改动，复用当前调度、证据、授权、上下文和投递机制。
- 来源：dsh-superagent-prototype 的 [观察策略](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/policy.ts)、[人工介入状态](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/stage.ts)、[领域模型](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/001-domain.md)。借鉴行为设计，不引入其运行时或复制源码。

## 1. 相对 baseline 的效果

| 编号 | 改进 | 已有基础与真实缺口 | 完成后的可观察变化 |
|---|---|---|---|
| 1 | 通知观察模式 | 已有 policy verdict 和 outbox；没有完整决策路径的观察模式 | 机会型任务可以只记录“本来会通知什么”，不真正发送；明确提醒和按约报告照常履约 |
| 2 | 统一人工待办 | 审批、activity、投递状态已存在；目前没有逐事项汇总 | 一处看到所有当前需要处理的问题及处理入口 |
| 3 | 任务授权溯源 | 已有 revision/grant_refs；缺少可查询的请求来源及完整版本历史 | 新建/修改任务可关联可信请求、任务版本和授权；旧任务诚实显示来源未记录 |
| 4 | 通知表达更明确 | worker 内部记录与 compose 已分离；表达规则可以补强 | 少发进度播报；结果明确，需要决定时提出具体问题 |
| 5 | 利用待发送上下文 | pending 摘要已实现，但与默认关闭的会话开关耦合 | 没有接入聊天记录也能考虑排队通知，减少重复提案 |
| 6 | 异常提示降噪 | heartbeat 故障通常只记账，hook 已有退避；状态页可能把单次故障突出为待处理 | 偶发机会型故障留诊断，连续故障升级待办；按约任务失败仍立即可见 |
| 7 | 待办及时消退 | 现有记录能反映部分解决状态；缺逐事项恢复投影和过期审批过滤 | 同一问题解决后退出待办，历史保留；无关任务成功不会误清除问题 |

七项不是七套新子系统。2、6、7 共用一个只读状态投影；4、5 补强现有机制；1 是新增策略行为；3 涉及新增持久化。

## 2. 全局边界

实施前阅读 [CONTRIBUTING](CONTRIBUTING.md)、[SECURITY](SECURITY.md)、[契约说明](../schemas/README.md) 和 [兼容性记录](COMPATIBILITY.md)。

- 保留 reminder 零模型路径、due 按约交付、heartbeat 无候选零模型路径。
- 保留本轮证据闭包、可信控制入口、grant 撤销、冻结参数审批、lease fence 和投递前复验。
- `delivery_unknown` 不能因时间流逝、另一任务成功或新的通知而自动视作解决，也不能盲目重发。
- 模型输出、网页、笔记、worker 报告和上下文摘要均不能创建授权或选择新接收方。
- 不新增 Thread/Ticket/Subscription、多 worker 调度、常驻会话、通用事件总线或连接器。
- 不增加运行时依赖。公共契约若确实变化，Python、schema、生成 TypeScript 和 fixture 一起更新。
- 不改写历史 migration；需要持久化的新行为用追加 migration。默认值及旧数据读取保持兼容。
- 不把提示词建议描述成确定性保证，不把离线替身描述成真实模型或生产验收。
- 本文不授权生产部署、合并或发布；后续执行者按其任务获得的授权操作。

## 3. 各项实施范围

### 1）通知观察模式

**基线证据**

- [policy.py](../src/proactive_sdk/policy.py)：`PolicyEngine.apply_to_run` 评估提案后调用 `record_policy_verdicts`、`queue_run_actions`。
- [store.py](../src/proactive_sdk/store.py)：已有 `run_proposals.policy_json`；空 action entries 可让 run 正常完成，无需新建 run 状态机。
- [executor.py](../src/proactive_sdk/executor.py) 与 [host_bridge.py](../src/proactive_sdk/host_bridge.py)：已有 `tool_authority` 和 broker 能力声明。提示词不是宿主工具安全边界。
- hook 的 `dry_run` 仅覆盖 hook，不是模型决策与投递观察模式。

**最小实现**

增加一个默认关闭的可信配置项（建议 `policy.observe_notifications`），仅影响 job 绑定的机会型后台任务；manual 请求、reminder、due 报告保持原行为。观察与正常模式使用相同的证据、授权、去重及时间窗口检查，记录拟发送、拟延后或被抑制的结果；观察提案不创建 action、outbox 或审批记录，不占实际投递额度或业务去重键。

首版限定在 PAS 能约束的只读研究执行器；在调用模型前拒绝工具权限不可控的观察任务，不是等产生结果后才拒绝。允许内部记录和通知提案；扩大任务或产生外部动作的提案不得通过观察路径创建 watch、审批或其他执行记录。复用现有能力契约，不仅凭模型或提示词自报“只读”。

观察结果保存在现有 proposal/verdict/账本，并提供一个有界查询入口，能查看内容、reason、evidence_refs 和拟执行结果，明确标为“未投递”。不伪造通知反馈，也不把 run completed 展示为已发送。

这是通知观察，不是整个系统的无写入模拟：来源游标、分析记录以及原本明确启用的本地记忆行为仍按现有规则运行。关闭观察后只影响新的分析，不自动补发历史观察结果；开启观察也不自动撤回此前已经排队的通知。

**代码及调用范围**

- 必改：`config.py` 配置白名单/校验/序列化；`policy.py` 策略分支；`coordinator.py` 执行前边界与结果记录；`facade.py` 配置装配及查询。
- 直接调用方：`examples/release_app.py` 自建 PolicyConfig 的路径；`launcher.py` 的 settings 校验、保存和 PasConfig 构造，保证用户确实能启用该功能。
- 展示入口：`service.py` 提供有界观察结果查询，优先复用现有 run/activity 查询；不为本项另建管理面板。
- 检查 `run_stages.py` 的输入 hash 是否涵盖新增有效配置；不得复用另一模式下的 checkpoint。
- 预计无需数据库迁移；如复用 verdict JSON，保持既有顶层 run/decision 枚举不变。

**验收效果**

同一合法机会型通知，在正常模式进入 outbox，在观察模式可查询但不进入 outbox；观察不会使之后的真实新决策因虚假的已发送记录而被去重。不可控宿主在模型执行前被拒绝。明确提醒、due 报告及既有撤权/unknown 边界保持不变。

### 2）统一人工待办

**基线证据**

[launcher.py](../src/proactive_sdk/launcher.py) 的 `readiness` 读取最近 activity，但主要判断全局最新一条，另检查未知投递总数；[facade.py](../src/proactive_sdk/facade.py) 的 `status` 主要返回计数；审批已有独立查询。当前不是多事项待办视图。

**最小实现**

在 facade 下提供一个有界、只读的当前问题投影，由 store 对审批、job activity 和 outbox 做查询。每项返回稳定对象引用、类别、发生时间、简短原因和处理入口。优先显示有效待审批、需核实的未知投递、当前任务故障；普通静默、静默窗口延后和正常等待不列为人工问题。

按 job/阶段、approval_id、message_id 区分事项，不仅对全局最近五条记录做过滤。查询要有上限并说明截断，不能因为其他任务繁忙就让未解决问题消失。原始 health/统计保留，待办不是运行时健康状态的替代品。

`pas status` 和发布入口 `app.py status/check` 使用同一个投影。首版不增加新的 RPC 方法；如后续宿主确实需要，再同步 RPC 方法表和 TS 客户端。尚未落库的自然语言澄清不在此首版范围内。

**代码及调用范围**

`store.py` 新增当前问题查询；`facade.py:status` 聚合；`launcher.py:readiness` 消费。检查 `service.py:_cmd_status` 的输出兼容性，保留原字段、增加待办字段。不增加待办表或新的后台循环。

**验收效果**

不同任务的审批、失败和未知投递可以同时显示，各自有定位与处理方式；成功的任务不会遮住另一个未解决的问题。诊断列表与用户通知分开，不因状态查询而发送消息。

### 3）任务授权溯源

**基线证据**

[proactive.py](../src/proactive_sdk/proactive.py) 的 `_scheduled_input` 将 actor/request_id 或文本用于构造哈希 job ID，但没有保存可反查的请求来源；[store.py](../src/proactive_sdk/store.py) 的 `upsert_job` 更新当前 job，幂等表也不能还原完整请求和历史任务版本。现有 grant_refs 展示不等于来源溯源。

**最小实现**

增加按 `(job_id, revision)` 关联的追加式来源记录，与创建/修改 job 在同一事务提交。记录可信入口、经过认证的 actor、request_id 或宿主引用、输入摘要 hash、创建时间以及该版本的任务定义和 grant 引用。授权仍以现有 grant 和执行时复验为准，历史记录不得恢复已撤销权限。

不默认保存聊天原文，不承诺仅靠 hash 还原请求。CLI、SDK 等无宿主请求引用的入口如实记录入口种类和来源缺失；actor 只能来自可信会话/调用边界，不能相信模型字段或请求体自报身份。旧任务展示“来源未记录”，不从当前状态推测历史。

**代码及调用范围**

- `proactive.py`：保留原始可信请求与标准化候选之间的关联。
- `facade.py` 和 `store.py`：所有创建/修改路径共享持久化边界；添加来源记录及查询，追加 migration。
- `rpc_server.py`、`service.py`、`launcher.py`：透传各自实际掌握的可信来源；检查程序化 SDK、自动 research job、watch resolve 等直接写调用，允许诚实标注 system/unspecified，不能漏记或伪造用户请求。
- `service.py` 的 job 详情展示来源与版本；需要新跨进程字段时同步相关契约和 TS，不默认把 provenance 做成模型可写的 JobSpec 字段。

**验收效果**

新任务及新 revision 能定位其可信来源，重放同一幂等请求不重复记账，失败的 job 写入不留下虚假来源记录；旧库升级后旧任务仍可读且来源明确缺失。

本项单独实施。仅增加当前 grant 展示不能标记本项完成。

### 4）通知表达更明确

**基线证据**

[run_stages.py](../src/proactive_sdk/run_stages.py) 的 `worker_report` 已拒绝非 internal_record 提案；[coordinator.py](../src/proactive_sdk/coordinator.py) 的 compose 再决定通知。已有 `ready_for_decision` reason，但没有独立“问题—回答—关闭”实体。handoff 默认关闭，不应为了表达优化强制所有任务多调用一次模型。

**最小实现与范围**

调整 [decision_contract.py](../src/proactive_sdk/decision_contract.py) 的公共提示和 coordinator 的阶段提示，覆盖普通单阶段与 handoff 两条路径：过程进展留内部记录；通知突出有依据的结果；需要用户决定时提出一个明确问题，并用现有 reason 表达原因。保持现有证据与输出校验。

不新增问题类型、状态机、回复路由或 UI 表单，不修改模型工具授权。若公共提示的既有同步测试受影响，更新相应 fixture，不能只改某个宿主的私有提示。

**验收效果**

两条执行路径都收到一致表达规则，内部 worker 仍不能直接发通知。离线测试只能验证合同与通路；真实模型是否更简洁需要单独样本验收，不能承诺绝不播报进度。

### 5）利用待发送上下文

**基线证据**

[coordinator.py](../src/proactive_sdk/coordinator.py) 的 pending 摘要读取跟随 `conversation_context`；[store.py](../src/proactive_sdk/store.py) 的 `pending_notification_summary` 已限制最多 20 条，只有 digest、主题、状态和时间等信息；[context.py](../src/proactive_sdk/context.py) 已渲染 pending 与 recent sent。精确业务键去重已由 policy/store 实现。

**最小实现与范围**

在 coordinator 中将有界 pending 摘要与会话开关解耦，无需另加开关；在 context 的数据说明和公共提示中明确参考已有排队消息，只有存在新增且值得打扰的信息才另提机会型通知。不读取完整通知正文或引入跨用户数据。

复用现有字段、20 条预算和持久化投影，保留 queued/unknown 不等于收到或读过的语义。检查 context schema/fixture 兼容，但不为已有字段再建协议。

**验收效果**

关闭会话上下文时，pending 摘要仍能进入普通执行和 compose；不开启额外聊天读取。due 报告不因主题相同而取消，新 revision/新事实仍可发送。这不是改写或合并 outbox，也不保证语义级完全去重。

### 6）异常提示降噪

**基线证据**

coordinator 对 heartbeat 全源失败通常只记录 suppressed；due 研究失败由 `_research_failure` 形成可审计系统反馈。hook 已有连续错误计数与退避。缺口主要在状态展示的升级判断，不是普遍存在逐次故障推送。

**最小实现与范围**

并入第 2 项的查询投影，首版按 job 统计已落账的机会型分析失败，不做跨任务的 source 健康系统。以连续三次可重试来源故障作为首版升级门槛；首次失败仍保留诊断和 activity。成功检查打断该连续序列；没有真正检查来源的 quiet/idle 抑制不算恢复，也不算新的来源失败。

授权缺失等必须人工处理的问题直接进入待办，不等三次。due 任务本轮失败立即可见，原有失败报告不受此门槛影响。正常成功、未变化但确实检查成功、跳过未检查要根据真实执行证据区分，不能把所有 suppressed 当失败或恢复。

代码主要位于 `store.py` 查询及 facade/launcher 投影。若既有 activity/run_events 无法可靠判断某次检查结果，只补 coordinator 的最小结构化记账；不解析自由文本来推测成功。不改 hook 重试、provider 重试、delivery 重试或用户通知数量。

**验收效果**

一次机会型临时故障不会升级人工待办，连续三次才升级，同一 job 一次真实恢复可清除连续失败；其他 job 的成功不影响它。按约失败与撤权仍即时可见。

### 7）待办及时消退

**基线证据**

`ApprovalManager.pending` 调用 store 的状态筛选；`list_approvals` 本身不按当前时间过滤过期项。当前最新 activity 视图也不是逐事项的解决状态。outbox 已有可用的当前投递状态。

**最小实现与范围**

与第 2 项在同一查询投影中实现：有效待审批排除过期、已解决及失效授权；过期审批若使任务仍无法交付，应转换成明确的任务受阻提示，不能把整个问题静默隐藏。同一 job/阶段真实恢复后移出对应故障待办，历史记录不删除。

投递 unknown 只随已有权威核实或明确终态退出“待核实”；若核实未送达后仍需处理，按真实后继状态显示。不能用新通知、时间过去或一次模型成功代替投递证据。首版不新增任意 dismiss/归档接口，以免多建一套待办持久化。

代码复用 `store.py`、`facade.py`、`launcher.py`，必要时修正 approval pending 的查询语义及直接调用方；只读状态查询不修改历史记录。

**验收效果**

处理审批、真实恢复或投递核实后，重复读取 status 得到一致的新待办列表；旧故障仍能从 activity 查到；无关成功与未核实 unknown 不被误清除。

## 4. 建议实施顺序与交接

| 阶段 | 内容与所有权 | 停止条件 |
|---|---|---|
| A | 2＋6＋7：store 查询、facade status、launcher 展示及直接测试 | 多事项当前状态与恢复判定可验证；无法从账本判断的检查结果先补最小记账，不猜测 |
| B | 5＋4：coordinator/context/decision_contract 及现有契约测试 | 无会话接入也有 pending 摘要，两条执行路径规则一致，不改变 due 交付 |
| C | 1：config/policy/coordinator、装配、launcher 配置和观察查询 | 观察路径无新外部投递/审批，执行前权限可确认，关闭后无历史自动补发 |
| D | 3：可信任务写入口、来源迁移与查询展示 | 新版本来源可追踪、幂等和旧库兼容；没有真实原始来源时明确留空 |

阶段顺序是依赖与范围安排，不是额外验证轮次或新增测试配额。默认一个 agent 顺序实施；需要拆任务时明确文件所有权，尤其避免多人同时修改 store/coordinator/facade。后续 agent 从当前分支内容和实际 HEAD 开始，先记录差异，不能把本文基线当作永远不变。

任务清单：

- [ ] A：人工待办、故障升级与解决消退。
- [ ] B：pending 解耦与通知表达规则。
- [ ] C：受限通知观察模式及可用设置/查询入口。
- [ ] D：完整新任务版本来源记录。
- [ ] 汇总实际改动、检查结果与仍未验证的真实模型/宿主路径。
- [ ] 只有获得发布任务后，才调整版本并执行发布/部署。

## 5. 验证与完成定义

### 已有证据

本次源码核实时运行以下既有测试，共 **36 项通过**：`test_launcher`、`test_revision_016`、`test_a5_followup`、`test_v3_switch_off`、`test_policy.DedupFeedbackQuotaTests`，以及审批过期和撤权取消待审批各一项。它们证明 baseline 的相关行为，不证明本文新增能力已经完成。

### 后续实施的最小验证

先复用既有用例覆盖 reminder/due、去重、审批冻结/过期/撤权、上下文预算、默认开关和协议同步。新增用例围绕实际增量选取，不为实现细节造测试：

1. 观察通知可查但无 action/outbox/审批；正常投递记录不被污染。
2. 不受控宿主在观察任务执行前被拒绝。
3. 多任务待办查询及故障升级、同事项恢复的生命周期。
4. 关闭会话读取时 pending 仍进入上下文，保留其预算与状态语义。
5. 任务来源随创建/版本修改原子保存，重复请求幂等，旧任务不伪造历史。

以上是优先验收主题，不是允许把任意数量输入塞进五个测试函数。每个参数化/生成输入都计入案例预算；整次实施任务新增最多五个行为案例，阶段/agent 拆分不重置额度。优先复用既有测试；确有未满足的必要验收且预算不足，说明具体缺口后申请一次有界扩展，不省略边界或暗增用例。

实现后最小相关检查一次，相关修正后最多一次定向重跑；未变更的通过结果复用。准备提交时执行 CONTRIBUTING 明列的提交门，不将其重复执行成额外审计循环。超时、缺环境和 skip 如实记录，不能当作通过。

完成需同时满足：用户可用入口和直接调用方接通；对应验收有实际证据；默认/关闭语义明确；已有投递与授权边界保留；公开契约变化同步；数据库迁移可验证；最终 diff 仅包含已授权范围。

真实模型是否减少重复、表达是否更清晰，不由脚本替身推断。真实外部模型/渠道、生产升级和部署按后续任务授权单独验收。

## 6. 本 PR 交付范围

仅交付七项实施方案和上述验收定义。没有引入新功能、依赖、数据库迁移或版本升级，也未执行生产操作。本文合并或关闭均不代表七项功能已实现。该文档 PR 的实际提交门结果记录在 PR 描述中，由接手 agent 与后续实现结果分开引用。
