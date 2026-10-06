# 施工进度（P0–P7）

按 SPEC §15.1 阶段推进；每阶段完成后在此登记，汇报格式遵循 AGENTS.md。
"完成"以该阶段验收条件全部通过为准，不以代码写完为准。

| 阶段 | 状态 | 完成日期 | 说明 |
|---|---|---|---|
| P0 边界与来源 | **done** | 2026-10-06 | contracts/schema、许可清单与门禁、审计复现命令、zip-slip 防护、单 profile 边界；详见下 |
| P1 持久化与时钟 | not started | — | store、migrations、Clock、五类 schedule、misfire、claim/fencing、jobs API |
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
