# 兼容矩阵（P7 / SPEC §17.3.3）

机器可读版本：仓库根目录 `compatibility-lock.json`。本文件是人读摘要；
两者不一致时以 lock 文件 + VALIDATION.md 的实测证据为准。

## 支持范围（v0.1 边界）

| 维度 | 支持 | 说明 |
|---|---|---|
| profile | 单 profile | 一个 state 目录、一个 DB、一个 owner destination（不是多租户产品） |
| 主机 | 单主机 | 控制面默认 Unix socket；远程 TLS HTTP 未实现，属部署方自行方案 |
| Python | ≥ 3.11 | 实测 3.14.4（macOS）；3.11/3.12 版本矩阵未跑，如实标注 |
| 运行时依赖 | 仅标准库 | 可选 extra：`jsonschema`（conformance 工具用，不影响运行） |

## 外部宿主（锁定版本，真实服务验证）

| 宿主 | 锁定版本 | 验证方式 | 结果 |
|---|---|---|---|
| Hermes Runs gateway | 0.21.5+8493.g9b38eb1 (2026.9.24) | P5 真实服务探针 1–5（capabilities / submit / idempotency replay / cancel / key conflict） | 5/5 PASS（见 VALIDATION §5e） |
| @earendil-works/pi-coding-agent | 1.0.4 | P5 真实服务探针 6–8（initialize / run envelope / cancel 语义） | 3/3 PASS |

宿主版本升级规则：升级前必须重跑 `tools/validate_p5_real.py` 的探针集，
通过后才更新 `compatibility-lock.json` 的锁定版本。协议大版本不匹配时
控制面 `system.hello` 直接拒绝（fail closed）。

## 连接器与通知通道

| 组件 | 状态 | `e2e_verified` |
|---|---|---|
| Gmail（GWS 受限 grammar） | 传输层契约测试（脚本替身 + 真实适配器代码路径） | ❌ |
| Google Calendar（同上） | 传输层契约测试 | ❌ |
| Webhook 通知 sink | loopback HTTP 契约（幂等键 / unknown 对账 / ACK 丢失不盲发） | ❌（未绑定外部 provider） |
| 公开资料跟踪 | loopback TLS（自签 CA + IP 钉扎）验证传输与门禁 | ❌ |

无授权凭据，e2e 一列保持 ❌——README 与此同步，不冒充已联调。

## Skills 兼容层

- 审计：88 个入口，`tools/reproduce_audit.py` 可复现。
- 导入管线：scan → sidecar → install 与审计一致（P6，88/88 字段级一致）。
- 端到端能力验证：**0 个**。`technical_status` 到 `parsed` 为止；
  `e2e_verified` 需要真实授权与逐项联调，缺口透明记录，不宣称
  "88 个能力全部实现"。

## 运维矩阵

| 平台 | 状态 |
|---|---|
| macOS（darwin 27 arm64） | 全量测试绿；sandbox-exec 可用（Apple 已弃用，探测失败即 fail closed） |
| Linux（Ubuntu 24.04） | P5 真实服务验证所在主机；Bubblewrap argv/探测已测，hook 端到端未在该机复跑全量 |

## 数据与升级规则

- store 迁移只增不改；旧二进制拒绝打开新库；备份带 `schema_version`，
  restore 遇到"备份比二进制新"即拒绝。
- 控制面协议（`PAS_PROTOCOL_VERSION=1.0`）大版本变更会破坏 client-ts
  客户端——hello 阶段协商失败即停。
- 本文外部文档变化后先跑测试再更新矩阵（SPEC §17.3.6）。
