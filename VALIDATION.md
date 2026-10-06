# 验证记录

日期：2026-10-06。只记录本次实际完成的检查；以下数字不代表成品 SDK 的完整测试覆盖率。

## 环境

| 工具 | 实测版本 |
|---|---|
| Python | 3.14.4（P1 复测时；交接时为 3.13.5） |
| Python 链接的 SQLite | 3.50.4（P1 复测时；交接时为 3.46.1） |
| Node.js | 24.14.0（交接时为 22.16.0） |
| TypeScript compiler | 5.8.3（经 npx 固定版本调用） |
| 原 helper 依赖 | Bash、jq，环境中可用 |

参考 SQLite ledger 明确使用 DELETE journal；没有在此环境启用需要另外核验修复版本的 WAL。目标 Python 3.11+ 是设计范围，本次没有跑 3.11/3.12 的版本矩阵。
P1 复测环境与交接时不同（Python/SQLite/Node 均升级）；DELETE journal 决定在新版本下仍然成立（PRAGMA 实测值见下文 P1 一节），WAL 修复核验在启用 WAL 前仍是前置条件。

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

## 5. P1 持久化与时钟（新增，2026-10-06）

实际命令：

```bash
python3 -m unittest discover -s tests -v
python3 examples/reference_core.py
npm exec --yes --package=typescript@5.8.3 -- tsc --strict --target ES2022 \
  --module commonjs --lib ES2022,DOM --outDir /tmp/pas-ts-build \
  examples/pi_executor.ts tests/pi_contract_test.ts
node /tmp/pas-ts-build/tests/pi_contract_test.js
python3 tools/reproduce_audit.py
python3 tools/license_gate.py
```

**177 项 Python 测试全部通过**（交接包原有 52 项 + P1 新增 125 项），参考 demo
输出与交接时一致（`{"delivered_notifications":0,"events":1,"pending_notifications":1,"runs":1}`）；
TypeScript strict 编译 + 4 项结构检查通过；审计复现 OK；license gate PASS。

P1 新增覆盖（对应 SPEC §16.1 Scheduling / Transactions / Operations 行）：

- **迁移**：全量建表与 checksum 记账、重复打开幂等、注入坏 SQL 时事务整体回滚、
  库版本新于二进制时拒绝打开。实测 PRAGMA：journal_mode=delete、synchronous=2(FULL)、
  foreign_keys=1（FK 违规真实抛错）。
- **调度**：interval 锚定槽与漂移（槽后 500ms 内准入）、时钟回拨不重放已准入
  slot、runonce 过期窗口、Berlin 2026-03-29 春令时跳空跳过 / 2026-10-25 回拨
  折叠到 earliest（fold_policy=latest 取第二次）、转换日 daily 偏移 1h 变化、
  每月 31 日短月跳过不钳到月底、闰年 2/29、ISO 周几。
- **misfire**：停机 7 天后 heartbeat 恰好补 1 次（不是 336 次）；grace_once 超
  窗记 expired + `episode_slots=336` 可查询 reason、窗内补跑一次；expire 策略
  只在健康 tick 容差内准入；连续错过 episode 计数不重算；job deadline 到期阻断。
- **事务/并发**：同 occurrence 重试单入队（event/run/occurrence 各 1 条）；两个
  独立 SQLite 连接抢同一 run 恰好一胜、4 个 run 被两连接无重复认领；**真实子进程**
  claim 后 `os._exit(1)` 模拟崩溃，父进程重启回收过期 lease 并以新 fence 续作，
  死进程旧 fence 提交被拒；lease 存活期间第二进程拿不到租约。
- **jobs API**：idempotency 重放返回原记录、同 key 异内容 conflict、乐观 revision
  （必须 +1）、暂停/恢复 revision 语义、删除有准入历史的 job 被拒。
- **扫描效率**：due 查询 EXPLAIN QUERY PLAN 走 `jobs_due` 索引；1000 任务注册
  + 全量准入 < 30s（本机 ~2s，环境相关，非基准声明）。

P1 未做/边界（继续成立）：未做跨进程多 writer 长时间压测与断电耐久测试；未做
WAL 启用；store 层 jobs API 不等于公共 facade/daemon（P3+/P7）；run 状态机为
参考切片三态 + failed，§4.3 完整状态随 P3；outbox/grants/approvals 表已建但
写入路径属 P4；`packages/client-ts` 未生成，fold_policy 的 TS 侧同步待 P5。

## 6. 明确未做（交接包历史记录，继续有效）

没有对真实 Hermes gateway、真实 Pi SDK、Muse 后台、邮箱、日历、设备、push 服务或付费模型执行联调；没有发布、安装或提交到用户的仓库；没有验证全部 88 个 Skill 的实际功能；没有完成第三方代码再分发授权核验。

生产版本的完成标准以 SPEC P0–P7 和第 16 节为准。测试桩通过不能用来抹掉上述缺口。
