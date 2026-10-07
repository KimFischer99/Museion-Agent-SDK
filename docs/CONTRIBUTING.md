# 贡献说明（CONTRIBUTING）

## 前提阅读

`SPEC.md`（契约与验收）、`AGENTS.md`（不可妥协约束）、`VALIDATION.md`
（测试边界：mock 与真实集成的区别）。**不要**把 skip、mock、TODO 或
接口桩描述成已完成能力。

## 施工约定

- P0–P7 已完成；后续工作按工单/缺口推进（VALIDATION 各阶段"未做"清单
  是当前缺口的事实来源）。
- 每个 PR 保持可运行、可测试；公共 schema 变更须同步 Python、
  TypeScript（`packages/client-ts` 经 `tools/gen_client_ts.py` 生成）与
  fixtures。
- store 迁移只增不改；新增失败模式应新增恢复测试。
- 不在 import 时开后台线程/进程；不做隐式继承宿主凭据的实现。
- 性能数值必须标注环境；没测过就不写"已达到"。

## 本地验证（提交前全部通过）

```bash
python -m unittest discover -s tests            # 全量单元/契约测试
python examples/reference_core.py               # 参考 demo 输出不得变化
python examples/agent_loop_demo.py
python examples/policy_delivery_demo.py
python examples/skills_loop_demo.py
python tools/reproduce_audit.py                 # 88 入口审计复现
python tools/license_gate.py                    # 许可门禁
python tools/gen_client_ts.py                   # client-ts 幂等
npm exec --yes --package=typescript@5.8.3 -- tsc -p packages/client-ts/tsconfig.json
python -m pip wheel . -w dist --no-deps         # 构建产物
python tools/package_gate.py dist/*.whl         # 产物扫描（非 git 扫描）
python tools/gen_sbom.py dist/*.whl -o dist/sbom.cdx.json
python tools/install_smoke.py dist              # 全新 venv 安装 smoke
```

## 提交信息

一行主题（阶段/工单 + 概要），正文列出：改变契约、兼容性影响、测试
证据、已知缺口。禁止把长期 TODO 默认为已完成。

## 分发红线

- `private-vendor/`（Muse 原文件、私有 helper 副本、凭据、环境文件）
  只存本地：不进 git、不进包、不进容器、不上传公开渠道。
- 不用他处同名 Muse SDK 的许可证覆盖本项目约束。
- 容器/生成包做来源/许可/隐私扫描时针对产物本体（package_gate），
  不能只扫 git tracked files。
