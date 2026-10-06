# Proactive Personal Agent SDK — 施工仓库

自用施工仓库（2026-10-06 整理）。目标：按 `SPEC.md` 的 P0–P7 阶段实现独立的主动 Agent 内核（工作名 `proactive-agent-sdk`，简称 PAS）。本仓库目前是设计交接包 + 施工现场，不是已完工的 SDK。

## 阅读顺序

1. `SPEC.md`：总体设计、接口、状态机、施工阶段、验收与来源。
2. `AUDIT_AND_REUSE.md`：Muse 附件事实、88 个 Skill 的兼容缺口、可复用代码与授权边界。
3. `AGENTS.md`：交接约束。
4. `VALIDATION.md`：实际测试范围，区分 mock 与真实集成。

## 仓库结构

```text
SPEC.md / AUDIT_AND_REUSE.md / AGENTS.md / VALIDATION.md
examples/          参考代码切片与 schema 起点
tests/             可执行参考测试
audit/             88 个 Skill 的元数据审计与来源 hash（不含 Skill 正文）
reuse/             受限的私有提取与测试脚本
private-vendor/    本地私有素材，不进入 git（见下）
```

`private-vendor/`（git 不跟踪，保留在本地）：

- `muse-sdk/` — 原始 Muse 附件解包，作为审计依据与 P6 Skill 导入的参照语料。保持原样、只读，不改动其中文件。
- `muse-reuse/` — 从原附件逐字节提取的 `hatch_hook_runtime.sh`（SHA-256 与使用说明见其 `README_PRIVATE.md`）。

## 快速验证

```bash
python -m unittest discover -s tests -v
python examples/reference_core.py
```

TypeScript 检查方法见 VALIDATION.md。

## 分发与许可

项目当前为自用，原始素材仅存在于本地 `private-vendor/`。若日后转为公开发布：恢复 AGENTS.md 的分发约束，执行 SPEC P0/P7 门禁（license gate、SBOM、隐私扫描），且不得直接公开 Muse 原文件。

工作名 `proactive-agent-sdk`、Python 包名和 CLI 名字都是设计占位名称，不代表已经存在或可安装的官方产品。
