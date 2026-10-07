# Museion Agent SDK v0.1.0 — 施工仓库

自用施工仓库（2026-10-06 整理）。产品名与版本为 **Museion Agent SDK v0.1.0**；Python distribution/import 名称 `proactive-sdk`/`proactive_sdk`、CLI `pas` 和协议简称 PAS 暂沿用已有接口。P0–P7 已全部施工完成；本仓库是施工现场，不是已发布的成品。

**兼容层状态（如实声明）**：Skills 兼容层完成了 88 个入口的审计一致导入（scan→sidecar→install），但端到端能力验证为 **0 个**（`e2e_verified` 均为 false，需真实授权逐项联调）。Gmail/Calendar/Webhook 通道只到传输层契约测试。不要把本包描述成"88 个能力全部实现"。详见 `docs/COMPATIBILITY.md` 与 `VALIDATION.md`。

## 阅读顺序

1. `SPEC.md`：总体设计、接口、状态机、施工阶段、验收与来源。
2. `AUDIT_AND_REUSE.md`：Muse 附件事实、88 个 Skill 的兼容缺口、可复用代码与授权边界。
3. `AGENTS.md`：交接约束。
4. `VALIDATION.md`：实际测试范围，区分 mock 与真实集成。
5. `docs/COMPATIBILITY.md` + `compatibility-lock.json`：锁定版本与兼容矩阵。
6. `deploy/README.md`：systemd / container / launchd 部署与备份操作。

## 仓库结构

```text
SPEC.md / AUDIT_AND_REUSE.md / AGENTS.md / VALIDATION.md
compatibility-lock.json
src/proactive_sdk/   契约与内核：contracts / pathsafe / schema_validate（P0）；
                     clock / store / scheduler / migrations（P1）；hooks（P2）；
                     model / tools / context / executor / coordinator（P3）；
                     policy / delivery（P4）；hermes / pi_worker / rpc（P5）；
                     skills / gws / net / connectors（P6）；
                     config / observability / backup / facade / daemon /
                     rpc_server / service（P7：配置、日志指标、备份、
                     公共 facade、daemon、控制面、CLI）
packages/client-ts/  JSON-RPC 控制面的 TypeScript 客户端（从 schemas 生成）
schemas/v1/          JSON Schema 2020-12 契约（10 个对象）+ 说明
examples/            参考切片与 demo：reference_core / agent_loop_demo /
                     policy_delivery_demo / skills_loop_demo / pas_app（serve 示例）
deploy/              systemd unit、Containerfile、compose、launchd 示例
tests/               标准库单元/契约测试（P0–P7）
audit/               88 个 Skill 的元数据审计与来源 hash（不含 Skill 正文）
reuse/               受限的私有提取与测试脚本
tools/               reproduce_audit / license_gate / validate_p5_real /
                     gen_client_ts / package_gate / gen_sbom / install_smoke
docs/                LICENSES / PROGRESS / COMPATIBILITY / SECURITY / CONTRIBUTING
private-vendor/      本地私有素材，不进入 git（见下）
muse-refer/          本地参考文档，不进入 git；不随 wheel 分发
OPTIMIZATION_PLAN.md 本地 Ling 对照施工计划，不进入 git
```

`private-vendor/`（git 不跟踪，保留在本地）：

- `muse-sdk/` — 原始 Muse 附件解包，作为审计依据与 P6 Skill 导入的参照语料。保持原样、只读，不改动其中文件。
- `muse-reuse/` — 从原附件逐字节提取的 `hatch_hook_runtime.sh`（SHA-256 与使用说明见其 `README_PRIVATE.md`）。

## 安装与快速开始

```bash
# 从源码构建 wheel（无第三方运行时依赖）
python -m pip wheel . -w dist --no-deps
python -m pip install dist/proactive_sdk-*.whl

pas --help            # CLI 总览（serve / doctor / jobs / runs / backup …）
pas version
```

嵌入模式（自己拥有事件循环，SPEC §14.1）：

```python
from proactive_sdk import ProactiveAgent, Job

agent = ProactiveAgent(state_dir="./pas-state", executor=my_executor,
                       timezone="Europe/Berlin", locale="zh-CN", profile="personal")
grant = agent.create_grant_from_user_consent(          # 只能由可信 UI 调用
    capability="calendar.read", account_ref="account:primary",
    scope={"resource_ids": ["work"]}, consent_evidence_ref="consent:doc-1")
agent.jobs_upsert(Job(id="daily", mode="task",
                      schedule={"kind": "daily", "local_time": "09:00",
                                "timezone": "Europe/Berlin"},
                      instruction="总结今日日程，只通知本人。",
                      grant_refs=(grant.grant_id,)),
                  idempotency_key="setup-daily-v1")
await agent.tick()      # 或 await agent.serve() 前台常驻
```

daemon 模式（CLI 拥有事件循环，`--app` 指向装配模块，见 `examples/pas_app.py`）：

```bash
pas serve --app app.wiring:build_agent --config /etc/pas/pas.yaml
pas doctor --deep       # 环境/锁/socket/配置检查
pas status              # jobs/runs/outbox/health 摘要
```

## 快速验证

```bash
python -m unittest discover -s tests -v
python examples/reference_core.py
python examples/agent_loop_demo.py
python examples/policy_delivery_demo.py
python examples/skills_loop_demo.py
python tools/reproduce_audit.py
python tools/license_gate.py
```

TypeScript / client-ts / 安装 smoke / SBOM 的检查方法见 VALIDATION.md 与
`tools/`（`install_smoke.py`、`package_gate.py`、`gen_sbom.py`）。

## 数据安全（备份 / 恢复 / 删除）

```bash
pas backup /var/backups/pas-$(date +%F).bin          # 在线备份（0600 + sha256 sidecar）
pas restore /var/backups/pas-2026-10-07.bin --yes    # 校验 identity/schema 后原子替换
pas export dump.json                                 # 逻辑导出（JSON）
pas delete-data --yes                                # 清空 profile 数据（不可逆）
```

## 分发与许可

项目当前为自用，原始素材仅存在于本地 `private-vendor/`，构建产物由
`tools/package_gate.py` 对 wheel 本体做路径/hash/密钥扫描（不只扫 git
tracked files）。若日后转为公开发布：恢复 AGENTS.md 的分发约束，执行
SPEC P0/P7 门禁（license gate、SBOM、隐私扫描），且不得直接公开 Muse
原文件；第三方代码再分发授权核验仍是一项未完成的前置义务。

Museion Agent SDK v0.1.0 是本仓库的自用版本标识，不代表已在 PyPI/npm 发布或已完成真实用户流程回归。
