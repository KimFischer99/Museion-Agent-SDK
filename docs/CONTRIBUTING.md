# 贡献说明（CONTRIBUTING）

## 前提阅读

`docs/SEMANTIC_MATRIX.md`（任务语义）、`docs/SECURITY.md`（权限边界）、
`schemas/README.md`（公共契约）、`docs/COMPATIBILITY.md`（实测支持范围）。**不要**把 skip、mock、TODO 或
接口桩描述成已完成能力。

## 施工约定

- 后续工作按兼容矩阵中的缺口推进；区分脚本化契约测试与真实服务联调。
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
python tools/license_gate.py                    # 许可门禁
python tools/gen_client_ts.py                   # client-ts 幂等
npm exec --yes --package=typescript@5.8.3 -- tsc -p packages/client-ts/tsconfig.json
python -m pip wheel . -w dist --no-deps         # 构建产物
python tools/package_gate.py dist/*.whl         # 产物扫描（非 git 扫描）
python tools/gen_sbom.py dist/*.whl -o dist/sbom.cdx.json
python tools/install_smoke.py dist              # 全新 venv 安装 smoke
```

`tests/fixtures/skills.json` 保留 88 入口的兼容性回归基线；
`tests/fixtures/selected-source-manifest.json` 用于防止私有原文件混入产物。
两者是开发与构建数据，不参与 SDK runtime。若本机保留完整私有原附件，
可另行运行 `python tools/reproduce_audit.py` 重现原始审计；它不属于部署前置条件。

## 提交信息

一行主题（阶段/工单 + 概要），正文列出：改变契约、兼容性影响、测试
证据、已知缺口。禁止把长期 TODO 默认为已完成。

## 分发红线

- `01/`（含 `private-vendor/` 的 Muse 原文件、私有 helper 副本、凭据、环境文件与 `muse-refer/`）
  只存本地：不进 git、不进包、不进容器、不上传公开渠道。
- `_deployment_reference/skills/` 是明确附带的部署参考文件；保持来源清单和文件 hash 一致，不将其接入默认 runtime 或自动执行。
- 不用他处同名 Muse SDK 的许可证覆盖本项目约束。
- 容器/生成包做来源/许可/隐私扫描时针对产物本体（package_gate），
  不能只扫 git tracked files。
