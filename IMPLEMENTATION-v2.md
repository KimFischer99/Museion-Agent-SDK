# PAS 主动层 v2 更新详情

- 分支：`feat/pas-v2`（基线 `b875f88`，即 `origin/main`）
- 规模：58 files changed, 6530 insertions(+), 130 deletions(-)；新增 109 个测试
- 验证环境：macOS，Python 3.14.4（3.11/3.12 未跑，见「未验项」）
- 依据文档：《Museion PAS 主动层 v2 改进计划.md》《Museion PAS 主动层 v2 施工清单.md》（同分支已提交）

---

## 1. 一句话结论

PAS 从「有骨架的主动运行时」推进到 v2：决策有结构化理由、静默有语义、有用户在场门、两段判定省模型调用、来源从单一 Bing News 扩展到 RSS/sitemap/页面变更、投递结果有反馈回路与 `pas stats` 报表。既有的证据闭包、投递状态机、可信入口边界未放宽。

## 2. 提交清单

| # | commit | 内容 |
|---|---|---|
| 1 | `55c781a` | W1+W3：决策 reason / 静默 silence_reason / 迁移 012 |
| 2 | `76b88ed` | W2：用户在场门（idle gate） |
| 3 | `03b41e6` | W5：两段判定（probe → candidates → run）/ 迁移 013 |
| 4 | `0b58f96` | W4a：`SourceConnector` 协议 + SDK 程序化注册 + `RssSource` |
| 5 | `99ac5ef` | W4b：`SitemapDiffSource` |
| 6 | `cb0bab1` | W4c：`PageHashSource` + 安全/离线 e2e |
| 7 | `7d5e6ed` | W6：`feedback.record` + `delivery_feedback` + `pas stats` / 迁移 014 |
| 8 | `808623f` | 文档 + TS client 同步（COMPATIBILITY / README） |
| 9 | `4a4a3cb` | 评审决定落地（5 MiB 上限、`application/xhtml+xml`） |
| 10 | `59629f5` | 计划与施工清单入库（reference） |
| 11 | 本文档 | 更新详情 |

每批自包含、可单独 revert。

## 3. 变更详情

### 3.1 W1 决策结构化理由（reason）

- `ActionProposal.reason`：枚举 `time_sensitive | changed | requested | ready_for_decision | new_opportunity`；`notify_self` / `draft` 必填。
- 缺失或非法 → 非法决策：走现有「最多修复一次」再 fail closed；**不默认填值**。
- 灰度开关 `policy.require_reason`（默认 true）：关闭时缺失记 `unspecified`，非法值仍拒。
- 契约版本 `DECISION_CONTRACT_VERSION` 1.0 → 1.1；`schemas/v1/decision.json`、`decision_contract` 规则与 prompt、`packages/client-ts` 生成物同批更新。
- 落库：迁移 012 增加 `run_proposals.reason`，读面（`pas runs show`、`pas notifications list`、`runs_proposals`）可查。

### 3.2 W3 沉默的语义理由（silence_reason）

- 宿主侧枚举：`no_source_change | user_active | quota_exhausted | quiet_window | gate_error`。
- 模型侧可选字段：`moot | already_handled | unconfirmed | not_useful_now | reason_none`；未带记 `unspecified`；**未知值降级为 `unspecified` 并记 `silence_reason_unknown` run event**（全系统唯一一个「未知值不拒绝」的字段）。
- 迁移 012 增加 `runs.silence_reason`；`RunReport.silence_reason`；`job_activity` 的 analysis 行 reason 优先语义枚举、回落原始机器原因（原始 `l0_*` 字符串保持不变）。

### 3.3 W2 用户在场门（idle gate）

- `ActivityProbe` 协议：`last_user_activity_at_ms() -> int | None`；内置 `StoreUserActivityProbe` 读 PAS 自己 `events.origin='manual'`；可注入宿主 probe，两者取最新。
- 门在 coordinator L0、**任何模型调用之前**：`job 存在 && mode != reminder && origin != 'manual' && now-last < idle_min_minutes` → 抑制，`silence_reason=user_active`。
- **probe 异常/未知 fail open**：继续执行并记 `activity_probe_failed` 事件与 warning，不让宿主故障变成永久静默。
- 豁免：reminder（本就不进 coordinator）、`origin='manual'` 的用户 wake。
- 配置：`policy.idle_gate`（默认 true）、`policy.idle_min_minutes`（默认 60）。

### 3.4 W5 两段判定

- 统一路径：fetch → 候选（按 `(source_id, fact_id, revision)` 去重、`observed_at` 新鲜度排序、`policy.max_candidates` 默认 10 截断、溢出记 `truncated`）→ 有候选才起 run。
- job 驱动的 task 默认不再「每槽必跑」；保留三类旧语义：无源纯推理 task、`origin='manual'`（手动触发/用户 wake）、hook 显式信号。
- 兼容开关 `task.always_run`（job 级，schema/JobSpec/CLI `--always-run`/RPC 全链路）。
- 迁移 013：`runs.candidates_count` / `truncated_count`（旧行 NULL，不编 0）。
- 附带行为变化：源只推进 cursor 不返条目的 tick 也从「跑一次」变为「抑制」。

### 3.5 W4 来源抽象与产能

- 协议：复用既有 `Source.fetch_delta` 作为 probe 原语，新增 `SourceConnector`（+ `describe()`）；注册时 `describe()` 落入 `SourceEntry.description`。
- 新包 `src/proactive_sdk/sources/`：
  - `feed.py`：共享 RSS/Atom 解析（从原 news source 抽出，行为不变）；
  - `rss.py`：`RssSource`，任意 RSS/Atom URL；
  - `sitemap_diff.py`：`SitemapDiffSource`，无状态 diff（cursor 只带 `v1:<URL集哈希>:<lastmod watermark>`），首轮只建基线，一层 sitemapindex（子 URL 逐个 allowlist 校验）；URL/lastmod 变化时重新检查当前条目（保留 max_items 上限，可能重复检查），不承诺精确增量；删除的 URL 不输出，但删除可触发剩余条目重检；
  - `page_hash.py`：`PageHashSource`，正文抽取（去 script/style/nav/header/footer 等）→ 5-gram shingle → 64-bit simhash，Hamming 距离 > 阈值（默认 8/64）才算变化；小页面退化为精确哈希。
- 安全默认（owner 评审后）：
  - 仅 HTTPS；allowlist 必填（覆盖子域）；SSRF 防护（解析后拒私网/回环/reserved）+ IP pinning；
  - **拒绝一切重定向**；**单响应上限 5 MiB**；content-type 白名单（`text/*`、json、atom+xml、rss+xml、**xhtml+xml**、xml，`octet-stream` 仍拒）；超时 20s（叠加请求 deadline）；固定 UA；
  - **默认不扫**：新源 `requires_explicit_selection=True`，必须由 job 的 `task.refresh_source_ids` 点名；不注册则完全无网络面。
- 注册方式：**程序化注册**（不改配置语法）——`examples/custom_sources_app.py` 示例；release app 支持 `PAS_RSS_SOURCES` / `PAS_SITEMAP_SOURCES` / `PAS_PAGE_SOURCES`（JSON array，未设置=无新增网络面）；`deploy/README.md` 记录适配层契约。

### 3.6 W6 打扰后悔回路

- 新表 `delivery_feedback`（迁移 014）：`message_id → outbox`、`signal ∈ replied/ignored_24h/dismissed/complained`、`source ∈ user|host`、`(message_id, signal)` 唯一。**与既有偏好反馈 `feedback` 表分开**（mute/handled 不动）。
- RPC 新增 `feedback.record{message_id, signal}`（冻结方法集 25 → 26，纯加法；TS client 同步）；actor 来自认证会话；`ignored_24h` 不允许用户上报，只能 host 回填。
- `ignored_24h` 回填：`policy.feedback_ignored_backfill`（默认 false）；只标记「已投递超 24h 且无 replied/dismissed/complained」的消息，幂等。
- `run_proposals.source_id`（迁移 014）：决策落库时记录候选来源，供按来源统计。
- `pas stats --since 7d [--json]`：按 reason / 按 source 输出 sent / accepted / negative / weak_ignored / pending / acceptance_rate；`ignored_24h` 单列弱信号不并入接受率；空数据可出表；阈值**不自动调整**。
- export / wipe 显式表清单纳入新表（否则数据导出/删除会静默漏项）。

### 3.7 W7 宿主执行器

未开工。前置门未满足（需 W1–W5 完成 + ≥7 天账本数据 + 宿主侧内存/token/时延实测）；本仓库内无改动。

## 4. 数据迁移与兼容性

- 迁移 012：`run_proposals.reason`（旧行回填 `unspecified`）、`runs.silence_reason`（旧行 NULL）。
- 迁移 013：`runs.candidates_count` / `truncated_count`（旧行 NULL）。
- 迁移 014：`delivery_feedback` 新表 + 索引；`run_proposals.source_id`（旧行 NULL）。
- 全部只增不改、幂等；旧二进制拒绝新库；备份 `schema_version` 更高时 restore 拒绝 → 数据回滚走备份恢复，不做原地降级。
- **行为变更（发布说明必须写）**：job 驱动的 task 默认不再每槽必跑；旧行为用 `task.always_run: true` 或 `pas jobs create --always-run`。

## 5. 新增公共面

| 类别 | 新增 |
|---|---|
| Python 导出 | `REASON_ENUM`、`HOST_SILENCE_REASONS`、`MODEL_SILENCE_REASONS`、`SILENCE_UNSPECIFIED`、`ActivityProbe`、`StoreUserActivityProbe`、`SourceConnector`、`RssSource`、`SitemapDiffSource`、`PageHashSource` |
| RPC | `feedback.record`（其余方法不变） |
| CLI | `pas stats --since 7d [--json]`；`pas jobs create ... --always-run` |
| 配置键 | `policy.require_reason`、`policy.idle_gate`、`policy.idle_min_minutes`、`policy.max_candidates`、`policy.feedback_ignored_backfill` |
| 环境变量 | `PAS_RSS_SOURCES`、`PAS_SITEMAP_SOURCES`、`PAS_PAGE_SOURCES`（release app；JSON array） |
| Schema | `decision.json`（reason/silence_reason）、`job_spec.json`（task.always_run）；TS client 由 `tools/gen_client_ts.py` 重生成 |

## 6. 验证

```
$ python3 -m unittest discover -s tests
Ran 960 tests in 32.816s
FAILED (errors=28)
```

- 开工基线：`Ran 851 tests / errors=29`；本次新增 109 个测试，**0 failures**。
- 28 个 error 与开工后第一轮（B1）的错误集合**逐条相同**，全部为 hook 子进程沙箱类环境错误（本机 sandbox 后端不可用），非代码缺陷；未改动任何测试来「修绿」。
- 新增测试模块：`test_v2_decision_reasons`(19)、`test_v2_activity_gate`(14)、`test_v2_two_stage`(16)、`test_v2_sources`(14)、`test_v2_sitemap`(13)、`test_v2_page_hash`(12)、`test_v2_feedback`(17)、`test_migration_v012`(3)。
- 既有 851 测试全部保持通过（契约 drift、plugins、release app、hooks 等）。

## 7. 评审决定（已落地）

- **1A**：接受 task 两段判定默认行为变更；发布说明写明 `task.always_run` 迁移路径。
- **2A**：重定向保持全拒。
- **3.1**：响应上限 2 MiB → **5 MiB**（三个连接器；解析器预算同步提到 8 MiB 以保证传输上限有效）。
- **3.2**：content-type 补 **`application/xhtml+xml`**；`octet-stream` 继续拒绝。
- **3.3–3.6**：保持默认（allowlist 子域语义、默认不被 heartbeat 自动扫、固定 UA、20s 超时）。
- **4C**：暂不跑 py3.11/3.12，`docs/COMPATIBILITY.md` 保持「未验」口径。

## 8. 未验项与非目标

未验项：

- Python 3.11/3.12 矩阵未跑（本机只有 3.14.4）。
- 真实外网 RSS/sitemap/页面未联网验证（按计划只留手工验证，CI 不跑真实网络）。
- 宿主侧组件不在本仓库：W2 的宿主活动 probe 实现、W6 的真实回复通道、W7 宿主执行器。
- `pas stats` 的宿主回复通道端到端未验（存储/聚合/CLI 已验）。

非目标（明确不做）：机器人自己开关注；复刻 cron 版 prompt 替代品；dashboard；真实消息外发；自动调阈值；robots.txt 解析、代理、响应缓存。

## 9. 回滚

- 逐批 revert：每个 commit 自包含；迁移只加列/加表，不改旧迁移。
- 运行时灰度（不改代码）：`policy.require_reason`、`policy.idle_gate`、`policy.feedback_ignored_backfill`、per-job `task.always_run`。
- 数据层：恢复备份（旧二进制不接受新库，降级不是支持路径）。

## 10. Review 指引

```bash
git log --oneline b875f88..HEAD
git show 55c781a   # W1+W3 契约与迁移
git show 76b88ed   # W2 在场门
git show 03b41e6   # W5 两段判定
git show 0b58f96   # W4a 协议 + 注册 + rss
git show 99ac5ef   # W4b sitemap_diff
git show cb0bab1   # W4c page_hash + 安全/e2e
git show 7d5e6ed   # W6 feedback + pas stats
git diff b875f88..HEAD -- docs/COMPATIBILITY.md README.md
python3 -m unittest discover -s tests
```
