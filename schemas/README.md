# JSON Schemas（PAS Interoperability Profile v1）

本目录是 SPEC 第 4 节统一契约的 JSON Schema 2020-12 定义。它们是**契约冻结的载体**：
Python / TypeScript 实现与 fixtures 从这里派生，schema 变更须同步三处（见 AGENTS.md）。

## 约定

- 所有跨进程对象必须携带 `protocol_version`（`^\d+\.\d+$`）。当前为 `1.0`。
- 外部时间一律 RFC 3339，带 `Z` 或显式 offset；内部毫秒整数字段以 `_ms` 结尾。
- 变更类请求使用 `idempotency_key`；同 key 不同规范化内容返回 `conflict`。
- 规范化 JSON：键排序、紧凑分隔符（见 `proactive_sdk.contracts.canonical_json`），
  hash 一律 SHA-256 小写十六进制。

## 校验器支持范围

`proactive_sdk.schema_validate` 实现本仓库 schema 实际使用的 2020-12 关键字子集：
`type`（含 `["x","null"]`）、`properties`、`required`、`additionalProperties:false`、
`enum`、`const`、`items`、`minItems`/`maxItems`、`minLength`/`maxLength`、
`minimum`/`maximum`、`pattern`、`format: date-time`、`$defs`/内部 `$ref`。
标注关键字（`title`/`description`/`$id`/`$schema`）忽略。

**跨字段规则不在 schema 内表达**，由 `contracts.py` 的语义校验实现并有独立测试：

| 规则 | 位置 |
|---|---|
| `Schedule` 按 kind 的必填字段（interval⇒anchor+every_seconds；daily/monthly⇒local_time+timezone；weekly⇒weekdays+local_time+timezone；runonce⇒at） | `contracts.validate_schedule` |
| `Schedule.fold_policy` 仅允许 `earliest`/`latest`（默认 `earliest`；DST 回拨重复时刻取哪一次，春令时跳空一律跳过。P1 起 schema 与实现同步支持） | `contracts.validate_schedule` |
| `Decision.decision == "silent"` ⇒ `proposals` 为空；`"propose"` ⇒ 至少 1 条 | `contracts.validate_decision` |
| `notify_self` 提案必须带 `evidence_refs` 与 `expires_at` | `contracts.validate_decision` |

若后续引入 `jsonschema` 库做 conformance（P7），这些规则应同步写成
`if/then` 或保留为代码层检查，两种途径的测试结果必须一致。

## 文件

| Schema | 对应 SPEC 对象 |
|---|---|
| `error.json` | §4.4 统一错误 |
| `job_spec.json` | §4.1 JobSpec（含 Schedule） |
| `wake_event.json` | §4.1 WakeEvent |
| `run_request.json` / `run_handle.json` | §4.1 RunRequest / RunHandle |
| `context_pack.json` | §4.1 + §7.1 ContextPack |
| `decision.json` | §4.1 Decision + ActionProposal |
| `action_record.json` | §4.1 ActionRecord |
| `delivery_attempt.json` | §4.1 DeliveryAttempt（含 outbox 状态机 §10.2） |
| `usage.json` | §4.1 Usage |

示例值见 `tests/test_schemas.py`，与 SPEC §7.1 / §8.1 的示例保持一致（假数据）。
