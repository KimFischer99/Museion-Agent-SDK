# 许可证清单（License Inventory）

<!-- license-gate
status: pass
# 原创代码 MIT；用户明确要求将选定 Skills 作为被动部署参考随包附带。
# pass 表示内容边界与来源清单检查通过，不表示已核验第三方再分发许可。
-->

本文件是 P0 许可清单与 release 门禁的数据源。`tools/license_gate.py` 读取上方
`<!-- license-gate -->` 标记与审计 hash，核对禁止路径和明确登记的参考文件。
门禁通过与第三方许可核验是两种状态；随包参考的来源许可尚未整体核验。

## 1. 原创代码 — MIT

`LICENSE`（MIT, Copyright (c) 2026 Kim Fischer）覆盖以下本仓库原创内容：

- `src/proactive_sdk/` 的原创实现；不包含 `_deployment_reference/skills/` 中的第三方参考文件
- `schemas/` — 交互契约 schema
- `tools/` — 审计复现与许可门禁工具
- `examples/`、`tests/` — 原创参考切片与测试
- `docs/` — 本清单与进度记录
- `tests/fixtures/*.json` — 机器可读**元数据**（名称、hash、issue 标志），
  由用户提供的附件派生；不含 Skill 正文

## 2. 用户提供的 Muse 素材与随包参考

| 对象 | 位置 | 状态 |
|---|---|---|
| 选定 Skills（88 入口、377 个源文件） | 源文件：`src/proactive_sdk/_deployment_reference/skills/`；交付：`release/skills/` | 独立于运行 wheel，平铺为被动部署参考；随目录提供 `NOTICE.md` 和 `manifest.json`；原来源许可状态保留 |
| Muse 附件解包（88 Skill、runtime 脚本、平台文档） | `01/private-vendor/muse-sdk/` | git 不跟踪；仅本地参照 |
| `hatch_hook_runtime.sh` 原样副本 | `01/private-vendor/muse-reuse/` | SHA-256 `c87af221…58e741`；未发现再分发许可 |
| 本地参考文档与缓存 | `01/`（含 `muse-refer/`） | git 不跟踪；不随包分发 |

当前交付约定（2026-10-07）：运行交付位于 `release/runtime/`，包含 SDK wheel、装配 `app.py` 与安装说明；wheel 不含 Skills 或 Skills 的 `NOTICE.md`。Skills 作为独立目录交付于 `release/skills/`，与完整私有快照及内部施工文档分开。
`release/skills/manifest.json` 记录来源、文件数量、一个目录总校验值与许可核验状态，不保存逐文件哈希清单；`release/skills/NOTICE.md` 明确参考文件不受项目 MIT 许可覆盖。
`tools/license_gate.py` 与 `tools/package_gate.py` 保留来源边界、hash 与敏感文件门禁；不得将其他原始文件混入运行或 Skills 交付物。

## 3. 第三方运行时依赖

首版核心仅依赖 Python 3.11+ 标准库（含 zoneinfo）。测试同样仅用标准库。
`pyproject.toml` 的 `schema` extra（jsonschema）是可选的 conformance 工具依赖，
不是运行时依赖；引入任何第三方依赖前必须先在本清单登记并锁定版本。

## 4. 阻断规则

- 本标记 `status:` 为 `blocked` ⇒ release 阻断。
- 发现被跟踪文件与审计 hash 匹配，但不在指定的部署参考路径/清单中 ⇒ release 阻断。
- 部署参考缺失、改变或新增未登记文件 ⇒ release 阻断；实际私钥块仍由产物扫描阻断。
- 附带参考不赋予新的使用或再分发权利；原有文件级许可和来源说明必须保留。
