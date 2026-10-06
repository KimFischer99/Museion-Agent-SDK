# 许可证清单（License Inventory）

<!-- license-gate
status: pass
# 自用项目：原创代码 MIT；Muse 素材仅存于本地 private-vendor/（git 不跟踪）。
# 若转为公开发布：重新评估本清单；任何 unknown-permission 条目未解决前，
# 将本标记改为 blocked，tools/license_gate.py 将阻断 release（SPEC §17.3）。
-->

本文件是 P0 许可清单与 release 门禁的数据源。`tools/license_gate.py` 读取上方
`<!-- license-gate -->` 标记与审计 hash，实施"未知许可阻断 release"。

## 1. 原创代码 — MIT

`LICENSE`（MIT, Copyright (c) 2026 Kim Fischer）覆盖以下本仓库原创内容：

- `src/proactive_sdk/` — contracts、pathsafe、schema_validate
- `schemas/` — 交互契约 schema
- `tools/` — 审计复现与许可门禁工具
- `examples/`、`tests/` — 交接包参考切片与测试（同为原创；边界声明见 VALIDATION.md）
- `docs/` — 本清单与进度记录
- `audit/*.json`、`audit/*.log` — 机器可读**元数据**（名称、hash、issue 标志），
  由用户提供的附件派生；不含 Skill 正文

## 2. 用户提供的 Muse 素材 — 未再分发（LOCAL ONLY）

| 对象 | 位置 | 状态 |
|---|---|---|
| Muse 附件解包（88 Skill、runtime 脚本、平台文档） | `private-vendor/muse-sdk/` | git 不跟踪；仅本地参照 |
| `hatch_hook_runtime.sh` 原样副本 | `private-vendor/muse-reuse/` | SHA-256 `c87af221…58e741`；未发现再分发许可 |

决定记录（2026-10-06）：用户确认本项目为**自用**，来源授权由用户负责；
素材不进入 git、不进入分发包、不上传公开渠道（AGENTS.md 约束）。
`tools/license_gate.py` 以路径前缀 + 全量 hash 比对双重检查此约束。

## 3. 第三方运行时依赖

首版核心仅依赖 Python 3.11+ 标准库（含 zoneinfo）。测试同样仅用标准库。
`pyproject.toml` 的 `schema` extra（jsonschema）是可选的 conformance 工具依赖，
不是运行时依赖；引入任何第三方依赖前必须先在本清单登记并锁定版本。

## 4. 阻断规则

- 本标记 `status:` 为 `blocked` ⇒ release 阻断。
- 发现被跟踪文件与审计 hash（skills.json / selected-source-manifest.json /
  helper hash）匹配 ⇒ release 阻断。
- 新增内容若含未知许可来源 ⇒ 先登记并解决，再改回 `pass`。
