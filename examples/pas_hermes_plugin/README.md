# PAS proactive plugin for Hermes

SPEC §12.1 方式 B：已有宿主（Hermes）使用 PAS 主动能力。插件通过 Hermes
官方注册入口（`register(ctx)` → `ctx.register_tool`）暴露五个工具；所有
操作经 JSON-RPC 2.0 落到 PAS 控制面。插件不保存任何计划、授予或审批
状态。

| 工具 | PAS 方法 | 说明 |
|---|---|---|
| `proactive_schedule` | `jobs.create` | 提议一个持久化计划；PAS 受理 ≠ 已通知 |
| `proactive_status` | `jobs.list` | 读计划与 next due |
| `proactive_pause` / `proactive_resume` | `jobs.pause` / `jobs.resume` | 暂停/恢复 |
| `proactive_skills_inspect` | `skills.explain` | 只读兼容状态；import/audit 不对模型暴露 |

## 配置

环境变量（profile 作用域，随 profile 的 `.env` 注入）：

- `PAS_RPC_URL` — PAS 控制面 URL；仅接受 HTTPS 或字面回环 HTTP。
- `PAS_RPC_TOKEN` — bearer token；不传给模型、不写入会话记录。

未配置时工具调用返回明确的 `ok:false`（fail closed），插件加载不受影响。

## 边界（务必保留）

- 本插件不暴露 `grants.create` / `approvals.resolve`（§14.3：仅限可信用户 UI）。
- 模型无法通过本插件批准任何动作或改写 grant。
- 若宿主切换 scheduler ownership，必须显式停止 PAS 同任务定时器（SPEC §3）。

## 安装与验证

- 契约测试：`python3 -m unittest tests.test_pas_plugin`（用 stub ctx 与脚本化
  PAS RPC 服务器，无网络、无模型调用）。
- Hermes 官方入口校验：`hermes plugins validate examples/pas_hermes_plugin`。
- 安装到运行中的 Hermes 属操作者决定（需要 PAS daemon 在线，属 P7）。
