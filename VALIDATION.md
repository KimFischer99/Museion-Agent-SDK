# 验证记录

日期：2026-10-06。只记录本次实际完成的检查；以下数字不代表成品 SDK 的完整测试覆盖率。

## 环境

| 工具 | 实测版本 |
|---|---|
| Python | 3.13.5 |
| Python 链接的 SQLite | 3.46.1 |
| Node.js | 22.16.0 |
| TypeScript compiler | 5.8.3 |
| 原 helper 依赖 | Bash、jq，环境中可用 |

参考 SQLite ledger 明确使用 DELETE journal；没有在此环境启用需要另外核验修复版本的 WAL。目标 Python 3.11+ 是设计范围，本次没有跑 3.11/3.12 的版本矩阵。

## 1. 原创 Python 参考代码

实际命令：

```bash
python -m unittest discover -s tests -v
python examples/reference_core.py
```

**52 项测试通过。** 其中 44 项覆盖参考核心，8 项覆盖 Hermes transport 的本地模拟契约。

覆盖严格 hook 解析、非法 JSON/输出大小/退出码、interval coalescing、静默时间窗口、重复本地时刻的投递规则、hook 状态 CAS、dry-run、失败回滚、重启读取、run lease/fencing、事务 outbox、同事实去重、通知过期、unknown 状态与晚到回执、固定本人目标、profile 拒绝混用等。

不涵盖真实多进程并发压测、断电文件系统耐久、完整 daily/weekly/monthly 发生器、生产政策服务、sandbox、真实 Google/通知 API 或 LLM。

demo 输出：

```json
{"delivered_notifications":0,"events":1,"pending_notifications":1,"runs":1}
```

没有创建虚假的“已投递”成功记录。

## 2. Pi TypeScript 示例

实际命令：

```bash
tsc --strict --target ES2022 --module commonjs --lib ES2022,DOM \
  --outDir /tmp/pas-ts-build \
  examples/pi_executor.ts tests/pi_contract_test.ts
node /tmp/pas-ts-build/tests/pi_contract_test.js
```

**TypeScript strict 编译通过；4 项结构接口检查通过。** 覆盖等待完成后读取结果并 dispose、拒绝 silent 携带提案、拒绝未允许工具、拒绝预先取消的任务。

这里使用结构接口与 mock session，**没有安装/加载真实 Pi 包**。也没有对完整 ActionProposal schema、extension 隔离或在途取消进行端到端验证。`parseDecision` 只是 envelope 校验，不能替代生产 action validator。

## 3. 附件原脚本

对 17 个原始 Python 文件逐个完成 `ast.parse`。对 21 个原始 shell 文件逐个完成 `bash -n`。全部通过。这是语法检查，不是正确性或安全认证。

只对指定 SHA-256 的 `hatch_hook_runtime.sh` 执行了受控功能测试：

```bash
python reuse/test_original_helper.py /private/path/hatch_hook_runtime.sh -v
```

**8 项测试通过。** 分别为 silent、wake/disable、stderr log、初始状态、状态 roundtrip、dry-run 不写状态、非法 state 类型和非法 payload 拒绝。

每次使用临时目录、显式环境变量与子 Bash 进程。没有执行附件中的 home-init、tripwire、日志裁剪、凭据客户端或任何实际连接器。

## 4. 数据库设计与提取工具

- `examples/schema.sql` 在内存 SQLite 中成功建立 **18 张表**。
- 这仅验证 DDL 可执行；没有实现完整迁移、所有状态 CHECK 约束、schema 回滚或生产 DAO。
- `reuse/extract_original.py` 对原 ZIP/hash 验证通过，并成功提取 byte-identical helper 到新私有目录。
- 原 helper SHA-256：`c87af221181a1559e3adcb0cdd601f5be5d4bd91eec2fe0597c09dc27558e741`。

## 5. 明确未做

没有对真实 Hermes gateway、真实 Pi SDK、Muse 后台、邮箱、日历、设备、push 服务或付费模型执行联调；没有发布、安装或提交到用户的仓库；没有验证全部 88 个 Skill 的实际功能；没有完成第三方代码再分发授权核验。

生产版本的完成标准以 SPEC P0–P7 和第 16 节为准。测试桩通过不能用来抹掉上述缺口。
