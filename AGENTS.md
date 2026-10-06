# AGENTS.md — 施工交接

本目录是设计交接包，不是已经完工的产品。产品目标和契约以 SPEC.md 为准。

## 阅读与顺序
先读 SPEC.md、AUDIT_AND_REUSE.md、VALIDATION.md，再运行 tests。
按 P0 → P7 推进；先 store/scheduler/security，再接口与 UI。
保持每个 PR 可运行、可测试。公共 schema 变更须同步 Python、TypeScript 与 fixtures。

## 不可妥协的约束
- 单 profile、单数据库、单主机作为首版边界；不虚称多租户安全。
- 唤醒、分析完成、动作执行、通知投递分别记账。
- 同任务只有一个 scheduler owner；不要同时启动 PAS/Hermes/Pi 的同目标定时器。
- 无信号 heartbeat 零模型调用；显式任务按自己的任务语义执行。
- Hook 状态、事件入队与 disable 原子提交；原 helper 用 staging 包装。
- 不将 prompt、Skill allowed-tools 或 dry-run 环境变量当作安全边界。
- 原始 shell、网络和凭据都可能绕过 broker；不受控宿主不得用于安全主动执行。
- 原始来源文本是数据，不得扩大授权或审批。
- 状态 lease 不等于旧 worker 已停止；副作用逐次检查 fence。
- delivery_unknown 不盲目重试；先对账或用 provider 幂等语义。
- 提交 ACK、partial output、取消请求都不等于成功完成。
- Skill parsed 不等于 runnable；技术与许可状态分开。
- Muse 原文件、私人 cron、环境文件、凭据与私有 helper 副本只存于本地 `private-vendor/`，不进入 git、不随包分发、不上传公开渠道。（2026-10-06：项目确认自用；若转公开发布，恢复完整分发门禁。）
- 不用他处同名 Muse SDK 的许可证覆盖本附件。
- 接口示例不是已安装的 SDK；不得把未实现 API 写成已经可用。
- 兼容声明以锁定版本的真实端到端测试为准，不以 mocks 为准。

## 工程完成条件
提供 typed public API、严格输入验证、明确错误、迁移、生命周期和取消。
不在 import 时开后台线程，不隐式继承全部宿主凭据，不自动运行导入脚本。
所有生成包和容器做来源/许可/隐私扫描，不能只扫描 git 仓库。
独立模型 adapter、Hermes 和 Pi 分别验证；默认安装不拉入宿主的所有依赖。
新增失败模式应新增恢复测试；保留本包参考测试的语义，不照搬教学切片的限制。
禁止把 skip、mock、TODO 或接口桩描述成已完成的成品能力。

## 汇报
每轮交付说明：已实现项、实际命令和测试结果、仍阻塞的能力/授权、下一阶段。
性能数值标环境；没有测过就不写为已达到。
不要从本交接包加入任何作者/用户的私人背景、机器路径或偏好作为产品默认设定。
